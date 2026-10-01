"""Redis read cache for single items. Redis is an optimisation, never a requirement:
any Redis error is logged as a WARNING and the caller falls back to postgres."""
import json
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from common.errors import describe_exception
from common.logging_setup import log_event
from common.metrics import track_dependency

logger = logging.getLogger("inventory.cache")

CACHE_TTL_SECONDS = 30


def _key(sku: str) -> str:
    return f"item:{sku}"


def _warn(operation: str, exc: Exception) -> None:
    log_event(logger, logging.WARNING, "redis_unavailable", operation=operation, error=describe_exception(exc))


async def cache_get(redis: Redis, sku: str) -> tuple[str, dict | None]:
    """Returns ("HIT", item), ("MISS", None) or ("ERROR", None) when Redis failed."""
    try:
        with track_dependency("redis"):
            raw = await redis.get(_key(sku))
    except (RedisError, OSError) as exc:
        _warn("get", exc)
        return "ERROR", None
    if raw is None:
        return "MISS", None
    try:
        return "HIT", json.loads(raw)
    except ValueError:
        return "MISS", None  # corrupt entry: treat as a miss, it will be overwritten


async def cache_set(redis: Redis, item: dict) -> None:
    try:
        with track_dependency("redis"):
            await redis.set(_key(item["sku"]), json.dumps(item, default=float), ex=CACHE_TTL_SECONDS)
    except (RedisError, OSError) as exc:
        _warn("set", exc)


async def cache_invalidate(redis: Redis, sku: str) -> None:
    """Called after stock changes so the next read shows the new stock level."""
    try:
        with track_dependency("redis"):
            await redis.delete(_key(sku))
    except (RedisError, OSError) as exc:
        _warn("delete", exc)
