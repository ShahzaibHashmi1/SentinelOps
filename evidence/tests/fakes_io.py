"""Fake transports and builders for the I/O side (sources, collector, loop, API). No sockets."""
import json
import urllib.parse
from datetime import timedelta

import _bootstrap  # noqa: F401
from fakes import T0

from app.sources import TransportError


def ok(payload):
    return 200, json.dumps(payload).encode("utf-8")


def matrix_payload(series):
    """series: list of (labels dict, [(ts, value), ...])."""
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": labels, "values": [[ts, str(v)] for ts, v in values]} for labels, values in series]}}


def vector_payload(rows):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": labels, "value": [1700000000, str(v)]} for labels, v in rows]}}


def streams_payload(lines):
    """lines: list of (ts_ns, line)."""
    return {"status": "success", "data": {"resultType": "streams", "result": [
        {"stream": {"service": "x"}, "values": [[str(ts), line] for ts, line in lines]}]}}


class FakeHttp:
    """Transport(method, url, body, timeout). `routes` maps a path to a function(params, method) -> (status, bytes)."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.fail = None  # a TransportError message makes every call fail
        self.ready_status = 200

    def __call__(self, method, url, body, timeout):
        parts = urllib.parse.urlsplit(url)
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
        if body:
            params.update({k: v[-1] for k, v in urllib.parse.parse_qs(body.decode("utf-8")).items()})
        self.calls.append((method, parts.path, params))
        if self.fail:
            raise TransportError(self.fail)
        if parts.path in ("/-/ready", "/ready"):
            return self.ready_status, b"ready" if self.ready_status == 200 else b"not ready"
        handler = self.routes.get(parts.path)
        if handler is None:
            return 404, b"not found"
        return handler(params, method)

    def count(self, path):
        return sum(1 for _m, p, _q in self.calls if p == path)


def ts(seconds):
    return (T0 + timedelta(seconds=seconds)).timestamp()
