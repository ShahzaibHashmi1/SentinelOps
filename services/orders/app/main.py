"""Orders service (port 8001): order workflow across inventory, postgres and payments."""
import logging
from contextlib import asynccontextmanager

from app.routes import router
from common.app import create_app
from common.db import Database, check_postgres, register_db_error_handler
from common.http import create_http_client
from common.logging_setup import log_event
from common.settings import get_settings

settings = get_settings()
logger = logging.getLogger("orders")


@asynccontextmanager
async def lifespan(app):
    if not (settings.inventory_url and settings.payments_url):
        raise RuntimeError("INVENTORY_URL and PAYMENTS_URL are required for the orders service")
    app.state.settings = settings
    app.state.db = Database(settings)
    app.state.http = create_http_client(settings)
    await app.state.db.open()
    log_event(logger, logging.INFO, "service_started")
    try:
        yield
    finally:
        await app.state.http.aclose()
        await app.state.db.close()
        log_event(logger, logging.INFO, "service_stopped")


# /ready checks only this service's OWN datastore (postgres), not inventory or payments.
app = create_app(settings, lifespan=lifespan, ready_checks={"postgres": check_postgres})
register_db_error_handler(app)
app.include_router(router)
