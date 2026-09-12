from app.core.config import get_settings
from app.services.ai.base import AIProvider
from app.services.ai.openrouter import OpenRouterProvider

_provider: AIProvider | None = None


def get_ai_provider() -> AIProvider:
    """Composition root: this is the only place that chooses a concrete provider."""
    global _provider
    if _provider is None:
        settings = get_settings()
        if settings.ai_provider == "openrouter":
            _provider = OpenRouterProvider(settings)
        else:
            raise ValueError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")
    return _provider


async def close_ai_provider() -> None:
    global _provider
    if _provider is not None:
        await _provider.close()
        _provider = None
