from typing import NoReturn

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.services.ai.openrouter import OpenRouterProvider

pytestmark = pytest.mark.health


def fail_safely(service: str, exc: Exception) -> NoReturn:
    """Do not let client-library tracebacks print URLs containing credentials."""
    response = getattr(exc, "response", None)
    status = f" (HTTP {response.status_code})" if response is not None else ""
    pytest.fail(f"{service} health check failed: {type(exc).__name__}{status}", pytrace=False)


async def test_postgresql_connection() -> None:
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"timeout": settings.database_connect_timeout_seconds},
    )
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    except Exception as exc:
        fail_safely("PostgreSQL", exc)
    finally:
        await engine.dispose()


async def test_redis_connection() -> None:
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        assert await redis.ping() is True
    except Exception as exc:
        fail_safely("Redis", exc)
    finally:
        await redis.aclose()


async def test_openrouter_authenticated_connection() -> None:
    provider = OpenRouterProvider(get_settings())
    try:
        await provider.health_check()
    except Exception as exc:
        fail_safely("OpenRouter", exc)
    finally:
        await provider.close()
