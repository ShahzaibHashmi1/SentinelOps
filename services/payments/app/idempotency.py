"""Idempotency keys in Redis: a repeated charge with the same key returns the first result.

Redis is a fast path, not the only guard: postgres has a UNIQUE constraint on idempotency_key.
If Redis is down the check is skipped (WARNING logged) and the charge still goes through.
"""
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.models import Payment
from common.errors import describe_exception
from common.logging_setup import log_event
from common.metrics import track_dependency

logger = logging.getLogger("payments.idempotency")

KEY_TTL_SECONDS = 24 * 60 * 60


def _key(idempotency_key: str) -> str:
    return f"idem:{idempotency_key}"


def _warn(operation: str, exc: Exception) -> None:
    log_event(
        logger, logging.WARNING, "redis_unavailable",
        operation=operation, error=describe_exception(exc),
        note="idempotency check skipped" if operation == "get" else "idempotency key not stored",
    )


async def lookup(redis: Redis, idempotency_key: str) -> tuple[str, Payment | None]:
    """Returns ("HIT", payment), ("MISS", None) or ("ERROR", None) when Redis failed."""
    try:
        with track_dependency("redis"):
            raw = await redis.get(_key(idempotency_key))
    except (RedisError, OSError) as exc:
        _warn("get", exc)
        return "ERROR", None
    if raw is None:
        return "MISS", None
    try:
        return "HIT", Payment.model_validate_json(raw)
    except ValueError:
        return "MISS", None  # corrupt entry: ignore, it will be overwritten


async def remember(redis: Redis, payment: Payment) -> None:
    try:
        with track_dependency("redis"):
            await redis.set(_key(payment.idempotency_key), payment.model_dump_json(), ex=KEY_TTL_SECONDS)
    except (RedisError, OSError) as exc:
        _warn("set", exc)
