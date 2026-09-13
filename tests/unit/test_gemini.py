from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.services.ai.gemini import GeminiProvider
from app.services.ai.models import AIOptions, ChatMessage


class FakeInteractions:
    def __init__(self, response: Any = None, events: list[Any] | None = None) -> None:
        self.response = response
        self.events = events or []
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if request.get("stream"):
            async def event_stream():
                for event in self.events:
                    yield event

            return event_stream()
        return self.response


def fake_client(interactions: FakeInteractions) -> Any:
    return SimpleNamespace(aio=SimpleNamespace(interactions=interactions))


def test_gemini_translates_messages_options_and_tools() -> None:
    interactions = FakeInteractions()
    provider = GeminiProvider(
        Settings(gemini_api_key="test-key", gemini_model="gemini-test"),
        client=fake_client(interactions),
    )

    request = provider._request(
        [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Check stock."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "check_stock",
                            "arguments": '{"sku":"A1"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "check_stock",
                "content": '{"quantity":3}',
            },
        ],
        AIOptions(
            model="gemini-override",
            max_tokens=321,
            temperature=0.2,
            reasoning_effort="xhigh",
            show_thinking=True,
        ),
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "check_stock",
                    "description": "Check inventory",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert request["model"] == "gemini-override"
    assert request["system_instruction"] == "Be concise."
    assert [step["type"] for step in request["input"]] == [
        "user_input",
        "function_call",
        "function_result",
    ]
    assert request["input"][1]["arguments"] == {"sku": "A1"}
    assert request["extra_body"]["generation_config"] == {
        "max_output_tokens": 321,
        "temperature": 0.2,
        "thinking_summaries": "auto",
        "thinking_level": "high",
    }
    assert request["tools"] == [
        {
            "type": "function",
            "name": "check_stock",
            "description": "Check inventory",
            "parameters": {"type": "object"},
        }
    ]


@pytest.mark.asyncio
async def test_gemini_normalizes_complete_interaction() -> None:
    response = SimpleNamespace(
        output_text=None,
        steps=[
            SimpleNamespace(
                type="function_call",
                id="call-2",
                name="lookup",
                arguments={"query": "cash"},
            )
        ],
    )
    interactions = FakeInteractions(response=response)
    provider = GeminiProvider(
        Settings(gemini_api_key="test-key"), client=fake_client(interactions)
    )

    message = await provider.complete_chat(
        [ChatMessage(role="user", content="Look it up")], AIOptions()
    )

    assert message == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-2",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query": "cash"}'},
            }
        ],
    }
    assert interactions.requests[0]["store"] is False


@pytest.mark.asyncio
async def test_gemini_normalizes_stream_events() -> None:
    events = [
        SimpleNamespace(
            event_type="step.delta",
            delta=SimpleNamespace(
                type="thought_summary",
                content=SimpleNamespace(type="text", text="brief thought"),
            ),
        ),
        SimpleNamespace(
            event_type="step.delta",
            delta=SimpleNamespace(type="text", text="hello"),
        ),
    ]
    interactions = FakeInteractions(events=events)
    provider = GeminiProvider(
        Settings(gemini_api_key="test-key"), client=fake_client(interactions)
    )

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="hello")],
            AIOptions(show_thinking=True, reasoning_effort="low"),
        )
    ]

    assert [(chunk.type, chunk.text) for chunk in chunks] == [
        ("reasoning", "brief thought"),
        ("content", "hello"),
        ("done", ""),
    ]
    assert interactions.requests[0]["stream"] is True


def test_gemini_rejects_unsupported_reasoning_budget() -> None:
    provider = GeminiProvider(
        Settings(gemini_api_key="test-key"), client=fake_client(FakeInteractions())
    )

    with pytest.raises(ValueError, match="use reasoning_effort"):
        provider._generation_config(AIOptions(reasoning_max_tokens=100))
