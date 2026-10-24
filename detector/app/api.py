"""Read-only HTTP API (decision D60): GET only, fixed schema, JSON UTF-8, port 9100.

  GET /health                       liveness, always 200
  GET /ready                        200 when the loop finished a cycle within the last 3 intervals, else 503
                                    (a source in no_data does NOT make the detector not ready)
  GET /metrics                      Prometheus text format
  GET /internal/v1/status           sources, last cycle, rules loaded, open incident, per-rule states, settings
  GET /internal/v1/rules            the loaded rules
  GET /internal/v1/incidents        ?status=open|resolved &limit=1..200 (default 50) &since=<ISO time>
  GET /internal/v1/incidents/<id>   one incident or 404

Any other method on a known path gives 405 with "Allow: GET"; an unknown path gives 404.
Errors have the body {"error": {"code": ..., "message": ...}}. Incidents are read through a read-only
connection opened per request; the API never touches the writer.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .logging_setup import log_event
from .metrics import Metrics
from .model import FIRING, NO_DATA, OK, iso_z, parse_iso
from .store import StoreReader

JSON_TYPE = "application/json; charset=utf-8"
METRICS_TYPE = "text/plain; version=0.0.4; charset=utf-8"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
READY_CYCLES = 3
_QUIET_PATHS = ("/health", "/ready", "/metrics")      # logged at DEBUG, like the M1 services


@dataclass
class ApiContext:
    db_path: str
    rules: list
    state: object                      # LoopState
    metrics: Metrics
    settings: dict
    version: str
    interval_s: float
    logger: logging.Logger
    mono: Callable = time.monotonic
    extra: dict = field(default_factory=dict)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def _error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False).encode("utf-8")


def _json(status: int, obj) -> tuple:
    return status, JSON_TYPE, json.dumps(obj, ensure_ascii=False).encode("utf-8"), {}


# ------------------------------------------------------------------- handlers

def _health(ctx: ApiContext, query: dict):
    return _json(200, {"status": "ok", "service": "detector", "version": ctx.version})


def _ready(ctx: ApiContext, query: dict):
    snap = ctx.state.snapshot()
    if snap.last_cycle_mono is None:
        raise ApiError(503, "not_ready", "the evaluation loop has not finished a cycle yet")
    age = ctx.mono() - snap.last_cycle_mono
    if age > READY_CYCLES * ctx.interval_s:
        raise ApiError(503, "not_ready", f"the last evaluation cycle finished {age:.1f} s ago "
                                         f"(limit {READY_CYCLES * ctx.interval_s:g} s)")
    return _json(200, {"status": "ready", "last_cycle_age_s": round(age, 3)})


def _metrics(ctx: ApiContext, query: dict):
    return 200, METRICS_TYPE, ctx.metrics.render().encode("utf-8"), {}


def _rule_states(ctx: ApiContext, observations: list) -> list:
    by_rule: dict = {}
    for obs in observations:
        by_rule.setdefault(obs.rule_id, []).append(obs)
    rows = []
    for rule in ctx.rules:
        obs_list = sorted(by_rule.get(rule.id, []), key=lambda o: o.subject or "")
        states = {o.state for o in obs_list}
        # firing beats no_data beats ok; a rule that has not been evaluated yet counts as no_data
        state = FIRING if FIRING in states else (NO_DATA if (NO_DATA in states or not states) else OK)
        rows.append({
            "rule_id": rule.id, "name": rule.name, "source": rule.source, "state": state,
            "subjects": [{"subject": o.subject, "state": o.state, "value": o.value, "detail": o.detail}
                         for o in obs_list]})
    return rows


def _status(ctx: ApiContext, query: dict):
    snap = ctx.state.snapshot()
    sources = {}
    for name in sorted(snap.sources):
        st = snap.sources[name]
        sources[name] = {"state": st.state, "since": iso_z(st.since) if st.since else None, "detail": st.detail}
    open_id = None
    try:
        with StoreReader(ctx.db_path) as reader:
            open_id = reader.open_incident_id()
    except sqlite3.Error as exc:
        raise ApiError(503, "store_unavailable", f"cannot read the incident store: {exc}")
    return _json(200, {
        "service": "detector", "version": ctx.version,
        "sources": sources,
        "last_cycle_at": iso_z(snap.last_cycle_at) if snap.last_cycle_at else None,
        "cycle_duration_ms": round(snap.duration_s * 1000, 1) if snap.duration_s is not None else None,
        "cycles": snap.cycles,
        "rules_loaded": len(ctx.rules),
        "open_incident_id": open_id,
        "rules": _rule_states(ctx, snap.observations),
        "settings": ctx.settings})


def _rules(ctx: ApiContext, query: dict):
    return _json(200, {"rules": [r.to_dict() for r in ctx.rules]})


def _param(query: dict, name: str) -> Optional[str]:
    values = query.get(name)
    return values[-1] if values else None


def _incidents(ctx: ApiContext, query: dict):
    status = _param(query, "status")
    if status is not None and status not in ("open", "resolved"):
        raise ApiError(400, "bad_request", "status must be open or resolved")
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
            parse_iso(since)
        except ValueError:
            raise ApiError(400, "bad_request", "since must be an ISO 8601 time such as 2026-10-19T12:00:00Z")
    try:
        with StoreReader(ctx.db_path) as reader:
            incidents = reader.list_incidents(status=status, limit=limit, since=since)
    except sqlite3.Error as exc:
        raise ApiError(503, "store_unavailable", f"cannot read the incident store: {exc}")
    return _json(200, {"incidents": [i.to_api() for i in incidents]})


def _incident(ctx: ApiContext, incident_id: str):
    try:
        with StoreReader(ctx.db_path) as reader:
            incident = reader.get_incident(incident_id)
    except sqlite3.Error as exc:
        raise ApiError(503, "store_unavailable", f"cannot read the incident store: {exc}")
    if incident is None:
        raise ApiError(404, "not_found", f"no incident {incident_id}")
    return _json(200, incident.to_api())


ROUTES = {
    "/health": _health, "/ready": _ready, "/metrics": _metrics,
    "/internal/v1/status": _status, "/internal/v1/rules": _rules, "/internal/v1/incidents": _incidents,
}
INCIDENT_PREFIX = "/internal/v1/incidents/"


def dispatch(ctx: ApiContext, method: str, target: str) -> tuple:
    """(status, content type, body bytes, extra headers). Pure function of the request: no sockets."""
    parsed = urllib.parse.urlsplit(target)
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    try:
        if path in ROUTES:
            handler = ROUTES[path]
            args = (ctx, query)
        elif path.startswith(INCIDENT_PREFIX) and "/" not in path[len(INCIDENT_PREFIX):] and path != INCIDENT_PREFIX:
            handler = _incident
            args = (ctx, urllib.parse.unquote(path[len(INCIDENT_PREFIX):]))
        else:
            raise ApiError(404, "not_found", f"no such path: {path}")
        if method != "GET":
            raise ApiError(405, "method_not_allowed", f"{method} is not allowed here, use GET",
                           headers={"Allow": "GET"})
        return handler(*args)
    except ApiError as exc:
        return exc.status, JSON_TYPE, _error_body(exc.code, exc.message), exc.headers
    except Exception as exc:  # never a stack trace to the client
        log_event(ctx.logger, logging.ERROR, "api_error", method=method, path=path,
                  error=f"{exc.__class__.__name__}: {exc}")
        return 500, JSON_TYPE, _error_body("internal_error", "unexpected error, see the detector log"), {}


# --------------------------------------------------------------------- server

_MAX_DRAIN_BYTES = 1 << 20      # a request body is read and dropped up to this size


class DetectorHandler(BaseHTTPRequestHandler):
    server_version = "sentinelops-detector"
    sys_version = ""
    timeout = 10                # a client that announces a body and never sends it must not hold a thread for ever

    def _drain_body(self) -> None:
        """Read and drop the request body before answering. The API accepts no body (every route but GET is a 405),
        but a server that closes the connection with unread data makes the client see a connection reset instead of
        the answer; on Windows this showed up as WinError 10053 in a test."""
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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        path = urllib.parse.urlsplit(self.path).path
        level = logging.DEBUG if path in _QUIET_PATHS else (logging.WARNING if status >= 500 else logging.INFO)
        log_event(ctx.logger, level, "http_request", method=self.command, path=path, status=status,
                  duration_ms=round((time.monotonic() - started) * 1000, 2))

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve

    def log_message(self, format, *args):  # the access log is the JSON http_request event above
        pass


class DetectorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, ctx: ApiContext):
        super().__init__(address, DetectorHandler)
        self.ctx = ctx


def make_server(ctx: ApiContext, host: str = "0.0.0.0", port: int = 9100) -> DetectorServer:
    return DetectorServer((host, port), ctx)
