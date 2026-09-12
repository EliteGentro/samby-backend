from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

from app.core.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """Return a small shared pool suitable for constrained/free Redis plans."""
    global _redis
    if _redis is None:
        settings = get_settings()
        pool = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            health_check_interval=30,
        )
        _redis = Redis(connection_pool=pool)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
