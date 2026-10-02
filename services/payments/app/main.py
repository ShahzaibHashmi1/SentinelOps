"""Payments service (port 8002): simulated payment processing with Redis idempotency."""
import logging
from contextlib import asynccontextmanager

from app.config import get_payments_settings
from app.routes import router
from common.app import create_app
from common.db import Database, check_postgres, register_db_error_handler
from common.logging_setup import log_event
from common.redis_client import check_redis, create_redis

settings = get_payments_settings()
logger = logging.getLogger("payments")


@asynccontextmanager
async def lifespan(app):
    app.state.settings = settings
    app.state.db = Database(settings)
    app.state.redis = create_redis(settings)
    await app.state.db.open()
    log_event(
        logger, logging.INFO, "service_started",
        payment_delay_ms_min=settings.payment_delay_ms_min, payment_delay_ms_max=settings.payment_delay_ms_max,
    )
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.db.close()
        log_event(logger, logging.INFO, "service_stopped")


# /ready checks only this service's own datastores: postgres and redis.
app = create_app(
    settings,
    lifespan=lifespan,
    ready_checks={"postgres": check_postgres, "redis": check_redis},
)
register_db_error_handler(app)
app.include_router(router)
