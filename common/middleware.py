"""One ASGI middleware that gives every request: request id, metrics and an access-log line.

Written as a plain ASGI middleware (not BaseHTTPMiddleware) so the request-id ContextVar
reliably reaches the endpoint code and the response header can be added in one place.
"""
import asyncio
import logging
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from common.context import request_id_var
from common.errors import describe_exception
from common.logging_setup import log_event
from common.metrics import HTTP_DURATION, HTTP_IN_FLIGHT, HTTP_REQUESTS
from common.settings import Settings

logger = logging.getLogger("access")

# Probe endpoints are polled constantly (Docker healthcheck, later Prometheus).
# Successful probe requests are logged at DEBUG so INFO logs show real traffic only.
PROBE_PATHS = {"/health", "/ready", "/metrics"}


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.service = settings.service_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1) request id: accept the caller's, otherwise generate one
        incoming = Headers(scope=scope).get("x-request-id")
        request_id = (incoming or str(uuid.uuid4()))[:128]
        token = request_id_var.set(request_id)

        HTTP_IN_FLIGHT.inc()
        start = time.perf_counter()
        status = 500
        response_started = False
        error: str | None = None

        async def send_wrapper(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id  # 2) return it
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            if not response_started:
                status, error = 499, "client_disconnected"
            raise
        except Exception as exc:  # unhandled bug: log it and answer with a JSON 500
            error = describe_exception(exc)
            if not response_started:
                body = {"error": "internal_error", "request_id": request_id}
                await JSONResponse(body, status_code=500)(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            HTTP_IN_FLIGHT.dec()
            method, path = scope["method"], scope["path"]
            # Route template (e.g. /items/{sku}) keeps metric labels low-cardinality.
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            HTTP_REQUESTS.labels(self.service, method, route, str(status)).inc()
            HTTP_DURATION.labels(self.service, method, route).observe(duration)

            error = error or scope.get("sentinelops_error")
            if status >= 500:
                level = logging.ERROR
            elif path in PROBE_PATHS:
                level = logging.DEBUG
            else:
                level = logging.INFO
            log_event(
                logger, level, "http_request",
                method=method, path=path, status=status,
                duration_ms=round(duration * 1000, 2), error=error,
            )
            request_id_var.reset(token)
