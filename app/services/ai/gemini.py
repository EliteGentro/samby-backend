import json
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types

from app.core.config import Settings
from app.services.ai.base import AIProvider
from app.services.ai.models import AIChunk, AIOptions, ChatMessage


class GeminiProvider(AIProvider):
    """Google GenAI Interactions adapter with provider-neutral responses."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self._owns_client = client is None
        if client is None:
            if not settings.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY is not configured")
            client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(timeout=60_000),
            )
        self.client = client

    @staticmethod
    def _content(text: Any) -> list[dict[str, str]]:
        return [{"type": "text", "text": str(text or "")}]

    @staticmethod
    def _arguments(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _input(
        self, messages: list[dict[str, Any]] | list[ChatMessage]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system: list[str] = []
        steps: list[dict[str, Any]] = []
        for item in messages:
            message = item.model_dump() if isinstance(item, ChatMessage) else item
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                if content:
                    system.append(str(content))
                continue
            if role == "user":
                steps.append({"type": "user_input", "content": self._content(content)})
                continue
            if role == "assistant":
                if content:
                    steps.append({"type": "model_output", "content": self._content(content)})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    steps.append(
                        {
                            "type": "function_call",
                            "id": str(call.get("id") or "unknown"),
                            "name": str(function.get("name") or "unknown"),
                            "arguments": self._arguments(function.get("arguments")),
                        }
                    )
                continue
            if role == "tool":
                steps.append(
                    {
                        "type": "function_result",
                        "call_id": str(message.get("tool_call_id") or "unknown"),
                        "name": str(message.get("name") or "unknown"),
                        "result": self._content(content),
                    }
                )
                continue
            raise ValueError(f"Unsupported chat message role: {role}")
        return "\n\n".join(system) or None, steps

    @staticmethod
    def _tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        translated: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") != "function":
                translated.append(tool)
                continue
            function = tool.get("function")
            if not isinstance(function, dict):
                translated.append(tool)
                continue
            translated.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description"),
                    "parameters": function.get("parameters", {}),
                }
            )
        return translated

    @staticmethod
    def _generation_config(options: AIOptions) -> dict[str, Any]:
        if options.reasoning_max_tokens is not None:
            raise ValueError(
                "Gemini Interactions does not support reasoning_max_tokens; "
                "use reasoning_effort instead"
            )
        config: dict[str, Any] = {
            "max_output_tokens": options.max_tokens,
            "temperature": options.temperature,
            "thinking_summaries": "auto" if options.show_thinking else "none",
        }
        if options.reasoning_effort is not None:
            config["thinking_level"] = {
                "none": "minimal",
                "minimal": "minimal",
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }[options.reasoning_effort]
        return config

    def _request(
        self,
        messages: list[dict[str, Any]] | list[ChatMessage],
        options: AIOptions,
        tools: list[dict[str, Any]] | None = None,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        system_instruction, input_steps = self._input(messages)
        request: dict[str, Any] = {
            "model": options.model or self.settings.gemini_model,
            "input": input_steps,
            # google-genai 2.23's generated Interactions serializer drops the
            # documented temperature field. extra_body preserves the complete
            # generation_config while still using the SDK request lifecycle.
            "extra_body": {"generation_config": self._generation_config(options)},
            "store": False,
        }
        if system_instruction:
            request["system_instruction"] = system_instruction
        translated_tools = self._tools(tools)
        if translated_tools:
            request["tools"] = translated_tools
        if stream:
            request["stream"] = True
        return request

    @staticmethod
    def _normalized_response(interaction: Any) -> dict[str, Any]:
        tool_calls = []
        for step in getattr(interaction, "steps", None) or []:
            if getattr(step, "type", None) != "function_call":
                continue
            arguments = getattr(step, "arguments", {}) or {}
            tool_calls.append(
                {
                    "id": str(getattr(step, "id", "unknown")),
                    "type": "function",
                    "function": {
                        "name": str(getattr(step, "name", "unknown")),
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": getattr(interaction, "output_text", None),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    async def complete_chat(
        self,
        messages: list[dict[str, Any]] | list[ChatMessage],
        options: AIOptions,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        interaction = await self.client.aio.interactions.create(
            **self._request(messages, options, tools)
        )
        return self._normalized_response(interaction)

    async def stream_chat(
        self, messages: list[ChatMessage], options: AIOptions
    ) -> AsyncIterator[AIChunk]:
        stream = await self.client.aio.interactions.create(
            **self._request(messages, options, stream=True)
        )
        async for event in stream:
            if getattr(event, "event_type", None) != "step.delta":
                continue
            delta = getattr(event, "delta", None)
            delta_type = getattr(delta, "type", None)
            if delta_type == "text" and getattr(delta, "text", None):
                yield AIChunk(type="content", text=delta.text)
            elif delta_type == "thought_summary" and options.show_thinking:
                content = getattr(delta, "content", None)
                if getattr(content, "type", None) == "text" and content.text:
                    yield AIChunk(type="reasoning", text=content.text)
        yield AIChunk(type="done")

    async def health_check(self) -> None:
        if not self.settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        await self.client.aio.models.get(model=self.settings.gemini_model)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aio.aclose()
