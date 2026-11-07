"""HTTP data sources of the evidence builder: Prometheus, Loki and the detector API.

All three use one HttpSource base (readiness check, warm-up, state, error handling). The transport is injected,
so the unit tests never open a socket.

No-data and warm-up (same rules as the detector, D59 and D62):
  * begin_cycle() asks the readiness path. Anything but HTTP 200 (a Loki 503 included), a timeout or a refused
    connection gives the state "no_data".
  * After a source returns, and for the first warmup_s after the start, it is "warming_up": the collector gives
    no_data for its signals (right after a restart a missing series must not read as a missing container).
  * A call that fails because the server cannot be reached puts the whole source in "no_data" at once (and the
    warm-up starts again when it returns). A call that fails because the server answered with an error (HTTP
    4xx or 5xx, status "error", unexpected result type) fails only that call.
  * An EMPTY result is a normal answer. It is returned as an empty result, never as an error.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .model import Point

SOURCE_OK = "ok"
SOURCE_NO_DATA = "no_data"
SOURCE_WARMING_UP = "warming_up"
MAX_BODY_BYTES = 8 * 1024 * 1024
HTTP_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class SourceStatus:
    state: str
    since: datetime | None = None
    detail: str = ""


class QueryFailed(Exception):
    """A call could not be answered. transport=True: the server could not be reached."""

    def __init__(self, message: str, transport: bool = False):
        super().__init__(message)
        self.transport = transport


class TransportError(Exception):
    """The server could not be reached or did not answer in time."""


def _short(text, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ------------------------------------------------------------------- transport

def _read_limited(stream) -> bytes:
    data = stream.read(MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES:
        raise TransportError("response body too large")
    return data


def urllib_transport(method: str, url: str, body: bytes | None, timeout: float):
    """(HTTP status, body bytes). An HTTP error status is returned, not raised; only a failure to talk to
    the server raises TransportError."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    call = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return response.status, _read_limited(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _read_limited(exc)
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransportError(str(reason) or exc.__class__.__name__) from exc


# ------------------------------------------------------------------- parsing

def _check_success(payload, expected_type: str) -> list:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        message = payload.get("error") if isinstance(payload, dict) else None
        raise QueryFailed(f"query rejected: {_short(message or 'status is not success')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise QueryFailed("bad response: no data object")
    if data.get("resultType") != expected_type or not isinstance(data.get("result"), list):
        raise QueryFailed(f"bad response: unexpected result type {data.get('resultType')!r}")
    return data["result"]


def _finite(text) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise QueryFailed("bad response: a sample value is not a number") from None
    return value if math.isfinite(value) else None


def parse_matrix(payload, by: str | None = None) -> dict:
    """Range-query answer -> {label value (or None): [Point, ...]}. Non-finite values (NaN of a quantile
    without traffic) are dropped: they are missing steps, not values. An empty result gives {}."""
    result = _check_success(payload, "matrix")
    out: dict = {}
    if by is None and len(result) > 1:
        raise QueryFailed(f"expected one series, the query returned {len(result)}")
    try:
        for item in result:
            if by is None:
                key = None
            else:
                key = item["metric"].get(by)
                if key is None:
                    raise QueryFailed(f"a series has no label {by!r}")
            if key in out:
                raise QueryFailed(f"two series with the same {by!r} label")
            points = []
            for ts, raw in item["values"]:
                value = _finite(raw)
                if value is not None:
                    points.append(Point(float(ts), value))
            out[key] = points
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise QueryFailed(f"bad response: {exc.__class__.__name__} in a series") from exc
    return out


def parse_vector(payload) -> list:
    """Instant-query answer -> [(labels dict, value)]. Non-finite values are dropped."""
    result = _check_success(payload, "vector")
    out = []
    try:
        for item in result:
            value = _finite(item["value"][1])
            if value is not None:
                out.append((dict(item["metric"]), value))
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise QueryFailed(f"bad response: {exc.__class__.__name__} in a sample") from exc
    return out


def parse_streams(payload) -> list:
    """Loki query_range (streams) -> [(timestamp in ns, line)], newest first."""
    result = _check_success(payload, "streams")
    lines = []
    try:
        for stream in result:
            for ts, line in stream["values"]:
                lines.append((int(ts), str(line)))
    except (KeyError, TypeError, ValueError) as exc:
        raise QueryFailed(f"bad response: {exc.__class__.__name__} in a stream") from exc
    lines.sort(key=lambda item: item[0], reverse=True)
    return lines


# ------------------------------------------------------------------- HTTP source

class HttpSource:
    def __init__(self, name: str, base_url: str, ready_path: str, warmup_s: float,
                 timeout_s: float = HTTP_TIMEOUT_S, transport: Callable = urllib_transport):
        self.name = name
        self._base = base_url.rstrip("/")
        self._ready_url = self._base + ready_path
        self._warmup_s = float(warmup_s)
        self._timeout_s = float(timeout_s)
        self._transport = transport
        self._ready_before = False
        self._warm_until: float | None = None
        self._now_utc: datetime | None = None
        self._state = SOURCE_NO_DATA
        self._since: datetime | None = None
        self._detail = "not checked yet"
        self.query_errors = 0

    # -- cycle
    def begin_cycle(self, now_mono: float, now_utc: datetime) -> None:
        self._now_utc = now_utc
        ready, why = self._check_ready()
        if not ready:
            self._go_down(why)
            return
        if not self._ready_before:
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

    def is_ok(self) -> bool:
        return self._state == SOURCE_OK

    # -- calls
    def _call(self, method: str, path: str, params: dict) -> object:
        """JSON answer of one call. Raises QueryFailed (transport=True when the server was not reachable)."""
        encoded = urllib.parse.urlencode(params)
        if method == "POST":
            url, body = self._base + path, encoded.encode("utf-8")
        else:
            url, body = self._base + path + ("?" + encoded if encoded else ""), None
        try:
            status, raw = self._transport(method, url, body, self._timeout_s)
        except TransportError as exc:
            self.query_errors += 1
            self._go_down(f"call failed, not reachable: {_short(exc)}")
            raise QueryFailed(f"{self.name} not reachable: {_short(exc)}", transport=True) from exc
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            payload = None
        if status != 200:
            self.query_errors += 1
            message = payload.get("error") if isinstance(payload, dict) else None
            raise QueryFailed(f"HTTP {status}: {_short(message if isinstance(message, str) else 'no error text')}")
        return payload


class PrometheusSource(HttpSource):
    def __init__(self, base_url: str, warmup_s: float, timeout_s: float = HTTP_TIMEOUT_S, transport: Callable = urllib_transport):
        super().__init__("prometheus", base_url, "/-/ready", warmup_s, timeout_s, transport)

    def range_series(self, expr: str, start: float, end: float, step: float, by: str | None = None) -> dict:
        """{label value (None without `by`): [Point, ...]}; an empty result is {}."""
        payload = self._call("POST", "/api/v1/query_range", {"query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step:g}"})
        try:
            return parse_matrix(payload, by)
        except QueryFailed:
            self.query_errors += 1
            raise


class LokiSource(HttpSource):
    def __init__(self, base_url: str, warmup_s: float, timeout_s: float = HTTP_TIMEOUT_S, transport: Callable = urllib_transport):
        super().__init__("loki", base_url, "/ready", warmup_s, timeout_s, transport)

    def instant_vector(self, expr: str, at: float) -> list:
        """[(labels, value)] of an instant query evaluated at `at` (epoch seconds)."""
        payload = self._call("POST", "/loki/api/v1/query", {"query": expr, "time": f"{at:.3f}"})
        try:
            return parse_vector(payload)
        except QueryFailed:
            self.query_errors += 1
            raise

    def log_lines(self, expr: str, start: float, end: float, limit: int) -> list:
        """[(timestamp ns, line)], newest first, at most `limit` lines."""
        payload = self._call("GET", "/loki/api/v1/query_range", {
            "query": expr, "start": str(int(start * 1e9)), "end": str(int(end * 1e9)), "limit": str(int(limit)), "direction": "backward"})
        try:
            return parse_streams(payload)[: int(limit)]
        except QueryFailed:
            self.query_errors += 1
            raise


class DetectorSource(HttpSource):
    def __init__(self, base_url: str, timeout_s: float = HTTP_TIMEOUT_S, transport: Callable = urllib_transport):
        super().__init__("detector", base_url, "/ready", 0.0, timeout_s, transport)

    def incidents(self, status: str | None = None, limit: int = 50, since: str | None = None) -> list:
        """Incident objects of GET /internal/v1/incidents, newest first (docs/DETECTOR.md section 9.1)."""
        params: dict = {"limit": str(int(limit))}
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        payload = self._call("GET", "/internal/v1/incidents", params)
        items = payload.get("incidents") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            self.query_errors += 1
            raise QueryFailed("bad response: no incidents list")
        return items
