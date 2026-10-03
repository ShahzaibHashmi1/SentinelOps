"""Helpers shared by the unit tests of all four services.

Unit tests use fakes instead of postgres, redis and the network:
  FakeDb / FakeRedis            replace app.state.db / app.state.redis
  httpx.MockTransport           replaces the network (see create_http_client(transport=...))
  httpx.ASGITransport           calls the FastAPI app in-process (no server, no lifespan)
Nothing here is imported by production code.
"""
import io
import json
import logging
import re
import uuid
from contextlib import contextmanager

import httpx

from common.logging_setup import JsonFormatter
from common.settings import Settings

REQUIRED_METRICS = (
    "http_requests_total", "http_request_duration_seconds", "http_requests_in_flight",
    "dependency_requests_total", "dependency_request_duration_seconds",
    "db_pool_connections_in_use", "db_pool_connections_max", "app_info",
)


def make_client(app) -> httpx.AsyncClient:
    """HTTP client that talks to the app in-process."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class FakeDb:
    """Stands in for common.db.Database. `handler(sql, params)` returns what the query would return
    (a dict, a list of dicts, None, or a row count for execute())."""

    def __init__(self, handler=None):
        self.handler = handler
        self.calls: list[tuple[str, object]] = []
        self.down = False

    def _run(self, sql, params):
        if self.down:
            import psycopg
            raise psycopg.OperationalError("fake postgres is down")
        self.calls.append((" ".join(sql.split()), params))
        return self.handler(" ".join(sql.split()), params) if self.handler else None

    async def fetch_one(self, sql, params=None):
        return self._run(sql, params)

    async def fetch_all(self, sql, params=None):
        return self._run(sql, params) or []

    async def execute(self, sql, params=None):
        return self._run(sql, params)

    async def ping(self):
        self._run("SELECT 1", None)


class FakeRedis:
    """Dictionary-backed stand-in for redis.asyncio.Redis. Set `down = True` to simulate an outage."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.down = False

    def _check(self):
        if self.down:
            from redis.exceptions import ConnectionError as RedisConnectionError
            raise RedisConnectionError("fake redis is down")

    async def get(self, key):
        self._check()
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self._check()
        self.data[key] = value
        return True

    async def delete(self, *keys):
        self._check()
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def ping(self):
        self._check()
        return True

    async def aclose(self):
        pass


@contextmanager
def capture_logs(settings: Settings):
    """Collect every log line written inside the `with` block, formatted by our real JsonFormatter.
    Yields a function returning the parsed lines (list of dicts)."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(settings.service_name, settings.app_version))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    finally:
        root.removeHandler(handler)


# ---- generic checks, called from every service's tests/test_common.py ---------------------------

async def check_health(client: httpx.AsyncClient, settings: Settings) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == settings.service_name
    assert body["version"] == settings.app_version
    assert body["uptime_seconds"] >= 0


async def check_request_id(client: httpx.AsyncClient) -> None:
    # An incoming id is kept and returned ...
    r = await client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["x-request-id"] == "trace-abc-123"
    # ... and a missing one is generated (uuid4).
    r = await client.get("/health")
    assert uuid.UUID(r.headers["x-request-id"]).version == 4


async def check_json_access_log(client: httpx.AsyncClient, settings: Settings) -> None:
    with capture_logs(settings) as lines:
        # /health is logged at DEBUG, so use a normal path: an unknown URL gives a 404 access line.
        r = await client.get("/does-not-exist", headers={"X-Request-ID": "log-test-1"})
    assert r.status_code == 404
    access = [line for line in lines() if line["event"] == "http_request"]
    assert len(access) == 1  # exactly one access-log line per request
    line = access[0]
    for field in ("ts", "level", "service", "version", "request_id", "event",
                  "method", "path", "status", "duration_ms"):
        assert field in line, f"missing log field {field}"
    assert line["service"] == settings.service_name
    assert line["version"] == settings.app_version
    assert line["request_id"] == "log-test-1"
    assert (line["method"], line["path"], line["status"]) == ("GET", "/does-not-exist", 404)
    assert line["duration_ms"] >= 0


async def check_metrics(client: httpx.AsyncClient) -> None:
    text = (await client.get("/metrics")).text
    for name in REQUIRED_METRICS:
        assert re.search(rf"^# HELP {name} ", text, re.M), f"metric {name} missing from /metrics"
