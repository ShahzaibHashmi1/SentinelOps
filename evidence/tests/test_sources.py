import json
import unittest
from datetime import datetime, timezone

import _bootstrap  # noqa: F401
from fakes_io import FakeHttp, matrix_payload, ok, streams_payload, vector_payload

from app import sources as sc
from app.model import Point

NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)


def prom(http, warmup=30):
    return sc.PrometheusSource("http://prometheus:9090/", warmup, transport=http)


class WarmupTests(unittest.TestCase):
    def test_new_source_has_no_data_until_checked(self):
        self.assertEqual(prom(FakeHttp()).status().state, "no_data")

    def test_ready_then_warming_up_then_ok(self):
        http, src = FakeHttp(), None
        src = prom(http, warmup=30)
        src.begin_cycle(100.0, NOW)
        self.assertEqual(src.status().state, "warming_up")
        self.assertIn("30 s left", src.status().detail)
        src.begin_cycle(129.0, NOW)
        self.assertEqual(src.status().state, "warming_up")
        src.begin_cycle(130.0, NOW)
        self.assertEqual(src.status().state, "ok")
        self.assertTrue(src.is_ok())

    def test_since_changes_only_when_the_state_changes(self):
        src = prom(FakeHttp(), warmup=0)
        first = datetime(2026, 11, 3, 10, 0, 1, tzinfo=timezone.utc)
        src.begin_cycle(1.0, first)
        self.assertEqual(src.status().since, first)
        src.begin_cycle(6.0, datetime(2026, 11, 3, 10, 0, 6, tzinfo=timezone.utc))
        self.assertEqual(src.status().since, first)

    def test_not_ready_is_no_data_and_warmup_starts_again(self):
        http = FakeHttp()
        src = prom(http, warmup=10)
        src.begin_cycle(0.0, NOW)
        src.begin_cycle(10.0, NOW)
        self.assertEqual(src.status().state, "ok")
        http.ready_status = 503
        src.begin_cycle(15.0, NOW)
        self.assertEqual(src.status().state, "no_data")
        self.assertIn("HTTP 503", src.status().detail)
        http.ready_status = 200
        src.begin_cycle(20.0, NOW)
        self.assertEqual(src.status().state, "warming_up")
        src.begin_cycle(30.0, NOW)
        self.assertEqual(src.status().state, "ok")

    def test_unreachable_is_no_data(self):
        http = FakeHttp()
        http.fail = "connection refused"
        src = prom(http)
        src.begin_cycle(0.0, NOW)
        self.assertEqual(src.status().state, "no_data")
        self.assertIn("connection refused", src.status().detail)

    def test_detector_source_has_no_warmup(self):
        src = sc.DetectorSource("http://detector:9100", transport=FakeHttp())
        src.begin_cycle(0.0, NOW)
        self.assertEqual(src.status().state, "ok")

    def test_loki_503_is_no_data(self):
        http = FakeHttp()
        http.ready_status = 503
        loki = sc.LokiSource("http://loki:3100", 0, transport=http)
        loki.begin_cycle(0.0, NOW)
        self.assertEqual(loki.status().state, "no_data")


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.src = prom(self.http, warmup=0)
        self.src.begin_cycle(0.0, NOW)

    def route(self, payload, status=200):
        self.http.routes["/api/v1/query_range"] = lambda params, method: (status, json.dumps(payload).encode())

    def test_single_series(self):
        self.route(matrix_payload([({}, [(100, 1), (105, 2.5)])]))
        out = self.src.range_series("up", 100, 110, 5)
        self.assertEqual(out, {None: [Point(100.0, 1.0), Point(105.0, 2.5)]})

    def test_post_with_form_parameters(self):
        self.route(matrix_payload([]))
        self.src.range_series("sum(up)", 100.5, 200.25, 5)
        method, path, params = self.http.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/v1/query_range"))
        self.assertEqual(params, {"query": "sum(up)", "start": "100.500", "end": "200.250", "step": "5"})

    def test_empty_result_is_empty_not_an_error(self):
        self.route(matrix_payload([]))
        self.assertEqual(self.src.range_series("up", 0, 10, 5), {})
        self.assertEqual(self.src.query_errors, 0)

    def test_grouped_by_label(self):
        self.route(matrix_payload([({"dependency": "postgres"}, [(1, 0.1)]), ({"dependency": "redis"}, [(1, 0)])]))
        out = self.src.range_series("q", 0, 10, 5, by="dependency")
        self.assertEqual(sorted(out), ["postgres", "redis"])

    def test_nan_and_inf_are_dropped(self):
        self.route({"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {}, "values": [[1, "NaN"], [2, "+Inf"], [3, "4"], [4, "-Inf"]]}]}})
        self.assertEqual(self.src.range_series("q", 0, 10, 5), {None: [Point(3.0, 4.0)]})

    def test_more_than_one_series_without_by_is_a_failure(self):
        self.route(matrix_payload([({"a": "1"}, [(1, 1)]), ({"a": "2"}, [(1, 1)])]))
        with self.assertRaises(sc.QueryFailed):
            self.src.range_series("q", 0, 10, 5)

    def test_missing_or_duplicate_label(self):
        self.route(matrix_payload([({}, [(1, 1)])]))
        with self.assertRaises(sc.QueryFailed):
            self.src.range_series("q", 0, 10, 5, by="dependency")
        self.route(matrix_payload([({"dependency": "x"}, [(1, 1)]), ({"dependency": "x", "z": "1"}, [(1, 1)])]))
        with self.assertRaises(sc.QueryFailed):
            self.src.range_series("q", 0, 10, 5, by="dependency")

    def test_http_error_fails_only_the_call(self):
        self.route({"status": "error", "error": "bad query"}, status=400)
        with self.assertRaises(sc.QueryFailed) as ctx:
            self.src.range_series("q", 0, 10, 5)
        self.assertFalse(ctx.exception.transport)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertEqual(self.src.status().state, "ok")
        self.assertEqual(self.src.query_errors, 1)

    def test_status_error_in_a_200_answer(self):
        self.route({"status": "error", "error": "boom"})
        with self.assertRaises(sc.QueryFailed):
            self.src.range_series("q", 0, 10, 5)

    def test_wrong_result_type(self):
        self.route(vector_payload([({}, 1)]))
        with self.assertRaises(sc.QueryFailed):
            self.src.range_series("q", 0, 10, 5)

    def test_garbage_body(self):
        self.http.routes["/api/v1/query_range"] = lambda p, m: (200, b"<html>")
        with self.assertRaises(sc.QueryFailed):
            self.src.range_series("q", 0, 10, 5)

    def test_bad_sample_value(self):
        self.route({"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": [[1, "abc"]]}]}})
        with self.assertRaises(sc.QueryFailed):
            self.src.range_series("q", 0, 10, 5)

    def test_transport_failure_puts_the_whole_source_in_no_data(self):
        self.http.fail = "timed out"
        with self.assertRaises(sc.QueryFailed) as ctx:
            self.src.range_series("q", 0, 10, 5)
        self.assertTrue(ctx.exception.transport)
        self.assertEqual(self.src.status().state, "no_data")
        self.assertFalse(self.src.is_ok())


class LokiTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.loki = sc.LokiSource("http://loki:3100", 0, transport=self.http)
        self.loki.begin_cycle(0.0, NOW)

    def test_instant_vector(self):
        self.http.routes["/loki/api/v1/query"] = lambda p, m: ok(vector_payload([({}, 12), ({"event": "x"}, 3)]))
        self.assertEqual(self.loki.instant_vector("sum(count_over_time(x[1m]))", 100.0), [({}, 12.0), ({"event": "x"}, 3.0)])
        method, path, params = self.http.calls[-1]
        self.assertEqual((method, path, params["time"]), ("POST", "/loki/api/v1/query", "100.000"))

    def test_instant_vector_empty(self):
        self.http.routes["/loki/api/v1/query"] = lambda p, m: ok(vector_payload([]))
        self.assertEqual(self.loki.instant_vector("q", 1.0), [])

    def test_log_lines_newest_first_and_limited(self):
        self.http.routes["/loki/api/v1/query_range"] = lambda p, m: ok(streams_payload([(1_000, "a"), (3_000, "c"), (2_000, "b")]))
        out = self.loki.log_lines('{x="y"}', 1.0, 2.0, limit=2)
        self.assertEqual(out, [(3000, "c"), (2000, "b")])
        method, path, params = self.http.calls[-1]
        self.assertEqual(method, "GET")
        self.assertEqual((params["start"], params["end"], params["limit"], params["direction"]), ("1000000000", "2000000000", "2", "backward"))

    def test_log_lines_bad_answer(self):
        self.http.routes["/loki/api/v1/query_range"] = lambda p, m: ok(vector_payload([]))
        with self.assertRaises(sc.QueryFailed):
            self.loki.log_lines("q", 1.0, 2.0, 5)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.det = sc.DetectorSource("http://detector:9100", transport=self.http)
        self.det.begin_cycle(0.0, NOW)

    def test_incidents(self):
        self.http.routes["/internal/v1/incidents"] = lambda p, m: ok({"incidents": [{"incident_id": "INC-0001", "status": "open"}]})
        out = self.det.incidents(limit=10, status="open", since="2026-11-03T00:00:00Z")
        self.assertEqual(out[0]["incident_id"], "INC-0001")
        _m, _p, params = self.http.calls[-1]
        self.assertEqual(params, {"limit": "10", "status": "open", "since": "2026-11-03T00:00:00Z"})

    def test_bad_shapes(self):
        for payload in ({"incidents": "no"}, {"other": []}, [], {"incidents": [1]}):
            self.http.routes["/internal/v1/incidents"] = lambda p, m, payload=payload: ok(payload)
            with self.subTest(payload), self.assertRaises(sc.QueryFailed):
                self.det.incidents()

    def test_http_500(self):
        self.http.routes["/internal/v1/incidents"] = lambda p, m: (500, b'{"error": {"code": "internal_error"}}')
        with self.assertRaises(sc.QueryFailed):
            self.det.incidents()
        self.assertEqual(self.det.status().state, "ok")


class UrllibTransportTests(unittest.TestCase):
    def test_refused_connection_is_a_transport_error(self):
        with self.assertRaises(sc.TransportError):
            sc.urllib_transport("GET", "http://127.0.0.1:9/x", None, 0.5)

    def test_http_error_status_is_returned_and_body_is_read(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"no")

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = sc.urllib_transport("GET", f"http://127.0.0.1:{server.server_port}/", None, 2.0)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((status, body), (503, b"no"))


if __name__ == "__main__":
    unittest.main()
