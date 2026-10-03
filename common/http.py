"""Outbound HTTP between services: explicit timeouts and limits, request-id forwarding, metrics."""
import httpx

from common.context import request_id_var
from common.errors import AppError, describe_exception
from common.metrics import track_dependency
from common.settings import Settings


async def _forward_request_id(request: httpx.Request) -> None:
    # Runs for EVERY outbound request, so X-Request-ID can never be forgotten.
    request_id = request_id_var.get()
    if request_id:
        request.headers["X-Request-ID"] = request_id


def create_http_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """`transport` is only used by unit tests (httpx.MockTransport); production code leaves it None."""
    return httpx.AsyncClient(
        transport=transport,
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


def downstream_error(dependency: str, exc: Exception) -> AppError:
    """504 when the downstream timed out, 502 when it could not be reached or the connection broke.
    Body: {"error", "downstream", "request_id"}."""
    if isinstance(exc, httpx.TimeoutException):
        return AppError(504, "downstream_timeout", detail=describe_exception(exc), downstream=dependency)
    return AppError(502, "downstream_unreachable", detail=describe_exception(exc), downstream=dependency)


def downstream_bad_response(dependency: str, response: httpx.Response) -> AppError:
    """502 for an unexpected answer (5xx, or a 4xx that the caller does not handle)."""
    return AppError(
        502, "downstream_error", detail=f"HTTP {response.status_code}",
        downstream=dependency, downstream_status=response.status_code,
    )


async def request_downstream(
    client: httpx.AsyncClient, dependency: str, method: str, url: str, **kwargs
) -> httpx.Response:
    """call_dependency + httpx errors turned into AppError(504/502). Any HTTP response is returned
    to the caller, which decides what each status code means."""
    try:
        return await call_dependency(client, dependency, method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise downstream_error(dependency, exc) from exc
