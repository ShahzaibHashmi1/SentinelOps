"""Gateway (port 8000, the only public entry point): routing, request id, timeouts."""
import logging
from contextlib import asynccontextmanager

from app.routes import router
from common.app import create_app
from common.http import create_http_client
from common.logging_setup import log_event
from common.settings import get_settings

settings = get_settings()
logger = logging.getLogger("gateway")


@asynccontextmanager
async def lifespan(app):
    if not (settings.orders_url and settings.payments_url):
        raise RuntimeError("ORDERS_URL and PAYMENTS_URL are required for the gateway")
    app.state.settings = settings
    app.state.http = create_http_client(settings)
    log_event(logger, logging.INFO, "service_started")
    try:
        yield
    finally:
        await app.state.http.aclose()
        log_event(logger, logging.INFO, "service_stopped")


# The gateway owns no datastore, so /ready has no checks and is always 200 while the process runs.
# (A downstream outage shows up as 502/504 on requests, not as gateway "not ready".)
app = create_app(settings, lifespan=lifespan, ready_checks={})
app.include_router(router)
