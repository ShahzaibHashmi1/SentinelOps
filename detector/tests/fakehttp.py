"""A fake transport and a fake Prometheus/Loki HTTP server for the tests (localhost only, no Docker)."""
import _bootstrap  # noqa: F401
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.sources import TransportError


def vector(*items):
    """vector(({"service": "orders"}, 90.9), ...) -> the JSON answer of an instant query"""
    return {"status": "success", "data": {"resultType": "vector",
            "result": [{"metric": m, "value": [1760000000.0, repr(float(v)) if not isinstance(v, str) else v]}
                       for m, v in items]}}


class FakeBackend:
    """Behaviour of one server: ready code, answers per expression, errors, recorded calls."""

    def __init__(self):
        self.ready_status = 200
        self.reachable = True
        self.answers = {}          # expr -> json payload (dict) or (http status, json payload)
        self.calls = []            # (method, url, body bytes)
        self.delay_s = 0.0

    def handle(self, method, url, body):
        self.calls.append((method, url, body))
        if not self.reachable:
            raise TransportError("Connection refused")
        if url.endswith("/-/ready") or url.endswith("/ready"):
            return self.ready_status, b"ready" if self.ready_status == 200 else b"Ingester not ready"
        form = urllib.parse.parse_qs(body.decode("utf-8")) if body else {}
        expr = form.get("query", [""])[0]
        answer = self.answers.get(expr, vector())            # unknown expression: an EMPTY result
        status = 200
        if isinstance(answer, tuple):
            status, answer = answer
        return status, json.dumps(answer).encode("utf-8")


class FakeTransport:
    """Callable with the signature of urllib_transport; routes by host name to a FakeBackend."""

    def __init__(self, **backends):
        self.backends = backends          # {"prometheus": FakeBackend, "loki": FakeBackend}

    def __call__(self, method, url, body, timeout):
        host = urllib.parse.urlsplit(url).hostname
        return self.backends[host].handle(method, url, body)


class LocalServer:
    """A real HTTP server on 127.0.0.1 (port chosen by the OS) driven by a FakeBackend."""

    def __init__(self, backend):
        outer = backend

        class Handler(BaseHTTPRequestHandler):
            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if outer.delay_s:
                    time.sleep(outer.delay_s)
                self.server.seen.append((self.command, self.path, self.headers.get("Content-Type"), body))
                status, payload = outer.handle(self.command, self.path, body)
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                try:
                    self.end_headers()
                    self.wfile.write(payload)
                except ConnectionError:
                    pass   # the client gave up (timeout tests do this on purpose); not an error of the fake server
            do_GET = do_POST = _do

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.seen = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
