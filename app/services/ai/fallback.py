import logging
from collections.abc import AsyncIterator
from typing import Any

from app.services.ai.base import AIProvider
from app.services.ai.models import AIChunk, AIOptions, ChatMessage


logger = logging.getLogger(__name__)


class FallbackAIProvider(AIProvider):
    """Use the secondary provider only when the primary rejects access."""

    def __init__(
        self,
        primary: AIProvider,
        fallback: AIProvider,
        primary_name: str,
        fallback_name: str,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fallback_name = fallback_name

    @staticmethod
    def _is_forbidden(error: Exception) -> bool:
        response = getattr(error, "response", None)
        status = (
            getattr(error, "status_code", None)
            or getattr(error, "code", None)
            or getattr(response, "status_code", None)
        )
        return status == 403

    def _fallback_options(self, options: AIOptions) -> AIOptions:
        # Model IDs are provider-specific. The fallback must use its configured
        # model rather than forwarding an explicit primary-provider override.
        return options.model_copy(update={"model": None})

    def _log_fallback(self) -> None:
        logger.warning(
            "AI provider %s returned 403; retrying with %s",
            self.primary_name,
            self.fallback_name,
        )

    async def complete_chat(
        self,
        messages: list[dict[str, Any]] | list[ChatMessage],
        options: AIOptions,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        try:
            return await self.primary.complete_chat(messages, options, tools)
        except Exception as error:
            if not self._is_forbidden(error):
                raise
            self._log_fallback()
            return await self.fallback.complete_chat(
                messages, self._fallback_options(options), tools
            )

    async def stream_chat(
        self, messages: list[ChatMessage], options: AIOptions
    ) -> AsyncIterator[AIChunk]:
        emitted = False
        try:
            async for chunk in self.primary.stream_chat(messages, options):
                emitted = True
                yield chunk
            return
        except Exception as error:
            if emitted or not self._is_forbidden(error):
                raise
            self._log_fallback()
        async for chunk in self.fallback.stream_chat(
            messages, self._fallback_options(options)
        ):
            yield chunk

    async def health_check(self) -> None:
        try:
            await self.primary.health_check()
        except Exception as error:
            if not self._is_forbidden(error):
                raise
            self._log_fallback()
            await self.fallback.health_check()

    async def close(self) -> None:
        try:
            await self.primary.close()
        finally:
            await self.fallback.close()
