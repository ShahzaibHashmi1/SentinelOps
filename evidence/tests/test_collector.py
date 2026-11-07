import json
import unittest
from datetime import datetime, timezone

import _bootstrap  # noqa: F401
import test_bundle
from fakes import SIGNALS, TOPOLOGY, at, change
from fakes_io import FakeHttp, matrix_payload, ok, streams_payload, ts, vector_payload

from app import bundle as bd
from app import collector as co
from app.model import ContainerObservation
from app.sources import LokiSource, PrometheusSource

NOW = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
W = SIGNALS.window
INC = test_bundle.incident(first=1000)
WINDOW = bd.compute_window(INC, [], at(1200), W)  # window 880..1200, baseline 580..880
STEP = 5


def steps(a, b):
    return [ts(s) for s in range(int(a), int(b) + 1, STEP)]


class PromScript:
    """A fake Prometheus: the first matching rule answers a range query; guards are ok unless overridden."""

    def __init__(self, http):
        self.http = http
        self.rules = []
        self.queries = []
        http.routes["/api/v1/query_range"] = self.handle

    def on(self, *needles, series):
        self.rules.append((needles, series))

    def handle(self, params, method):
        q = params["query"]
        self.queries.append((q, params))
        for needles, series in self.rules:
            if all(n in q for n in needles):
                return ok(matrix_payload(series(params) if callable(series) else series))
        if q.startswith("count(container_memory_working_set_bytes{service=~"):
            return ok(matrix_payload([({}, [(t, 6) for t in steps_of(params)])]))
        if q.startswith("sum(rate(http_requests_total") and "status" not in q:
            return ok(matrix_payload([({}, [(t, 10) for t in steps_of(params)])]))
        return ok(matrix_payload([]))


def steps_of(params):
    return [float(params["start"]) + i * float(params["step"]) for i in range(int((float(params["end"]) - float(params["start"])) / float(params["step"])) + 1)]


def flat(value, a=580, b=1200):
    return [(t, value) for t in steps(a, b)]


def rising(low, high, switch=900, a=580, b=1200):
    return [(t, low if t < ts(switch) else high) for t in steps(a, b)]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.pq = FakeHttp()
        self.lk = FakeHttp()
        self.prom = PrometheusSource("http://p", 0, transport=self.pq)
        self.loki = LokiSource("http://l", 0, transport=self.lk)
        self.prom.begin_cycle(0.0, NOW)
        self.loki.begin_cycle(0.0, NOW)
        self.script = PromScript(self.pq)
        self.col = co.Collector(self.prom, self.loki, SIGNALS, TOPOLOGY, workers=4)
        self.lk.routes["/loki/api/v1/query"] = lambda p, m: ok(vector_payload([]))
        self.lk.routes["/loki/api/v1/query_range"] = lambda p, m: ok(streams_payload([]))

    def tearDown(self):
        self.col.close()

    def collect(self, containers=None, changes=None, window=WINDOW):
        return self.col.collect(window, containers or {}, changes or {})

    @staticmethod
    def signal(ev, name):
        return next(s for s in ev.signals if s.name == name)


class SignalTests(Fixture):
    def test_every_service_is_returned_with_its_tier(self):
        out = self.collect()
        self.assertEqual(sorted(out), list(TOPOLOGY.services))
        self.assertEqual(out["postgres"].tier, "infra")
        self.assertEqual(out["orders"].tier, "app")

    def test_baseline_and_window_are_split_at_window_start(self):
        self.script.on("container_cpu_usage_seconds_total", 'service="orders"', series=[({}, rising(20, 92))])
        s = self.signal(self.collect()["orders"], "cpu_pct_of_limit")
        self.assertTrue(s.anomalous)
        self.assertEqual(s.first_anomalous_at, at(900))
        self.assertEqual(s.baseline_max, 20)
        self.assertEqual(s.window_max, 92)

    def test_a_point_exactly_at_window_start_belongs_to_the_window(self):
        series = [(t, 10 if t < ts(880) else 95) for t in steps(580, 1200)]  # the first high value is at window.start itself
        self.script.on("container_cpu_usage_seconds_total", 'service="orders"', series=[({}, series)])
        s = self.signal(self.collect()["orders"], "cpu_pct_of_limit")
        self.assertEqual(s.first_anomalous_at, at(880))
        self.assertEqual(s.baseline_max, 10)

    def test_one_range_query_covers_baseline_and_window(self):
        self.collect()
        cpu = [p for q, p in self.script.queries if "container_cpu_usage_seconds_total" in q and 'service="orders"' in q]
        self.assertEqual(len(cpu), 1)
        self.assertEqual((float(cpu[0]["start"]), float(cpu[0]["end"]), cpu[0]["step"]), (ts(580), ts(1200), "5"))

    def test_the_number_of_range_queries_is_known(self):
        self.collect()
        # 4 app services x 10 signals + 2 infra services x 4 signals + 1 shared container guard
        # + 4 rate guards (the error-ratio and the latency guard of a service have the same text: one query)
        self.assertEqual(len(self.script.queries), 4 * 10 + 2 * 4 + 1 + 4)
        guards = [q for q, _p in self.script.queries if q.startswith("count(container_memory_working_set_bytes{service=~")]
        self.assertEqual(len(guards), 1)

    def test_unclean_baseline_uses_only_absolute_thresholds(self):
        self.script.on("container_cpu_usage_seconds_total", 'service="orders"', series=[({}, rising(70, 100))])
        clean = self.signal(self.collect()["orders"], "cpu_pct_of_limit")
        self.assertEqual(clean.threshold, 105)  # 1.5 x 70
        self.assertFalse(clean.anomalous)
        unclean = WINDOW.__class__(WINDOW.start, WINDOW.end, WINDOW.baseline_start, WINDOW.baseline_end, False)
        s = self.signal(self.collect(window=unclean)["orders"], "cpu_pct_of_limit")
        self.assertEqual(s.threshold, 85)
        self.assertTrue(s.anomalous)

    def test_container_missing_from_the_first_step_it_is_zero(self):
        self.script.on('count(container_memory_working_set_bytes{service="postgres"}) or vector(0)',
                       series=[({}, [(t, 1 if t < ts(950) else 0) for t in steps(580, 1200)])])
        s = self.signal(self.collect()["postgres"], "container_present")
        self.assertTrue(s.anomalous)
        self.assertEqual(s.first_anomalous_at, at(950))

    def test_container_present_without_series_anywhere_is_no_data_not_missing(self):
        self.script.on("count(container_memory_working_set_bytes{service=~", series=[])
        self.script.on('count(container_memory_working_set_bytes{service="postgres"}) or vector(0)', series=[({}, flat(0))])
        ev = self.collect()["postgres"]
        self.assertNotIn("container_present", [s.name for s in ev.signals])
        self.assertIn("container_present", [g[0] for g in ev.gaps])

    def test_down_signal_up(self):
        self.script.on('min(up{job="inventory"})', series=[({}, [(t, 1 if t < ts(1000) else 0) for t in steps(580, 1200)])])
        s = self.signal(self.collect()["inventory"], "up")
        self.assertTrue(s.anomalous)
        self.assertEqual(s.first_anomalous_at, at(1000))

    def test_restarts_from_start_time(self):
        base = 1_791_142_000
        self.script.on("container_start_time_seconds", 'service="payments"', series=[({}, [(t, base if t < ts(1000) else base + 31) for t in steps(580, 1200)])])
        s = self.signal(self.collect()["payments"], "restarts")
        self.assertTrue(s.anomalous)
        self.assertEqual(s.window_max, 1)

    def test_grouped_dependency_signals(self):
        q = 'dependency_requests_total{job="orders"'
        self.script.on(q, series=[({"dependency": "postgres"}, rising(0, 0.4)), ({"dependency": "inventory"}, flat(0))])
        names = {s.name: s for s in self.collect()["orders"].signals if s.name.startswith("dependency_error_rate")}
        self.assertTrue(names["dependency_error_rate:postgres"].anomalous)
        self.assertFalse(names["dependency_error_rate:inventory"].anomalous)

    def test_error_ratio_uses_the_rate_guard(self):
        err = [({}, flat(100))]
        self.script.on("status=~", 'service="gateway"', series=err)
        quiet = lambda params: [({}, [(t, 0.1) for t in steps_of(params)])]  # below 0.5 req/s
        self.script.rules.insert(0, ((('sum(rate(http_requests_total{service="gateway"',)), quiet))
        s = self.collect()["gateway"]
        value = [x for x in s.signals if x.name == "error_ratio_5xx"][0]
        self.assertFalse(value.anomalous)  # a quiet step cannot raise the alarm

    def test_a_failed_query_makes_only_that_signal_no_data(self):
        self.pq.routes["/api/v1/query_range"] = lambda params, method: (
            (400, b'{"status":"error","error":"bad"}') if "http_request_duration_seconds_bucket" in params["query"] and 'service="gateway"' in params["query"]
            else self.script.handle(params, method))
        ev = self.collect()["gateway"]
        self.assertIn("latency_p95", [g[0] for g in ev.gaps])
        self.assertNotIn("latency_p95", [s.name for s in ev.signals])
        self.assertIn("cpu_pct_of_limit", [s.name for s in ev.signals])
        self.assertIn("HTTP 400", dict(ev.gaps)["latency_p95"])

    def test_prometheus_outage_costs_one_timeout_not_one_per_query(self):
        self.pq.fail = "timed out"
        out = self.collect()
        self.assertLessEqual(self.pq.count("/api/v1/query_range"), 8)  # the pool size plus the calls already in flight
        for name, ev in out.items():
            self.assertEqual(ev.signals, ())
            self.assertEqual(len(ev.gaps), len(SIGNALS.signals_for(ev.tier)))
        self.assertEqual(self.prom.status().state, "no_data")

    def test_prometheus_warming_up_gives_no_calls_and_gaps(self):
        prom = PrometheusSource("http://p", 30, transport=self.pq)
        prom.begin_cycle(0.0, NOW)
        col = co.Collector(prom, self.loki, SIGNALS, TOPOLOGY)
        try:
            out = col.collect(WINDOW, {}, {})
        finally:
            col.close()
        self.assertEqual(self.pq.count("/api/v1/query_range"), 0)
        self.assertIn("warming_up", dict(out["orders"].gaps)["cpu_pct_of_limit"])

    def test_container_and_changes_are_passed_through(self):
        box = ContainerObservation("orders", status="running")
        rec = change("orders", "recreated", 900)
        out = self.collect({"orders": box}, {"orders": [rec]})
        self.assertIs(out["orders"].container, box)
        self.assertIsNone(out["payments"].container)
        self.assertEqual(out["orders"].changes, (rec,))

    def test_a_signal_with_an_empty_answer_is_reported_without_verdict(self):
        s = self.signal(self.collect()["orders"], "cpu_pct_of_limit")
        self.assertFalse(s.anomalous)
        self.assertIsNone(s.window_max)

    def test_choose_step(self):
        self.assertEqual(co.choose_step(620, 5), 5)
        self.assertEqual(co.choose_step(50000, 5), 5)
        self.assertEqual(co.choose_step(50001, 5), 10)
        self.assertEqual(co.choose_step(200000, 5), 20)

    def test_long_windows_widen_the_step(self):
        inc = test_bundle.incident(first=1000, last=1000)
        long_window = bd.compute_window(inc, [], at(1000 + 3600 * 20), W)
        self.collect(window=long_window)
        steps_used = {p["step"] for _q, p in self.script.queries}
        self.assertEqual(steps_used, {"10"})


class LogTests(Fixture):
    def lines(self, *items):
        return streams_payload([(1_700_000_000_000_000_000 + i, line) for i, line in enumerate(items)])

    def test_log_evidence_of_an_app_service(self):
        def vec(params, method):
            q = params["query"]
            if q.startswith("sum(count_over_time"):
                return ok(vector_payload([({}, 7 if "[320s]" in q else 2)]))
            if q.startswith("topk(5"):
                return ok(vector_payload([({"event": "db_timeout"}, 4), ({"event": "reserve_failed"}, 4), ({"event": "other"}, 1)]))
            return ok(vector_payload([]))

        self.lk.routes["/loki/api/v1/query"] = vec
        long_line = json.dumps({"level": "ERROR", "request_id": "req-1", "msg": "x" * 400, "password": "hunter2"})
        self.lk.routes["/loki/api/v1/query_range"] = lambda p, m: ok(self.lines(
            long_line, json.dumps({"level": "ERROR", "request_id": "req-2"}), "application_name=chaos-F11 waiting",
            json.dumps({"level": "WARNING", "request_id": "req-1"}), json.dumps({"request_id": None}), "plain text line"))
        ev = self.collect()["orders"].logs
        self.assertEqual((ev.state, ev.window_count, ev.baseline_count), ("ok", 7, 2))
        self.assertEqual(ev.top_events, (("db_timeout", 4), ("reserve_failed", 4), ("other", 1)))
        self.assertEqual(len(ev.samples), 3)
        self.assertTrue(all(len(s) <= 300 for s in ev.samples))
        text = json.dumps(ev.to_dict())
        self.assertNotIn("hunter2", text)
        self.assertNotIn("chaos", text.lower())
        self.assertEqual(ev.request_ids, ("req-1", "req-2"))

    def test_loki_queries_use_the_window_and_baseline_ranges(self):
        self.collect()
        counts = [p for p in (c[2] for c in self.lk.calls if c[1] == "/loki/api/v1/query") if p["query"].startswith("sum(count_over_time")]
        orders = [p["query"] for p in counts if 'service="orders"' in p["query"]]
        self.assertEqual(sorted(q.split("[")[1] for q in orders), ["300s]))", "320s]))"])
        times = {float(p["time"]) for p in counts if 'service="orders"' in p["query"]}
        self.assertEqual(times, {ts(1200), ts(880)})

    def test_infra_services_use_the_text_match_and_have_no_events(self):
        self.collect()
        queries = [c[2]["query"] for c in self.lk.calls if c[1] == "/loki/api/v1/query"]
        postgres = [q for q in queries if 'service="postgres"' in q]
        self.assertTrue(postgres and all("|~" in q and "level" not in q for q in postgres))
        self.assertFalse(any(q.startswith("topk") for q in postgres))
        orders = [q for q in queries if 'service="orders"' in q]
        self.assertTrue(any(q.startswith("topk(5") for q in orders))
        self.assertTrue(all('level=~"WARNING|ERROR"' in q for q in orders))

    def test_empty_answers_mean_zero_lines(self):
        ev = self.collect()["payments"].logs
        self.assertEqual((ev.state, ev.window_count, ev.baseline_count, ev.top_events, ev.samples, ev.request_ids), ("ok", 0, 0, (), (), ()))

    def test_loki_not_ok_gives_no_data_and_no_calls(self):
        self.lk.ready_status = 503
        self.loki.begin_cycle(5.0, NOW)
        out = self.collect()
        self.assertEqual(self.lk.count("/loki/api/v1/query"), 0)
        self.assertEqual(out["orders"].logs.state, "no_data")
        self.assertIn(("logs", "loki is no_data"), out["orders"].gaps)

    def test_a_failing_log_query_affects_only_that_service(self):
        def vec(params, method):
            if 'service="payments"' in params["query"]:
                return 500, b'{"error": "boom"}'
            return ok(vector_payload([]))

        self.lk.routes["/loki/api/v1/query"] = vec
        out = self.collect()
        self.assertEqual(out["payments"].logs.state, "no_data")
        self.assertEqual(out["orders"].logs.state, "ok")
        self.assertEqual(dict(out["payments"].gaps)["logs"][:8], "HTTP 500")

    def test_bundle_assignment_of_log_gaps_goes_to_loki(self):
        self.lk.routes["/loki/api/v1/query"] = lambda p, m: (500, b"{}")
        out = self.collect()
        b = test_bundle.build(services=out)
        self.assertTrue(any(g["source"] == "loki" and g["signal"] == "logs" for g in b["data_gaps"]))


if __name__ == "__main__":
    unittest.main()
