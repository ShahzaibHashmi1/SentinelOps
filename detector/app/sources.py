"""Data sources of the detector. It reads ONLY Prometheus and Loki over HTTP (decision D58).

Source (interface), Sample, SourceStatus and QueryFailed are used by the threshold detector and by
the unit tests (fake sources). HttpSource is the real client for both servers; they differ only in
the readiness path and the query path.

No-data and warm-up (decision D59), per cycle:
  * begin_cycle() asks the readiness path. Anything but HTTP 200 (a 503 from Loki included), a timeout
    or a refused connection gives the state "no_data".
  * After a source returns (and for the first warmup_s after the detector starts) it stays in
    "warming_up" for warmup_s: the detector turns its observations into no_data. Reasons: right after
    a restart a missing series must not read as a missing container, rate windows are not full yet and
    Loki windows can still contain old lines.
  * A query that fails because the server cannot be reached (timeout, refused) puts the whole source
    in "no_data" at once (and starts the warm-up again when it returns). A query that fails because
    the server answered with an error (HTTP 4xx/5xx, status "error", unexpected result type) makes only
    that rule no_data. An EMPTY result is a normal answer and is returned as an empty list.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

SOURCE_OK = "ok"
SOURCE_NO_DATA = "no_data"
SOURCE_WARMING_UP = "warming_up"

MAX_BODY_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class Sample:
    """One element of an instant-query vector: its labels and its numeric value."""
    labels: dict = field(default_factory=dict)
    value: float = 0.0


@dataclass(frozen=True)
class SourceStatus:
    state: str                              # "ok", "no_data" or "warming_up"
    since: Optional[datetime] = None
    detail: str = ""


class QueryFailed(Exception):
    """A query could not be answered (timeout, HTTP error, bad response).

    transport=True: the server could not be reached (timeout, refused connection).
    An EMPTY result is not a failure: it is a normal answer and the rule's on_missing decides.
    """

    def __init__(self, message: str, transport: bool = False):
        super().__init__(message)
        self.transport = transport


class Source(ABC):
    name: str = ""                          # "prometheus" or "loki"

    def begin_cycle(self, now_mono: float, now_utc: datetime) -> None:
        """Called once at the start of every evaluation cycle (readiness and warm-up). Default: nothing."""

    @abstractmethod
    def status(self) -> SourceStatus:
        """Cheap: answered from the state of the current cycle, no network call per rule."""

    @abstractmethod
    def query(self, expr: str) -> list:
        """Instant query -> list of Sample. Raises QueryFailed."""


# ------------------------------------------------------------------- transport

class TransportError(Exception):
    """The server could not be reached or did not answer in time."""


def urllib_transport(method: str, url: str, body: Optional[bytes], timeout: float):
    """(HTTP status, body bytes). An HTTP error status is returned, not raised; only a failure to
    talk to the server raises TransportError."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _read_limited(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _read_limited(exc)
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransportError(str(reason) or exc.__class__.__name__) from exc


def _read_limited(stream) -> bytes:
    data = stream.read(MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES:
        raise TransportError("response body too large")
    return data


# ---------------------------------------------------------------------- parsing

def parse_instant_result(payload) -> list:
    """The JSON answer of /api/v1/query (Prometheus) or /loki/api/v1/query -> list of Sample."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        message = payload.get("error") if isinstance(payload, dict) else None
        raise QueryFailed(f"query rejected: {_short(message or 'status is not success')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise QueryFailed("bad response: no data object")
    kind = data.get("resultType")
    result = data.get("result")
    try:
        if kind == "vector" and isinstance(result, list):
            return [Sample(dict(item["metric"]), float(item["value"][1])) for item in result]
        if kind == "scalar" and isinstance(result, list) and len(result) == 2:
            return [Sample({}, float(result[1]))]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise QueryFailed(f"bad response: {exc.__class__.__name__} in a sample") from exc
    raise QueryFailed(f"bad response: unexpected result type {kind!r}")


def _short(text, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ------------------------------------------------------------------- HTTP source

class HttpSource(Source):
    def __init__(self, name: str, base_url: str, ready_path: str, query_path: str, warmup_s: float,
                 timeout_s: float = HTTP_TIMEOUT_S,
                 transport: Callable = urllib_transport):
        self.name = name
        base = base_url.rstrip("/")
        self._ready_url = base + ready_path
        self._query_url = base + query_path
        self._warmup_s = float(warmup_s)
        self._timeout_s = float(timeout_s)
        self._transport = transport
        self._ready_before = False            # was the source ready in the previous cycle?
        self._warm_until: Optional[float] = None
        self._now_utc: Optional[datetime] = None
        self._state = SOURCE_NO_DATA
        self._since: Optional[datetime] = None
        self._detail = "not checked yet"
        self.query_errors = 0                 # total number of failed queries (metric)

    # ------------------------------------------------------------------ cycle

    def begin_cycle(self, now_mono: float, now_utc: datetime) -> None:
        self._now_utc = now_utc
        ready, why = self._check_ready()
        if not ready:
            self._go_down(why)
            return
        if not self._ready_before:            # first cycle after the start or after an outage
            self._warm_until = now_mono + self._warmup_s
        self._ready_before = True
        if self._warm_until is not None and now_mono < self._warm_until:
            self._set(SOURCE_WARMING_UP, f"warming up, {self._warm_until - now_mono:.0f} s left")
        else:
            self._set(SOURCE_OK, "")

    def _check_ready(self):
        try:
            status, body = self._transport("GET", self._ready_url, None, self._timeout_s)
        except TransportError as exc:
            return False, f"not reachable: {_short(exc)}"
        if status != 200:
            text = _short(body.decode("utf-8", "replace")) if body else ""
            return False, f"readiness answered HTTP {status}" + (f": {text}" if text else "")
        return True, ""

    def _go_down(self, detail: str) -> None:
        self._ready_before = False
        self._warm_until = None
        self._set(SOURCE_NO_DATA, detail)

    def _set(self, state: str, detail: str) -> None:
        if state != self._state:
            self._state = state
            self._since = self._now_utc
        self._detail = detail

    def status(self) -> SourceStatus:
        return SourceStatus(self._state, self._since, self._detail)

    # ------------------------------------------------------------------ query

    def query(self, expr: str) -> list:
        body = urllib.parse.urlencode({"query": expr}).encode("utf-8")
        try:
            status, raw = self._transport("POST", self._query_url, body, self._timeout_s)
        except TransportError as exc:
            self.query_errors += 1
            self._go_down(f"query failed, not reachable: {_short(exc)}")
            raise QueryFailed(f"{self.name} not reachable: {_short(exc)}", transport=True) from exc
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            payload = None
        if status != 200:
            self.query_errors += 1
            message = payload.get("error") if isinstance(payload, dict) else None
            raise QueryFailed(f"HTTP {status}: {_short(message or 'no error text')}")
        try:
            return parse_instant_result(payload)
        except QueryFailed:
            self.query_errors += 1
            raise


def prometheus_source(base_url: str, warmup_s: float, timeout_s: float = HTTP_TIMEOUT_S,
                      transport: Callable = urllib_transport) -> HttpSource:
    return HttpSource("prometheus", base_url, "/-/ready", "/api/v1/query", warmup_s, timeout_s, transport)


def loki_source(base_url: str, warmup_s: float, timeout_s: float = HTTP_TIMEOUT_S,
                transport: Callable = urllib_transport) -> HttpSource:
    return HttpSource("loki", base_url, "/ready", "/loki/api/v1/query", warmup_s, timeout_s, transport)
