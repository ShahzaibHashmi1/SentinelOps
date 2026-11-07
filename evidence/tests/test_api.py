import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import _bootstrap  # noqa: F401
import test_store
from fakes import GRAPH, SIGNALS, TOPOLOGY, at, change

from app import api
from app.logging_setup import setup_logging
from app.loop import LoopState
from app.metrics import Metrics
from app.sources import SourceStatus
from app.store import Store, StoreReader


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "e.db")
        self.store = Store(self.path)
        self.state = LoopState()
        self.mono = [1000.0]
        self.stream = io.StringIO()
        self.ctx = api.ApiContext(reader=StoreReader(self.path), graph=GRAPH, topology=TOPOLOGY, state=self.state, metrics=Metrics(SIGNALS.sources),
                                  settings={"interval_s": 5.0}, version="9.9.9", interval_s=5.0, logger=setup_logging("DEBUG", "t", self.stream),
                                  mono=lambda: self.mono[0])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put(self, **kw):
        return self.store.insert_bundle(test_store.make_bundle(self.store, **kw))

    def get(self, target, method="GET"):
        status, ctype, body, headers = api.dispatch(self.ctx, method, target)
        return status, ctype, body, headers

    def get_json(self, target):
        status, ctype, body, headers = self.get(target)
        self.assertEqual(ctype, api.JSON_TYPE if not target.startswith("/metrics") else api.METRICS_TYPE)
        return status, json.loads(body.decode("utf-8"))

    def finish_cycle(self, mono=1000.0):
        self.state.update({"prometheus": SourceStatus("ok", at(1), ""), "docker": SourceStatus("off", None, "disabled")}, at(5), mono, 0.25, 1)


class BasicRouteTests(ApiCase):
    def test_health(self):
        self.assertEqual(self.get_json("/health"), (200, {"status": "ok", "service": "evidence", "version": "9.9.9"}))

    def test_ready_before_and_after_a_cycle_and_when_stale(self):
        status, body = self.get_json("/ready")
        self.assertEqual((status, body["error"]["code"]), (503, "not_ready"))
        self.finish_cycle(1000.0)
        self.mono[0] = 1014.9
        self.assertEqual(self.get_json("/ready")[0], 200)
        self.mono[0] = 1015.1  # more than 3 intervals
        status, body = self.get_json("/ready")
        self.assertEqual(status, 503)
        self.assertIn("limit 15 s", body["error"]["message"])

    def test_a_source_in_no_data_does_not_make_the_service_not_ready(self):
        self.state.update({"prometheus": SourceStatus("no_data", None, "down")}, at(5), 1000.0, 0.1, 0)
        self.mono[0] = 1001.0
        self.assertEqual(self.get_json("/ready")[0], 200)

    def test_metrics(self):
        status, ctype, body, _ = self.get("/metrics")
        self.assertEqual((status, ctype), (200, api.METRICS_TYPE))
        self.assertIn(b"evidence_build_cycles_total", body)

    def test_status(self):
        self.put(trigger="opened")
        self.finish_cycle()
        status, body = self.get_json("/internal/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["sources"]["docker"], {"state": "off", "since": None, "detail": "disabled"})
        self.assertEqual(body["sources"]["prometheus"]["since"], fmt(at(1)))
        self.assertEqual((body["bundles_total"], body["bundles_by_trigger"]), (1, {"opened": 1, "refresh": 0, "resolved": 0}))
        self.assertEqual((body["cycles"], body["cycle_duration_ms"], body["open_incidents_tracked"]), (1, 250.0, 1))
        self.assertEqual(body["settings"], {"interval_s": 5.0})
        self.assertEqual(body["version"], "9.9.9")

    def test_status_before_any_cycle(self):
        status, body = self.get_json("/internal/v1/status")
        self.assertEqual((status, body["cycles"], body["last_cycle_at"], body["sources"]), (200, 0, None, {}))

    def test_graph(self):
        status, body = self.get_json("/internal/v1/graph")
        self.assertEqual(status, 200)
        self.assertEqual(body["nodes"], GRAPH.nodes())
        self.assertEqual(body["edges"], GRAPH.edges())
        self.assertEqual(body["depth"]["gateway"], 3)


def fmt(dt):
    from app.model import fmt_ts
    return fmt_ts(dt)


class EvidenceRouteTests(ApiCase):
    def test_latest_bundle_of_an_incident(self):
        self.put(trigger="opened")
        self.put()
        status, body = self.get_json("/internal/v1/evidence/INC-0001")
        self.assertEqual((status, body["bundle_id"], body["trigger"]), (200, "EVD-00002", "refresh"))

    def test_no_bundle_yet_is_404(self):
        status, body = self.get_json("/internal/v1/evidence/INC-0007")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))

    def test_bundle_list(self):
        self.put(trigger="opened")
        self.put(trigger="resolved")
        status, body = self.get_json("/internal/v1/evidence/INC-0001/bundles")
        self.assertEqual(status, 200)
        self.assertEqual(body["incident_id"], "INC-0001")
        self.assertEqual([(b["bundle_id"], b["trigger"]) for b in body["bundles"]], [("EVD-00001", "opened"), ("EVD-00002", "resolved")])

    def test_empty_bundle_list_is_200(self):
        self.assertEqual(self.get_json("/internal/v1/evidence/INC-0007/bundles"), (200, {"incident_id": "INC-0007", "bundles": []}))

    def test_one_bundle_by_id_is_exactly_the_stored_bundle(self):
        bundle = test_store.make_bundle(self.store, trigger="opened")
        self.store.insert_bundle(bundle)
        status, body = self.get_json("/internal/v1/bundles/EVD-00001")
        self.assertEqual((status, body), (200, bundle))

    def test_unknown_bundle_and_odd_ids_are_404(self):
        for target in ("/internal/v1/bundles/EVD-99999", "/internal/v1/bundles/x", "/internal/v1/bundles/EVD-1; DROP", "/internal/v1/bundles/",
                       "/internal/v1/evidence/", "/internal/v1/evidence/not-an-id", "/internal/v1/evidence/INC-1/other", "/internal/v1/evidence/INC-1/bundles/x",
                       "/internal/v1/evidence/..%2f..%2fetc", "/nope", "/internal/v1", "/internal/v1/status/x"):
            with self.subTest(target):
                self.assertEqual(self.get(target)[0], 404)

    def test_store_unavailable_is_503(self):
        self.ctx.reader = StoreReader(os.path.join(self.tmp.name, "gone.db"))
        for target in ("/internal/v1/status", "/internal/v1/evidence/INC-0001", "/internal/v1/bundles/EVD-00001", "/internal/v1/changes"):
            with self.subTest(target):
                status, body = self.get_json(target)
                self.assertEqual((status, body["error"]["code"]), (503, "store_unavailable"))


class ChangesRouteTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.store.add_changes([change("orders", "recreated", 10), change("orders", "restarted", 30), change("payments", "stopped", 40)])

    def test_default_lists_newest_first(self):
        status, body = self.get_json("/internal/v1/changes")
        self.assertEqual(status, 200)
        self.assertEqual([c["kind"] for c in body["changes"]], ["stopped", "restarted", "recreated"])
        self.assertEqual(set(body["changes"][0]), {"service", "kind", "ts", "old", "new"})

    def test_filters(self):
        self.assertEqual(len(self.get_json("/internal/v1/changes?service=orders")[1]["changes"]), 2)
        self.assertEqual(len(self.get_json("/internal/v1/changes?limit=1")[1]["changes"]), 1)
        self.assertEqual(len(self.get_json("/internal/v1/changes?since=" + fmt(at(25)).replace(":", "%3A"))[1]["changes"]), 2)

    def test_bad_parameters_are_400(self):
        for query in ("service=nope", "limit=0", "limit=201", "limit=abc", "limit=", "since=yesterday", "since="):
            with self.subTest(query):
                status, body = self.get_json("/internal/v1/changes?" + query)
                self.assertEqual((status, body["error"]["code"]), (400, "bad_request"))

    def test_limit_bounds_are_accepted(self):
        self.assertEqual(self.get_json("/internal/v1/changes?limit=1")[0], 200)
        self.assertEqual(self.get_json("/internal/v1/changes?limit=200")[0], 200)


class MethodTests(ApiCase):
    def test_other_methods_are_405_with_allow_get_on_every_known_path(self):
        self.put(trigger="opened")
        for target in ("/health", "/ready", "/metrics", "/internal/v1/status", "/internal/v1/graph", "/internal/v1/changes",
                       "/internal/v1/evidence/INC-0001", "/internal/v1/evidence/INC-0001/bundles", "/internal/v1/bundles/EVD-00001"):
            for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                with self.subTest(target=target, method=method):
                    status, _t, body, headers = self.get(target, method)
                    self.assertEqual((status, headers.get("Allow")), (405, "GET"))
                    self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")

    def test_unknown_path_is_404_for_any_method(self):
        self.assertEqual(self.get("/nope", "POST")[0], 404)

    def test_an_unexpected_error_is_a_500_without_a_stack_trace(self):
        self.ctx.metrics = None
        status, _t, body, _h = self.get("/metrics")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "internal_error")
        self.assertNotIn("Traceback", body.decode())
        self.assertIn("api_error", self.stream.getvalue())


class ServerTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.put(trigger="opened")
        self.finish_cycle(1000.0)
        self.server = api.make_server(self.ctx, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def call(self, path, method="GET", data=None):
        call = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=data)
        try:
            with urllib.request.urlopen(call, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, dict(exc.headers), exc.read()
            finally:
                exc.close()

    def test_get_over_http(self):
        status, headers, body = self.call("/internal/v1/evidence/INC-0001")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], api.JSON_TYPE)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(json.loads(body)["bundle_id"], "EVD-00001")
        self.assertNotIn("Python", headers.get("Server", ""))

    def test_post_with_a_body_gets_405_not_a_reset(self):
        status, headers, body = self.call("/internal/v1/status", "POST", data=b"x" * 5000)
        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_metrics_and_health_over_http(self):
        self.assertEqual(self.call("/health")[0], 200)
        status, headers, body = self.call("/metrics")
        self.assertEqual((status, headers["Content-Type"]), (200, api.METRICS_TYPE))

    def test_health_checks_are_not_logged_at_info(self):
        self.stream.truncate(0)
        self.stream.seek(0)
        self.ctx.logger.setLevel("INFO")
        self.call("/health")
        self.call("/internal/v1/graph")
        events = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        paths = [e["path"] for e in events if e["event"] == "http_request"]
        self.assertEqual(paths, ["/internal/v1/graph"])

    def test_head_is_405_without_a_body(self):
        status, headers, body = self.call("/health", "HEAD")
        self.assertEqual((status, body), (405, b""))


if __name__ == "__main__":
    unittest.main()
