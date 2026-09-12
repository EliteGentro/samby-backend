import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.routes.events import encode_sse
from app.core.config import get_settings
from app.core.security import AuthenticatedUser, get_current_user
from app.services.ai.base import AIProvider
from app.services.ai.factory import get_ai_provider
from app.services.ai.models import ChatRequest
from app.services.context_window import RollingContextWindow

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/chat/stream", summary="Stream a normalized AI response as SSE")
async def stream_chat(
    payload: ChatRequest,
    _user: AuthenticatedUser = Depends(get_current_user),
    provider: AIProvider = Depends(get_ai_provider),
) -> StreamingResponse:
    context = RollingContextWindow(get_settings().ai_context_max_tokens)
    messages = context.fit(payload.messages, reserve_tokens=payload.options.max_tokens)

    async def stream():
        try:
            async for chunk in provider.stream_chat(messages, payload.options):
                yield encode_sse(chunk.model_dump(), event=chunk.type)
        except Exception as exc:
            # Keep provider details out of clients and logs containing user prompts.
            logger.exception("AI provider stream failed: %s", type(exc).__name__)
            yield encode_sse({"message": "AI provider request failed"}, event="error")

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
