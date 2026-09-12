import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.security import AuthenticatedUser, get_current_user
from app.services.redis import get_redis

router = APIRouter()


class EventCreate(BaseModel):
    message: str = Field(min_length=1, max_length=500)


def encode_sse(data: dict, *, event: str, event_id: str | None = None) -> str:
    """Encode one standards-compliant SSE frame."""
    fields = []
    if event_id is not None:
        fields.append(f"id: {event_id}")
    fields.extend((f"event: {event}", f"data: {json.dumps(data)}"))
    return "\n".join(fields) + "\n\n"


@router.get("/stream", summary="Subscribe to protected Redis-backed SSE events")
async def stream_events(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    channel = get_settings().redis_event_channel

    async def stream():
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            yield encode_sse(
                {"message": "connected", "subject": user.subject}, event="connected"
            )
            while not await request.is_disconnected():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if message is None:
                    yield encode_sse(
                        {"at": datetime.now(UTC).isoformat()}, event="heartbeat"
                    )
                    continue
                yield encode_sse(json.loads(message["data"]), event="message")
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Publish an SSE example event")
async def publish_event(
    payload: EventCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> dict:
    event = {
        "message": payload.message,
        "subject": user.subject,
        "at": datetime.now(UTC).isoformat(),
    }
    subscribers = await redis.publish(get_settings().redis_event_channel, json.dumps(event))
    return {"published": True, "subscribers": subscribers}
