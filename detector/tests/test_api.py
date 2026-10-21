import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import _bootstrap  # noqa: F401
from _bootstrap import RULES_JSON
from fakes import T0, Driver
from app.api import ApiContext, dispatch, make_server
from app.logging_setup import configure
from app.loop import LoopSnapshot, LoopState
from app.metrics import Metrics, MetricsSnapshot
from app.model import FIRING, OK, NO_DATA, Observation, RuleMeta
from app.rules import load_rules
from app.sources import SourceStatus
from app.store import Store

METAS = [RuleMeta("A", "alpha", "high", "root_signal", True, 10), RuleMeta("B", "bravo", "medium", "effect", True, 10)]
INCIDENT_FIELDS = ["incident_id", "schema_version", "status", "opened_at", "updated_at", "resolved_at", "reopen_count",
                   "severity", "title", "primary_service", "candidate_services", "affected_services", "symptoms",
                   "data_gaps"]


def make_incident(rounds=1):
    """`rounds` incidents (resolved, last one open), built by the real correlator."""
    d = Driver(METAS, reopen_within_s=1)
    incidents = []
    for i in range(rounds):
        for _ in range(3):
            d.step(d.obs("A", "postgres", "firing", value=0.0, comparator="==", threshold=0.0),
                   d.obs("B", "gateway", "firing", value=9.0, threshold=5.0))
        incidents.append(d.incident)
        if i < rounds - 1:
            for _ in range(16):
                d.step(d.obs("A", "postgres", "ok", value=1.0), d.obs("B", "gateway", "ok", value=0.0))
    return incidents


class ApiFixture:
    def __init__(self, tmp, incidents=(), cycle_done=True):
        self.db = str(Path(tmp) / "d.db")
        self.store = Store(self.db)
        if incidents:
            self.store.save(list(incidents))
        self.rules = load_rules(RULES_JSON)
        self.state = LoopState()
        self.metrics = Metrics()
        self.clock = {"mono": 1000.0}
        self.logger = configure("DEBUG", "test", io.StringIO())
        self.ctx = ApiContext(db_path=self.db, rules=self.rules, state=self.state, metrics=self.metrics,
                              settings={"eval_interval_s": 5.0}, version="9.9.9", interval_s=5.0, logger=self.logger,
                              mono=lambda: self.clock["mono"])
        if cycle_done:
            self.finish_cycle()
        self.server = make_server(self.ctx, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def finish_cycle(self, observations=()):
        self.state.publish(LoopSnapshot(
            cycles=3, last_cycle_at=T0, last_cycle_mono=self.clock["mono"], duration_s=0.0123,
            sources={"prometheus": SourceStatus("ok", T0, ""), "loki": SourceStatus("warming_up", T0, "warming up, 12 s left")},
            observations=list(observations)))

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, path):
        status, headers, body = self.get(path)
        return status, headers, json.loads(body.decode("utf-8"))

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=b"x" if method in ("POST", "PUT", "PATCH") else None)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), body

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)


class ApiTestCase(unittest.TestCase):
    incidents = ()
    cycle_done = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = ApiFixture(self.tmp.name, self.incidents() if callable(self.incidents) else self.incidents, self.cycle_done)

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()


class HealthReadyMetricsTests(ApiTestCase):
    def test_health(self):
        status, headers, body = self.fx.json("/health")
        self.assertEqual((status, body), (200, {"status": "ok", "service": "detector", "version": "9.9.9"}))
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_ready_after_a_cycle(self):
        status, _, body = self.fx.json("/ready")
        self.assertEqual((status, body["status"]), (200, "ready"))

    def test_ready_is_503_when_the_last_cycle_is_older_than_three_intervals(self):
        self.fx.clock["mono"] += 15.0
        self.assertEqual(self.fx.get("/ready")[0], 200)              # exactly 3 x 5 s
        self.fx.clock["mono"] += 0.1
        status, _, body = self.fx.json("/ready")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "not_ready")
        self.assertIn("15", body["error"]["message"])

    def test_a_source_in_no_data_does_not_make_the_detector_not_ready(self):
        self.fx.state.publish(LoopSnapshot(cycles=1, last_cycle_mono=self.fx.clock["mono"],
                                           sources={"prometheus": SourceStatus("no_data", T0, "unreachable")}))
        self.assertEqual(self.fx.get("/ready")[0], 200)

    def test_metrics_format_and_content_type(self):
        self.fx.metrics.publish(MetricsSnapshot(cycles=3, source_ok={"prometheus": True},
                                                rule_states={("R01", "gateway"): "ok"}, rule_values={("R01", "gateway"): 1.0}))
        status, headers, body = self.fx.get("/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertIn(b'detector_rule_value{rule_id="R01",subject="gateway"} 1.0', body)
        self.assertIn(b"detector_evaluation_cycles_total 3", body)


class NotReadyYetTests(ApiTestCase):
    cycle_done = False

    def test_ready_is_503_before_the_first_cycle_but_health_is_200(self):
        self.assertEqual(self.fx.get("/health")[0], 200)
        status, _, body = self.fx.json("/ready")
        self.assertEqual((status, body["error"]["code"]), (503, "not_ready"))

    def test_status_before_the_first_cycle_shows_every_rule_as_no_data(self):
        status, _, body = self.fx.json("/internal/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual({r["state"] for r in body["rules"]}, {"no_data"})
        self.assertEqual(body["cycles"], 0 if body["last_cycle_at"] is None else body["cycles"])


class StatusTests(ApiTestCase):
    def incidents(self):
        return make_incident()

    def test_status_content(self):
        obs = [Observation("R03", "threshold", "prometheus", "orders", FIRING, 90.92, 85.0, ">", T0, ""),
               Observation("R03", "threshold", "prometheus", "gateway", OK, 40.0, 85.0, ">", T0, ""),
               Observation("R02", "threshold", "prometheus", "redis", NO_DATA, None, 0.0, "==", T0, "guard not satisfied"),
               Observation("R01", "threshold", "prometheus", "gateway", OK, 1.0, 0.0, "==", T0, "")]
        self.fx.finish_cycle(obs)
        status, _, body = self.fx.json("/internal/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(list(body)[:4], ["service", "version", "sources", "last_cycle_at"])
        self.assertEqual(body["version"], "9.9.9")
        self.assertEqual(body["sources"]["prometheus"], {"state": "ok", "since": "2026-10-18T12:00:00.000Z", "detail": ""})
        self.assertEqual(body["sources"]["loki"]["state"], "warming_up")
        self.assertEqual((body["last_cycle_at"], body["cycle_duration_ms"], body["cycles"]),
                         ("2026-10-18T12:00:00.000Z", 12.3, 3))
        self.assertEqual(body["rules_loaded"], 9)
        self.assertEqual(body["open_incident_id"], "INC-0001")
        self.assertEqual(body["settings"], {"eval_interval_s": 5.0})
        rules = {r["rule_id"]: r for r in body["rules"]}
        self.assertEqual(list(rules), [f"R0{i}" for i in range(1, 10)])
        self.assertEqual(rules["R03"]["state"], "firing")                         # firing beats ok
        self.assertEqual(rules["R03"]["subjects"], [{"subject": "gateway", "state": "ok", "value": 40.0, "detail": ""},
                                                    {"subject": "orders", "state": "firing", "value": 90.92, "detail": ""}])
        self.assertEqual(rules["R02"]["state"], "no_data")
        self.assertEqual(rules["R02"]["subjects"][0]["value"], None)
        self.assertEqual(rules["R01"]["state"], "ok")
        self.assertEqual(rules["R04"]["state"], "no_data")                          # not evaluated in this snapshot
        self.assertEqual(rules["R04"]["subjects"], [])
        self.assertEqual(rules["R03"]["name"], "cpu_high")

    def test_open_incident_id_is_null_without_an_open_incident(self):
        d = Driver(METAS)
        # an empty store: use a second fixture
        with tempfile.TemporaryDirectory() as tmp:
            fx = ApiFixture(tmp)
            try:
                self.assertIsNone(fx.json("/internal/v1/status")[2]["open_incident_id"])
            finally:
                fx.close()


class RulesTests(ApiTestCase):
    def test_rules(self):
        status, _, body = self.fx.json("/internal/v1/rules")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in body["rules"]], [f"R0{i}" for i in range(1, 10)])
        r05 = body["rules"][4]
        self.assertEqual(r05["min_traffic"]["min"], 0.5)
        self.assertEqual(set(r05["provenance"]), {"baseline_max", "fault_value_min", "margin_ratio", "derived_from"})
        self.assertEqual(body["rules"][8]["opens_incident"], False)


class IncidentTests(ApiTestCase):
    def incidents(self):
        return make_incident(rounds=3)

    def test_list_is_newest_first_with_full_incident_objects(self):
        status, _, body = self.fx.json("/internal/v1/incidents")
        self.assertEqual(status, 200)
        self.assertEqual([i["incident_id"] for i in body["incidents"]], ["INC-0003", "INC-0002", "INC-0001"])
        first = body["incidents"][0]
        self.assertEqual(list(first), INCIDENT_FIELDS)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(len(first["symptoms"]), 2)
        self.assertEqual(first["primary_service"], "postgres")

    def test_status_filter(self):
        ids = lambda q: [i["incident_id"] for i in self.fx.json("/internal/v1/incidents" + q)[2]["incidents"]]
        self.assertEqual(ids("?status=open"), ["INC-0003"])
        self.assertEqual(ids("?status=resolved"), ["INC-0002", "INC-0001"])

    def test_limit(self):
        ids = [i["incident_id"] for i in self.fx.json("/internal/v1/incidents?limit=2")[2]["incidents"]]
        self.assertEqual(ids, ["INC-0003", "INC-0002"])

    def test_since(self):
        second = self.fx.json("/internal/v1/incidents/INC-0002")[2]["opened_at"]
        ids = [i["incident_id"] for i in self.fx.json(f"/internal/v1/incidents?since={second}")[2]["incidents"]]
        self.assertEqual(ids, ["INC-0003", "INC-0002"])
        naive = second.rstrip("Z")                                                   # no zone: taken as UTC
        ids = [i["incident_id"] for i in self.fx.json(f"/internal/v1/incidents?since={naive}")[2]["incidents"]]
        self.assertEqual(ids, ["INC-0003", "INC-0002"])

    def test_bad_parameters_are_400_with_the_error_schema(self):
        for query in ("?status=closed", "?limit=0", "?limit=201", "?limit=abc", "?limit=", "?since=yesterday", "?since="):
            status, headers, body = self.fx.json("/internal/v1/incidents" + query)
            self.assertEqual(status, 400, query)
            self.assertEqual(list(body), ["error"])
            self.assertEqual(body["error"]["code"], "bad_request")
            self.assertTrue(body["error"]["message"])
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_limit_boundaries_are_accepted(self):
        self.assertEqual(self.fx.get("/internal/v1/incidents?limit=1")[0], 200)
        self.assertEqual(self.fx.get("/internal/v1/incidents?limit=200")[0], 200)

    def test_one_incident(self):
        status, _, body = self.fx.json("/internal/v1/incidents/INC-0001")
        self.assertEqual((status, body["incident_id"], body["status"]), (200, "INC-0001", "resolved"))
        self.assertEqual(list(body), INCIDENT_FIELDS)
        self.assertEqual(list(body["symptoms"][0])[:3], ["symptom_id", "rule_id", "detector"])

    def test_unknown_and_malformed_ids_are_404(self):
        for path in ("/internal/v1/incidents/INC-0099", "/internal/v1/incidents/nonsense",
                     "/internal/v1/incidents/INC-1%3B%20DROP%20TABLE", "/internal/v1/incidents/INC-0001/extra",
                     "/internal/v1/incidents/"):
            status, _, body = self.fx.json(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body["error"]["code"], "not_found")

    def test_unknown_path_is_404(self):
        status, _, body = self.fx.json("/nope")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))

    def test_other_methods_are_405_with_allow_get(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
            for path in ("/health", "/internal/v1/incidents", "/internal/v1/incidents/INC-0001", "/metrics"):
                status, headers, body = self.fx.request(method, path)
                self.assertEqual(status, 405, (method, path))
                self.assertEqual(headers["Allow"], "GET")
                if method != "HEAD":
                    self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")

    def test_no_write_reaches_the_store_through_the_api(self):
        before = self.fx.json("/internal/v1/incidents")[2]
        self.fx.request("POST", "/internal/v1/incidents")
        self.fx.request("DELETE", "/internal/v1/incidents/INC-0001")
        self.assertEqual(self.fx.json("/internal/v1/incidents")[2], before)

    def test_an_unreadable_store_is_503_not_a_stack_trace(self):
        self.fx.ctx.db_path = str(Path(self.tmp.name) / "missing" / "no.db")
        status, _, body = self.fx.json("/internal/v1/incidents")
        self.assertEqual((status, body["error"]["code"]), (503, "store_unavailable"))
        status, _, body = self.fx.json("/internal/v1/status")
        self.assertEqual((status, body["error"]["code"]), (503, "store_unavailable"))
        self.assertEqual(self.fx.get("/health")[0], 200)

    def test_an_unexpected_exception_is_a_500_without_details(self):
        self.fx.ctx.rules = None                                                      # breaks /rules
        status, _, body = self.fx.json("/internal/v1/rules")
        self.assertEqual((status, body["error"]["code"]), (500, "internal_error"))
        self.assertNotIn("Traceback", json.dumps(body))
        self.assertNotIn("NoneType", json.dumps(body))

    def test_dispatch_is_a_pure_function(self):
        status, content_type, body, headers = dispatch(self.fx.ctx, "GET", "/health")
        self.assertEqual((status, content_type), (200, "application/json; charset=utf-8"))


if __name__ == "__main__":
    unittest.main()
