from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.services.ai.models import AIChunk, AIOptions, ChatMessage


class AIProvider(ABC):
    """Dependency-inversion boundary for every AI provider adapter."""

    @abstractmethod
    async def stream_chat(
        self, messages: list[ChatMessage], options: AIOptions
    ) -> AsyncIterator[AIChunk]:
        if False:  # pragma: no cover - marks this async generator contract
            yield AIChunk(type="done")

    @abstractmethod
    async def health_check(self) -> None:
        """Raise when credentials or provider connectivity are unhealthy."""

    async def close(self) -> None:
        """Adapters with persistent clients can override this hook."""
