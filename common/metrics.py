"""Prometheus metrics with the names fixed in PROJECT_CONTEXT.md section 5."""
import asyncio
import time
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["service", "method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request duration",
    ["service", "method", "route"], buckets=_LATENCY_BUCKETS,
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_flight", "HTTP requests currently being handled")

DEPENDENCY_REQUESTS = Counter(
    "dependency_requests_total", "Calls to a dependency (postgres, redis, other service)",
    ["dependency", "outcome"],  # outcome: success | error | timeout
)
DEPENDENCY_DURATION = Histogram(
    "dependency_request_duration_seconds", "Duration of calls to a dependency",
    ["dependency"], buckets=_LATENCY_BUCKETS,
)

DB_POOL_IN_USE = Gauge("db_pool_connections_in_use", "Postgres pool connections currently checked out")
DB_POOL_MAX = Gauge("db_pool_connections_max", "Configured maximum size of the postgres pool")

APP_INFO = Gauge("app_info", "Service identity (value is always 1)", ["service", "version"])


class _Tracker:
    outcome: str | None = "success"


@contextmanager
def track_dependency(dependency: str):
    """Count and time one call to a dependency.

    Usage:  with track_dependency("redis"): await redis.get(key)
    Exceptions are re-raised after being counted ("timeout" for timeouts, else "error").
    The caller may set tracker.outcome itself (e.g. an HTTP 5xx response is an "error").
    """
    tracker = _Tracker()
    start = time.perf_counter()
    try:
        yield tracker
    except asyncio.CancelledError:
        tracker.outcome = None  # caller went away; not a dependency failure
        raise
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__
        tracker.outcome = "timeout" if timed_out else "error"
        raise
    finally:
        if tracker.outcome is not None:
            DEPENDENCY_REQUESTS.labels(dependency, tracker.outcome).inc()
            DEPENDENCY_DURATION.labels(dependency).observe(time.perf_counter() - start)
