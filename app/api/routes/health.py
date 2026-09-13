import asyncio

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.db.session import SessionFactory
from app.services.redis import get_redis

router = APIRouter()


class HealthResponse(BaseModel):
    status: str


async def _check_database() -> None:
    async with SessionFactory() as session:
        await session.execute(text("SELECT 1"))


async def _check_redis() -> None:
    if not await get_redis().ping():
        raise RuntimeError("Redis did not return PONG")


@router.get("/live", response_model=HealthResponse, summary="Process liveness")
async def live() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse, summary="Core dependency readiness")
async def ready() -> HealthResponse:
    # The configured AI provider is intentionally checked by the separate health suite;
    # putting third-party APIs in a readiness probe can restart a healthy service.
    results = await asyncio.gather(_check_database(), _check_redis(), return_exceptions=True)
    if any(isinstance(result, Exception) for result in results):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A core dependency is unavailable",
        )
    return HealthResponse(status="ok")
