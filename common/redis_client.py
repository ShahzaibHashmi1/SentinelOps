"""Redis client with short, explicit timeouts (a dead Redis must fail fast, never hang)."""
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from common.settings import Settings

REDIS_TIMEOUT_SECONDS = 1.0


def create_redis(settings: Settings) -> Redis:
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required for this service")
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),  # no hidden retries: callers decide how to degrade
    )


async def check_redis(app: FastAPI) -> None:
    """Readiness check used by /ready."""
    await app.state.redis.ping()
