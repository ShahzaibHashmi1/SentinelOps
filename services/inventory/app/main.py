"""Inventory service (port 8003): stock levels, Redis read cache, atomic reserve/release."""
import logging
from contextlib import asynccontextmanager

from app.routes import router
from common.app import create_app
from common.db import Database, check_postgres, register_db_error_handler
from common.logging_setup import log_event
from common.redis_client import check_redis, create_redis
from common.settings import get_settings

settings = get_settings()
logger = logging.getLogger("inventory")


@asynccontextmanager
async def lifespan(app):
    app.state.db = Database(settings)
    app.state.redis = create_redis(settings)
    await app.state.db.open()
    log_event(logger, logging.INFO, "service_started")
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
