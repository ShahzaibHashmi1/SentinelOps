import json
import unittest
import urllib.parse

import _bootstrap  # noqa: F401
from fakes import T0
from fakehttp import FakeBackend, FakeTransport, LocalServer, vector
from app.sources import (SOURCE_NO_DATA, SOURCE_OK, SOURCE_WARMING_UP, HttpSource, QueryFailed, TransportError,
                         loki_source, parse_instant_result, prometheus_source, urllib_transport)

BASE = "http://prometheus:9090"


def make(warmup=30.0, backend=None, name="prometheus"):
    backend = backend or FakeBackend()
    transport = FakeTransport(**{name: backend})
    factory = prometheus_source if name == "prometheus" else loki_source
    return factory(f"http://{name}:9090", warmup, transport=transport), backend


class ParseTests(unittest.TestCase):
    def test_vector_with_labels(self):
        samples = parse_instant_result(vector(({"service": "orders"}, 90.92), ({"service": "gateway"}, 40)))
        self.assertEqual([(s.labels, s.value) for s in samples],
                         [({"service": "orders"}, 90.92), ({"service": "gateway"}, 40.0)])

    def test_empty_vector_is_an_empty_list_not_an_error(self):
        self.assertEqual(parse_instant_result(vector()), [])

    def test_scalar(self):
        payload = {"status": "success", "data": {"resultType": "scalar", "result": [1760000000.0, "7.5"]}}
        self.assertEqual([(s.labels, s.value) for s in parse_instant_result(payload)], [({}, 7.5)])

    def test_nan_and_infinity_strings(self):
        samples = parse_instant_result(vector(({"a": "1"}, "NaN"), ({"a": "2"}, "+Inf"), ({"a": "3"}, "-Inf")))
        self.assertTrue(samples[0].value != samples[0].value)
        self.assertEqual((samples[1].value, samples[2].value), (float("inf"), float("-inf")))

    def test_bad_answers_raise_query_failed(self):
        for payload in (None, [], {"status": "error", "error": "parse error at char 5"},
                        {"status": "success"}, {"status": "success", "data": {"resultType": "matrix", "result": []}},
                        {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}}]}},
                        {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "x"]}]}}):
            with self.assertRaises(QueryFailed, msg=str(payload)):
                parse_instant_result(payload)

    def test_error_text_is_kept(self):
        with self.assertRaises(QueryFailed) as ctx:
            parse_instant_result({"status": "error", "error": "parse error at char 5"})
        self.assertIn("parse error at char 5", str(ctx.exception))


class ReadinessAndWarmupTests(unittest.TestCase):
    def test_first_cycle_after_the_start_is_warming_up_then_ok(self):
        src, backend = make(warmup=30)
        self.assertEqual(src.status().state, SOURCE_NO_DATA)          # before any cycle
        src.begin_cycle(100.0, T0)
        self.assertEqual(src.status().state, SOURCE_WARMING_UP)
        self.assertEqual(src.status().since, T0)
        src.begin_cycle(125.0, T0)
        self.assertEqual(src.status().state, SOURCE_WARMING_UP)
        self.assertEqual(src.status().since, T0)                       # since only moves on a state change
        src.begin_cycle(130.0, T0)                                     # exactly warmup_s after the first cycle
        self.assertEqual((src.status().state, src.status().detail), (SOURCE_OK, ""))

    def test_warmup_zero_is_ok_at_once(self):
        src, _ = make(warmup=0)
        src.begin_cycle(100.0, T0)
        self.assertEqual(src.status().state, SOURCE_OK)

    def test_not_ready_is_no_data_with_the_http_code(self):
        src, backend = make(name="loki")
        backend.ready_status = 503
        src.begin_cycle(100.0, T0)
        st = src.status()
        self.assertEqual(st.state, SOURCE_NO_DATA)
        self.assertIn("HTTP 503", st.detail)
        self.assertIn("Ingester not ready", st.detail)

    def test_unreachable_is_no_data(self):
        src, backend = make()
        backend.reachable = False
        src.begin_cycle(100.0, T0)
        self.assertEqual(src.status().state, SOURCE_NO_DATA)
        self.assertIn("not reachable", src.status().detail)

    def test_after_an_outage_the_warmup_starts_again(self):
        src, backend = make(warmup=30)
        for t in (100.0, 130.0):
            src.begin_cycle(t, T0)
        self.assertEqual(src.status().state, SOURCE_OK)
        backend.reachable = False
        src.begin_cycle(135.0, T0)
        self.assertEqual(src.status().state, SOURCE_NO_DATA)
        backend.reachable = True
        src.begin_cycle(140.0, T0)                                     # returned: warm-up of 30 s from here
        self.assertEqual(src.status().state, SOURCE_WARMING_UP)
        src.begin_cycle(169.0, T0)
        self.assertEqual(src.status().state, SOURCE_WARMING_UP)
        src.begin_cycle(170.0, T0)
        self.assertEqual(src.status().state, SOURCE_OK)

    def test_a_503_then_200_also_warms_up(self):
        src, backend = make(name="loki", warmup=30)
        backend.ready_status = 503
        src.begin_cycle(100.0, T0)
        backend.ready_status = 200
        src.begin_cycle(105.0, T0)
        self.assertEqual(src.status().state, SOURCE_WARMING_UP)

    def test_a_flapping_ready_never_reaches_ok(self):
        src, backend = make(warmup=30)
        for i in range(10):
            backend.ready_status = 200 if i % 2 == 0 else 503
            src.begin_cycle(100.0 + 5 * i, T0)
        self.assertNotEqual(src.status().state, SOURCE_OK)

    def test_paths(self):
        prom, pb = make()
        loki, lb = make(name="loki")
        prom.begin_cycle(0.0, T0)
        loki.begin_cycle(0.0, T0)
        prom.query("up")
        loki.query("x")
        self.assertEqual([c[1] for c in pb.calls], ["http://prometheus:9090/-/ready", "http://prometheus:9090/api/v1/query"])
        self.assertEqual([c[1] for c in lb.calls], ["http://loki:9090/ready", "http://loki:9090/loki/api/v1/query"])


class QueryTests(unittest.TestCase):
    def test_query_is_a_post_form(self):
        src, backend = make()
        backend.answers['sum by (le) (rate(x{a="b"}[1m]))'] = vector(({}, 1))
        samples = src.query('sum by (le) (rate(x{a="b"}[1m]))')
        method, url, body = backend.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(urllib.parse.parse_qs(body.decode()), {"query": ['sum by (le) (rate(x{a="b"}[1m]))']})
        self.assertEqual(samples[0].value, 1.0)

    def test_empty_result_is_not_an_error(self):
        src, backend = make()
        self.assertEqual(src.query("anything"), [])
        self.assertEqual(src.query_errors, 0)

    def test_http_error_fails_only_that_query_and_keeps_the_source_state(self):
        src, backend = make(warmup=0)
        src.begin_cycle(0.0, T0)
        backend.answers["bad"] = (400, {"status": "error", "errorType": "bad_data", "error": "parse error"})
        with self.assertRaises(QueryFailed) as ctx:
            src.query("bad")
        self.assertFalse(ctx.exception.transport)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("parse error", str(ctx.exception))
        self.assertEqual(src.status().state, SOURCE_OK)
        self.assertEqual(src.query_errors, 1)

    def test_bad_json_and_unexpected_result_count_as_errors(self):
        src, backend = make(warmup=0)
        backend.answers["m"] = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        with self.assertRaises(QueryFailed):
            src.query("m")
        self.assertEqual(src.query_errors, 1)

    def test_transport_failure_puts_the_whole_source_in_no_data_and_warms_up_again(self):
        src, backend = make(warmup=30)
        src.begin_cycle(0.0, T0)
        src.begin_cycle(30.0, T0)
        self.assertEqual(src.status().state, SOURCE_OK)
        backend.reachable = False
        with self.assertRaises(QueryFailed) as ctx:
            src.query("up")
        self.assertTrue(ctx.exception.transport)
        self.assertEqual(src.status().state, SOURCE_NO_DATA)
        backend.reachable = True
        src.begin_cycle(35.0, T0)
        self.assertEqual(src.status().state, SOURCE_WARMING_UP)
        self.assertEqual(src.query_errors, 1)


class RealHttpTests(unittest.TestCase):
    """urllib_transport against a real server on 127.0.0.1."""

    def test_end_to_end_with_a_local_server(self):
        backend = FakeBackend()
        backend.answers["up"] = vector(({"job": "orders"}, 1))
        with LocalServer(backend) as server:
            src = prometheus_source(server.url, 0)
            src.begin_cycle(0.0, T0)
            self.assertEqual(src.status().state, SOURCE_OK)
            samples = src.query("up")
            self.assertEqual([(s.labels, s.value) for s in samples], [({"job": "orders"}, 1.0)])
            method, path, content_type, body = server.httpd.seen[-1]
            self.assertEqual((method, path, content_type), ("POST", "/api/v1/query", "application/x-www-form-urlencoded"))
            self.assertEqual(body, b"query=up")

    def test_http_503_is_returned_not_raised(self):
        backend = FakeBackend()
        backend.ready_status = 503
        with LocalServer(backend) as server:
            status, body = urllib_transport("GET", server.url + "/ready", None, 2.0)
            self.assertEqual((status, body), (503, b"Ingester not ready"))

    def test_http_error_answer_of_a_query(self):
        backend = FakeBackend()
        backend.answers["bad"] = (422, {"status": "error", "error": "expression cannot be evaluated"})
        with LocalServer(backend) as server:
            src = prometheus_source(server.url, 0)
            with self.assertRaises(QueryFailed) as ctx:
                src.query("bad")
            self.assertIn("HTTP 422", str(ctx.exception))

    def test_connection_refused_is_a_transport_error(self):
        with LocalServer(FakeBackend()) as server:
            url = server.url
        with self.assertRaises(TransportError):
            urllib_transport("GET", url + "/ready", None, 1.0)

    def test_timeout_is_a_transport_error(self):
        backend = FakeBackend()
        backend.delay_s = 1.0
        with LocalServer(backend) as server:
            with self.assertRaises(TransportError):
                urllib_transport("GET", server.url + "/ready", None, 0.2)

    def test_a_down_server_makes_begin_cycle_no_data(self):
        with LocalServer(FakeBackend()) as server:
            url = server.url
        src = prometheus_source(url, 0, timeout_s=1.0)
        src.begin_cycle(0.0, T0)
        self.assertEqual(src.status().state, SOURCE_NO_DATA)


if __name__ == "__main__":
    unittest.main()
