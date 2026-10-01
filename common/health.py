"""/health, /ready and /metrics, identical on every service."""
import asyncio
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from common.errors import describe_exception, remember_error
from common.settings import Settings

# A readiness check receives the app (to reach app.state.db / app.state.redis) and raises on failure.
ReadyCheck = Callable[[FastAPI], Awaitable[None]]

READY_CHECK_TIMEOUT_SECONDS = 1.0


def build_ops_router(settings: Settings, checks: dict[str, ReadyCheck]) -> APIRouter:
    router = APIRouter(tags=["ops"])
    started_at = time.monotonic()

    @router.get("/health")
    async def health():
        """Liveness: the process is up. Never touches a dependency."""
        return {
            "status": "ok",
            "service": settings.service_name,
            "version": settings.app_version,
            "uptime_seconds": round(time.monotonic() - started_at, 1),
        }

    @router.get("/ready")
    async def ready(request: Request):
        """Readiness: only this service's OWN datastores, each limited to 1 second."""

        async def run(name: str, check: ReadyCheck):
            try:
                await asyncio.wait_for(check(request.app), READY_CHECK_TIMEOUT_SECONDS)
                return name, {"ok": True}
            except asyncio.TimeoutError:
                return name, {"ok": False, "error": f"timeout after {READY_CHECK_TIMEOUT_SECONDS}s"}
            except Exception as exc:
                return name, {"ok": False, "error": describe_exception(exc)}

        results = dict(await asyncio.gather(*(run(n, c) for n, c in checks.items())))
        failed = [f"{n}: {r['error']}" for n, r in results.items() if not r["ok"]]
        body = {
            "status": "not_ready" if failed else "ready",
            "service": settings.service_name,
            "checks": results,
        }
        if failed:
            remember_error(request, "not_ready: " + "; ".join(failed))
            return JSONResponse(body, status_code=503)
        return body

    @router.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return router
