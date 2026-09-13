from app.core.config import get_settings
from app.services.ai.base import AIProvider
from app.services.ai.fallback import FallbackAIProvider
from app.services.ai.gemini import GeminiProvider
from app.services.ai.openrouter import OpenRouterProvider

_provider: AIProvider | None = None


def _build_provider(name: str, settings) -> AIProvider:
    if name == "openrouter":
        return OpenRouterProvider(settings)
    if name == "gemini":
        return GeminiProvider(settings)
    raise ValueError(f"Unsupported AI provider: {name}")


def get_ai_provider() -> AIProvider:
    """Build the configured primary provider and optional 403 fallback."""
    global _provider
    if _provider is None:
        settings = get_settings()
        primary = _build_provider(settings.ai_provider, settings)
        fallback_name = settings.ai_provider_fallback
        if fallback_name and fallback_name != settings.ai_provider:
            _provider = FallbackAIProvider(
                primary,
                _build_provider(fallback_name, settings),
                settings.ai_provider,
                fallback_name,
            )
        else:
            _provider = primary
    return _provider


async def close_ai_provider() -> None:
    global _provider
    if _provider is not None:
        await _provider.close()
        _provider = None
