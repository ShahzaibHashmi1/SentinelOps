"""Read-only HTTP API (D71): GET only, fixed schema, JSON UTF-8, port 9200.

  GET /health                                       liveness, always 200
  GET /ready                                        200 when the build loop finished a cycle within the last 3 intervals
                                                    (a source in no_data does NOT make the service not ready)
  GET /metrics                                      Prometheus text format
  GET /internal/v1/status                           sources, last cycle, bundle counts, settings
  GET /internal/v1/graph                            nodes, edges and depth from the topology
  GET /internal/v1/changes                          ?service= &since=<ISO time> &limit=1..200 (default 50)
  GET /internal/v1/evidence/<incident_id>           the newest bundle of the incident (404 when none yet)
  GET /internal/v1/evidence/<incident_id>/bundles   bundle ids with trigger and built_at
  GET /internal/v1/bundles/<bundle_id>              one bundle

Any other method on a known path gives 405 with "Allow: GET"; an unknown path gives 404. Errors have the body
{"error": {"code": ..., "message": ...}}. The API reads the store through read-only connections; it cannot
start a build and cannot change anything.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .graph import DependencyGraph
from .logging_setup import log_event
from .loop import LoopState
from .metrics import Metrics
from .model import ModelError, fmt_ts, parse_ts
from .store import StoreError, StoreReader
from .topology import Topology

JSON_TYPE = "application/json; charset=utf-8"
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
READY_CYCLES = 3
_QUIET_PATHS = ("/health", "/ready", "/metrics")
_INCIDENT = re.compile(r"^INC-\d+$")
_BUNDLE = re.compile(r"^EVD-\d+$")
EVIDENCE_PREFIX = "/internal/v1/evidence/"
BUNDLE_PREFIX = "/internal/v1/bundles/"


@dataclass
class ApiContext:
    reader: StoreReader
    graph: DependencyGraph
    topology: Topology
    state: LoopState
    metrics: Metrics
    settings: dict
    version: str
    interval_s: float
    logger: logging.Logger
    mono: Callable = time.monotonic
    extra: dict = field(default_factory=dict)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def _error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode("utf-8")


def _json(status: int, obj) -> tuple:
    return status, JSON_TYPE, json.dumps(obj, ensure_ascii=False).encode("utf-8"), {}


def _store_call(fn):
    try:
        return fn()
    except (sqlite3.Error, StoreError) as exc:
        raise ApiError(503, "store_unavailable", f"cannot read the evidence store: {exc}")


def _param(query: dict, name: str):
    values = query.get(name)
    return values[-1] if values else None


# ------------------------------------------------------------------- handlers

def _health(ctx: ApiContext, query: dict):
    return _json(200, {"status": "ok", "service": "evidence", "version": ctx.version})


def _ready(ctx: ApiContext, query: dict):
    snap = ctx.state.snapshot()
    if snap.last_cycle_mono is None:
        raise ApiError(503, "not_ready", "the build loop has not finished a cycle yet")
    age = ctx.mono() - snap.last_cycle_mono
    limit = READY_CYCLES * ctx.interval_s
    if age > limit:
        raise ApiError(503, "not_ready", f"the last build cycle finished {age:.1f} s ago (limit {limit:g} s)")
    return _json(200, {"status": "ready", "last_cycle_age_s": round(age, 3)})


def _metrics(ctx: ApiContext, query: dict):
    return 200, METRICS_TYPE, ctx.metrics.render().encode("utf-8"), {}


def _status(ctx: ApiContext, query: dict):
    snap = ctx.state.snapshot()
    sources = {}
    for name in sorted(snap.sources):
        st = snap.sources[name]
        sources[name] = {"state": st.state, "since": fmt_ts(st.since) if st.since else None, "detail": st.detail}
    counts = _store_call(ctx.reader.counts)
    return _json(200, {
        "service": "evidence", "version": ctx.version, "sources": sources,
        "last_cycle_at": fmt_ts(snap.last_cycle_at) if snap.last_cycle_at else None,
        "cycle_duration_ms": round(snap.duration_s * 1000, 1) if snap.duration_s is not None else None,
        "cycles": snap.cycles, "bundles_total": counts["total"], "bundles_by_trigger": {k: counts[k] for k in ("opened", "refresh", "resolved")},
        "open_incidents_tracked": snap.open_incidents_tracked, "settings": ctx.settings})


def _graph(ctx: ApiContext, query: dict):
    return _json(200, {"nodes": ctx.graph.nodes(), "edges": ctx.graph.edges(), "depth": ctx.graph.depth()})


def _changes(ctx: ApiContext, query: dict):
    service = _param(query, "service")
    if service is not None and service not in ctx.topology.services:
        raise ApiError(400, "bad_request", f"service must be one of {', '.join(ctx.topology.services)}")
    limit = DEFAULT_LIMIT
    raw_limit = _param(query, "limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise ApiError(400, "bad_request", f"limit must be a whole number from 1 to {MAX_LIMIT}")
        if not 1 <= limit <= MAX_LIMIT:
            raise ApiError(400, "bad_request", f"limit must be a whole number from 1 to {MAX_LIMIT}")
    since = _param(query, "since")
    if since is not None:
        try:
            parse_ts(since)
        except ModelError:
            raise ApiError(400, "bad_request", "since must be an ISO 8601 time such as 2026-10-19T12:00:00Z")
    rows = _store_call(lambda: ctx.reader.list_changes(service=service, since=since, limit=limit))
    return _json(200, {"changes": rows})


def _latest(ctx: ApiContext, incident_id: str):
    bundle = _store_call(lambda: ctx.reader.latest_bundle(incident_id))
    if bundle is None:
        raise ApiError(404, "not_found", f"no bundle yet for {incident_id}")
    return _json(200, bundle)


def _bundle_list(ctx: ApiContext, incident_id: str):
    rows = _store_call(lambda: ctx.reader.list_bundles(incident_id))
    return _json(200, {"incident_id": incident_id, "bundles": rows})


def _bundle(ctx: ApiContext, bundle_id: str):
    bundle = _store_call(lambda: ctx.reader.get_bundle(bundle_id))
    if bundle is None:
        raise ApiError(404, "not_found", f"no bundle {bundle_id}")
    return _json(200, bundle)


ROUTES = {
    "/health": _health, "/ready": _ready, "/metrics": _metrics, "/internal/v1/status": _status,
    "/internal/v1/graph": _graph, "/internal/v1/changes": _changes,
}


def _resolve(path: str):
    """(handler, argument) for a known path, else None. Ids with an unexpected shape are simply not found."""
    if path in ROUTES:
        return ROUTES[path], None
    if path.startswith(EVIDENCE_PREFIX):
        rest = urllib.parse.unquote(path[len(EVIDENCE_PREFIX):]).split("/")
        if len(rest) == 1 and _INCIDENT.match(rest[0]):
            return _latest, rest[0]
        if len(rest) == 2 and rest[1] == "bundles" and _INCIDENT.match(rest[0]):
            return _bundle_list, rest[0]
    if path.startswith(BUNDLE_PREFIX):
        rest = urllib.parse.unquote(path[len(BUNDLE_PREFIX):])
        if _BUNDLE.match(rest):
            return _bundle, rest
    return None


def dispatch(ctx: ApiContext, method: str, target: str) -> tuple:
    """(status, content type, body bytes, extra headers). A pure function of the call: no sockets."""
    parsed = urllib.parse.urlsplit(target)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    try:
        found = _resolve(path)
        if found is None:
            raise ApiError(404, "not_found", f"no such path: {path}")
        if method != "GET":
            raise ApiError(405, "method_not_allowed", f"{method} is not allowed here, use GET", headers={"Allow": "GET"})
        handler, argument = found
        return handler(ctx, query) if argument is None else handler(ctx, argument)
    except ApiError as exc:
        return exc.status, JSON_TYPE, _error_body(exc.code, exc.message), exc.headers
    except Exception as exc:  # never a stack trace to the client
        log_event(ctx.logger, logging.ERROR, "api_error", method=method, path=path, error=f"{exc.__class__.__name__}: {exc}")
        return 500, JSON_TYPE, _error_body("internal_error", "unexpected error, see the evidence log"), {}


# --------------------------------------------------------------------- server

_MAX_DRAIN_BYTES = 1 << 20


class EvidenceHandler(BaseHTTPRequestHandler):
    server_version = "sentinelops-evidence"
    sys_version = ""
    timeout = 10

    def _drain_body(self) -> None:
        """Read and drop a request body before answering, so that the client sees the answer and not a reset."""
        try:
            remaining = min(int(self.headers.get("Content-Length") or 0), _MAX_DRAIN_BYTES)
        except ValueError:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    def _serve(self) -> None:
        started = time.monotonic()
        ctx: ApiContext = self.server.ctx
        self._drain_body()
        status, content_type, body, headers = dispatch(ctx, self.command, self.path)
        # logged BEFORE the response is sent: once a client has the answer, its log line already exists (no race in tests)
        path = urllib.parse.urlsplit(self.path).path
        level = logging.DEBUG if path in _QUIET_PATHS else (logging.WARNING if status >= 500 else logging.INFO)
        log_event(ctx.logger, level, "http_request", method=self.command, path=path, status=status,
                  duration_ms=round((time.monotonic() - started) * 1000, 2))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve

    def log_message(self, format, *args):
        pass


class EvidenceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, ctx: ApiContext):
        super().__init__(address, EvidenceHandler)
        self.ctx = ctx


def make_server(ctx: ApiContext, host: str = "0.0.0.0", port: int = 9200) -> EvidenceServer:
    return EvidenceServer((host, port), ctx)
