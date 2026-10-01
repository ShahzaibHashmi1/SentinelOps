"""Builds a FastAPI app with everything that is the same for all four services."""
from contextlib import AbstractAsyncContextManager
from collections.abc import Callable

from fastapi import FastAPI

from common.errors import AppError, app_error_handler
from common.health import ReadyCheck, build_ops_router
from common.logging_setup import setup_logging
from common.metrics import APP_INFO
from common.middleware import ObservabilityMiddleware
from common.settings import Settings


def create_app(
    settings: Settings,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager],
    ready_checks: dict[str, ReadyCheck],
) -> FastAPI:
    setup_logging(settings)
    app = FastAPI(
        title=f"SentinelOps {settings.service_name}",
        version=settings.app_version,
        lifespan=lifespan,  # startup/shutdown via lifespan (on_event is deprecated)
    )
    app.add_middleware(ObservabilityMiddleware, settings=settings)
    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(build_ops_router(settings, ready_checks))
    APP_INFO.labels(settings.service_name, settings.app_version).set(1)
    return app
