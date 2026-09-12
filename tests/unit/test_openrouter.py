import httpx
import pytest

from app.core.config import Settings
from app.services.ai.models import AIOptions, ChatMessage
from app.services.ai.openrouter import OpenRouterProvider


def test_openrouter_translates_normalized_reasoning_options() -> None:
    provider = OpenRouterProvider(
        Settings(openrouter_api_key="test-key"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
    )

    payload = provider._payload(
        [ChatMessage(role="user", content="hello")],
        AIOptions(reasoning_effort="high", show_thinking=True, max_tokens=200),
    )

    assert payload["reasoning"] == {"effort": "high", "exclude": False}
    assert payload["max_tokens"] == 200


@pytest.mark.asyncio
async def test_openrouter_normalizes_stream_chunks() -> None:
    body = (
        'data: {"choices":[{"delta":{"reasoning":"brief thought"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=body, headers={"Content-Type": "text/event-stream"}
            )
        )
    )
    provider = OpenRouterProvider(Settings(openrouter_api_key="test-key"), client=client)
    try:
        chunks = [
            chunk
            async for chunk in provider.stream_chat(
                [ChatMessage(role="user", content="hello")],
                AIOptions(show_thinking=True, reasoning_effort="low"),
            )
        ]
    finally:
        await client.aclose()

    assert [(chunk.type, chunk.text) for chunk in chunks] == [
        ("reasoning", "brief thought"),
        ("content", "hello"),
        ("done", ""),
    ]
