"""Outbound HTTP between services: explicit timeouts and limits, request-id forwarding, metrics."""
import httpx

from common.context import request_id_var
from common.metrics import track_dependency
from common.settings import Settings


async def _forward_request_id(request: httpx.Request) -> None:
    # Runs for EVERY outbound request, so X-Request-ID can never be forgotten.
    request_id = request_id_var.get()
    if request_id:
        request.headers["X-Request-ID"] = request_id


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
        ),
        event_hooks={"request": [_forward_request_id]},
    )


async def call_dependency(
    client: httpx.AsyncClient, dependency: str, method: str, url: str, **kwargs
) -> httpx.Response:
    """Call another service and record dependency_* metrics.

    Outcome: success for any response below 500 (a 404/409 is a business answer),
    error for 5xx or connection errors, timeout for timeouts. Exceptions are re-raised.
    """
    with track_dependency(dependency) as tracker:
        response = await client.request(method, url, **kwargs)
        if response.status_code >= 500:
            tracker.outcome = "error"
    return response
