import pytest

from app.core.config import Settings
from app.services.ai.base import AIProvider
from app.services.ai import factory
from app.services.ai.fallback import FallbackAIProvider
from app.services.ai.gemini import GeminiProvider
from app.services.ai.models import AIChunk, AIOptions, ChatMessage


class FakeProvider(AIProvider):
    def __init__(self, result=None, error=None):
        self.result = result or {"role": "assistant", "content": "ready"}
        self.error = error
        self.options = []
        self.closed = False

    async def complete_chat(self, messages, options, tools=None):
        self.options.append(options)
        if self.error:
            raise self.error
        return self.result

    async def stream_chat(self, messages, options):
        self.options.append(options)
        if self.error:
            raise self.error
        yield AIChunk(type="content", text=self.result["content"])
        yield AIChunk(type="done")

    async def health_check(self):
        if self.error:
            raise self.error

    async def close(self):
        self.closed = True


class ProviderError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.asyncio
async def test_factory_selects_gemini_from_ai_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: Settings(
            ai_provider="gemini",
            ai_provider_fallback=None,
            gemini_api_key="test-key",
        ),
    )
    factory._provider = None
    try:
        assert isinstance(factory.get_ai_provider(), GeminiProvider)
    finally:
        await factory.close_ai_provider()


@pytest.mark.asyncio
async def test_factory_wraps_primary_with_configured_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: Settings(
            ai_provider="gemini",
            ai_provider_fallback="openrouter",
            gemini_api_key="test-key",
        ),
    )
    factory._provider = None
    try:
        provider = factory.get_ai_provider()
        assert isinstance(provider, FallbackAIProvider)
        assert isinstance(provider.primary, GeminiProvider)
        assert provider.primary_name == "gemini"
        assert provider.fallback_name == "openrouter"
    finally:
        await factory.close_ai_provider()


@pytest.mark.asyncio
async def test_fallback_provider_retries_only_403_with_configured_model():
    primary = FakeProvider(error=ProviderError(403))
    fallback = FakeProvider()
    provider = FallbackAIProvider(primary, fallback, "gemini", "openrouter")

    result = await provider.complete_chat(
        [{"role": "user", "content": "hello"}],
        AIOptions(model="gemini-override"),
    )

    assert result["content"] == "ready"
    assert fallback.options[0].model is None
    await provider.close()
    assert primary.closed and fallback.closed


@pytest.mark.asyncio
async def test_fallback_provider_does_not_hide_non_403_failures():
    primary = FakeProvider(error=ProviderError(429))
    fallback = FakeProvider()
    provider = FallbackAIProvider(primary, fallback, "gemini", "openrouter")

    with pytest.raises(ProviderError) as raised:
        await provider.complete_chat(
            [{"role": "user", "content": "hello"}], AIOptions()
        )

    assert raised.value.status_code == 429
    assert fallback.options == []


@pytest.mark.asyncio
async def test_streaming_uses_fallback_when_primary_returns_403_before_output():
    primary = FakeProvider(error=ProviderError(403))
    fallback = FakeProvider(result={"role": "assistant", "content": "fallback"})
    provider = FallbackAIProvider(primary, fallback, "gemini", "openrouter")

    chunks = [
        chunk
        async for chunk in provider.stream_chat(
            [ChatMessage(role="user", content="hello")], AIOptions()
        )
    ]

    assert [(chunk.type, chunk.text) for chunk in chunks] == [
        ("content", "fallback"),
        ("done", ""),
    ]
