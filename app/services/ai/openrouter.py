import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import Settings
from app.services.ai.base import AIProvider
from app.services.ai.models import AIChunk, AIOptions, ChatMessage


class OpenRouterProvider(AIProvider):
    """OpenRouter adapter; route code only sees normalized AI models."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))
        self._owns_client = client is None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "HTTP-Referer": self.settings.openrouter_site_url,
            "X-Title": self.settings.openrouter_app_name,
            "Content-Type": "application/json",
        }

    def _payload(self, messages: list[ChatMessage], options: AIOptions) -> dict:
        payload: dict = {
            "model": options.model or self.settings.openrouter_model,
            "messages": [message.model_dump() for message in messages],
            "max_tokens": options.max_tokens,
            "temperature": options.temperature,
            "stream": True,
        }
        if options.reasoning_effort is not None:
            payload["reasoning"] = {
                "effort": options.reasoning_effort,
                "exclude": not options.show_thinking,
            }
        elif options.reasoning_max_tokens is not None:
            payload["reasoning"] = {
                "max_tokens": options.reasoning_max_tokens,
                "exclude": not options.show_thinking,
            }
        return payload

    async def complete_chat(
        self,
        messages: list[dict],
        options: AIOptions,
        tools: list[dict] | None = None,
    ) -> dict:
        """Return one OpenAI-compatible assistant message.

        Samby's workspace guide uses this non-streaming path so tool calls can be
        executed deterministically before the final answer is persisted.
        """
        payload: dict = {
            "model": options.model or self.settings.openrouter_model,
            "messages": messages,
            "max_tokens": options.max_tokens,
            "temperature": options.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = await self.client.post(
            f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers=self.headers,
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0].get("message"), dict):
            raise RuntimeError("OpenRouter returned no assistant message")
        return choices[0]["message"]

    async def stream_chat(
        self, messages: list[ChatMessage], options: AIOptions
    ) -> AsyncIterator[AIChunk]:
        url = f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions"
        async with self.client.stream(
            "POST", url, headers=self.headers, json=self._payload(messages, options)
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                reasoning = delta.get("reasoning")
                content = delta.get("content")
                if options.show_thinking and reasoning:
                    yield AIChunk(type="reasoning", text=reasoning)
                if content:
                    yield AIChunk(type="content", text=content)
        yield AIChunk(type="done")

    async def health_check(self) -> None:
        if not self.settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        response = await self.client.get(
            f"{self.settings.openrouter_base_url.rstrip('/')}/key", headers=self.headers
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
