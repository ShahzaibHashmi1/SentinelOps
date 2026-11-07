import copy
import io
import unittest
from contextlib import redirect_stdout

import _bootstrap  # noqa: F401
from _bootstrap import SIGNALS_PATH, TOPOLOGY_PATH

from app import signals as sg
from app.model import ModelError, load_json_file
from app.topology import load_topology

TOPO = load_topology(TOPOLOGY_PATH)


def raw():
    return copy.deepcopy(load_json_file(SIGNALS_PATH))


def parse(data):
    return sg.parse_signals(data, set(TOPO.services), TOPO.tiers)


class RealConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = sg.load_signals(SIGNALS_PATH, set(TOPO.services), TOPO.tiers)

    def test_loads(self):
        names = [s.name for s in self.cfg.signals]
        self.assertEqual(names, ["cpu_pct_of_limit", "memory_pct_of_limit", "container_present", "up", "restarts", "request_rate",
                                 "error_ratio_5xx", "latency_p95", "dependency_error_rate", "dependency_latency_p95"])
        self.assertEqual(self.cfg.window.step_s, 5)
        self.assertEqual(self.cfg.window.pre_s, 120)
        self.assertEqual(self.cfg.window.baseline_s, 300)
        self.assertEqual(self.cfg.window.query_timeout_s, 3)
        self.assertEqual(self.cfg.sources, ("detector", "prometheus", "loki", "docker", "topology"))

    def test_tiers(self):
        infra = {s.name for s in self.cfg.signals_for("infra")}
        self.assertEqual(infra, {"cpu_pct_of_limit", "memory_pct_of_limit", "container_present", "restarts"})
        self.assertIn("up", {s.name for s in self.cfg.signals_for("app")})

    def test_documented_numbers_match_the_detector_table(self):
        thr = lambda name, svc: self.cfg.by_name(name).threshold_for(svc).absolute_min
        self.assertEqual(thr("cpu_pct_of_limit", "orders"), 85)
        self.assertEqual(thr("memory_pct_of_limit", "payments"), 80)
        self.assertEqual(thr("error_ratio_5xx", "gateway"), 5)
        self.assertEqual(thr("dependency_error_rate", "orders"), 0.1)
        self.assertEqual(thr("latency_p95", "gateway"), 0.5)
        self.assertEqual(thr("request_rate", "gateway"), 30)
        self.assertIsNone(self.cfg.by_name("request_rate").threshold_for("orders"))

    def test_every_threshold_has_provenance_and_margin(self):
        for sig in self.cfg.signals:
            for th in sig.thresholds:
                self.assertTrue(th.provenance.source)
                if th.provenance.kind != "assumed" and th.provenance.baseline_max:
                    self.assertGreaterEqual(th.absolute_min, 1.5 * th.provenance.baseline_max - 1e-9)
        for sig in self.cfg.signals:
            if sig.kind == "down":
                self.assertTrue(sig.provenance.source)

    def test_assumed_count_is_two(self):
        self.assertEqual(self.cfg.assumed_count(), 2)

    def test_queries_have_the_placeholder_and_no_leftover(self):
        for sig in self.cfg.signals:
            out = sg.render(sig.query, service="orders")
            self.assertNotIn("@", out)
            self.assertIn('"orders"', out)

    def test_excluded_routes_and_label_rules(self):
        for name in ("request_rate", "error_ratio_5xx", "latency_p95"):
            self.assertIn("route!~", self.cfg.by_name(name).query)
        for name in ("dependency_error_rate", "dependency_latency_p95"):
            q = self.cfg.by_name(name).query
            self.assertIn('job="@service@"', q)
            self.assertNotIn('service="@service@"', q)
            self.assertEqual(self.cfg.by_name(name).by, "dependency")

    def test_log_queries_render(self):
        lg = self.cfg.logs
        sel = sg.render(lg.selectors["app"], service="orders")
        self.assertEqual(sel, '{service="orders",level=~"WARNING|ERROR"}')
        self.assertEqual(sg.render(lg.count_query, selector=sel, range=120), 'sum(count_over_time({service="orders",level=~"WARNING|ERROR"} [120s]))')
        ev = sg.render(lg.events_query, selector=sel, range=120, top=5)
        self.assertIn('| json event="event"', ev)
        self.assertNotIn("request_id", ev)
        self.assertEqual((lg.top_events, lg.samples, lg.sample_chars, lg.request_ids), (5, 3, 300, 3))

    def test_table_and_cli(self):
        lines = sg.format_table(self.cfg)
        self.assertEqual(len(lines), 2 + len(sg.provenance_rows(self.cfg)))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = sg.main(["x", SIGNALS_PATH])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("10 signals valid", out)
        self.assertIn("assumed provenance: 2", out)

    def test_cli_bad_usage_and_bad_file(self):
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            self.assertEqual(sg.main(["x"]), 2)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(sg.main(["x", "/no/such.json"]), 1)


class ValidationTests(unittest.TestCase):
    def sig(self, data, name):
        return next(s for s in data["signals"] if s["name"] == name)

    def test_baseline_margin_violation(self):
        d = raw()
        self.sig(d, "cpu_pct_of_limit")["thresholds"][0]["absolute_min"] = 60  # below 1.5 x 44
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_threshold_above_first_fault_reading(self):
        d = raw()
        self.sig(d, "cpu_pct_of_limit")["thresholds"][0]["absolute_min"] = 95
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_documented_number_needs_baseline(self):
        d = raw()
        self.sig(d, "cpu_pct_of_limit")["thresholds"][0]["provenance"]["baseline_max"] = None
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_unknown_key_and_missing_key(self):
        d = raw()
        d["extra"] = 1
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)
        d = raw()
        del self.sig(d, "up")["query"]
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_duplicate_signal_name(self):
        d = raw()
        d["signals"].append(copy.deepcopy(d["signals"][0]))
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_unknown_service_and_wrong_tier(self):
        d = raw()
        self.sig(d, "request_rate")["thresholds"][0]["services"] = ["nope"]
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)
        d = raw()
        self.sig(d, "request_rate")["thresholds"][0]["services"] = ["postgres"]  # infra, signal is app only
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_service_in_two_entries(self):
        d = raw()
        d_sig = self.sig(d, "error_ratio_5xx")
        d_sig["thresholds"][1]["services"].append("gateway")
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_query_without_placeholder_or_with_unknown_one(self):
        d = raw()
        self.sig(d, "up")["query"] = "min(up)"
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)
        d = raw()
        self.sig(d, "up")["query"] = 'min(up{job="@service@",x="@other@"})'
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_down_signal_rules(self):
        d = raw()
        self.sig(d, "up")["thresholds"] = [{"services": ["*"], "absolute_min": 1, "provenance": {"kind": "assumed", "baseline_max": None, "fault_value_min": None, "source": "x"}}]
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)
        d = raw()
        self.sig(d, "cpu_pct_of_limit")["comparator"] = "=="
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_bad_enums_and_numbers(self):
        for key, value in (("kind", "weird"), ("comparator", "<"), ("empty", "maybe"), ("tiers", ["cloud"]), ("by", "Bad Label")):
            d = raw()
            self.sig(d, "latency_p95")[key] = value
            with self.subTest(key), self.assertRaises(sg.SignalsConfigError):
                parse(d)
        d = raw()
        d["window"]["step_s"] = 0
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)
        d = raw()
        d["window"]["pre_s"] = "120"
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_sources_must_be_the_five(self):
        d = raw()
        for bad in (["detector", "prometheus"], ["a", "b", "c", "d", "d"], ["a", "b", "c", "d", 5], "abcde"):
            d["sources"] = bad
            with self.subTest(bad), self.assertRaises(sg.SignalsConfigError):
                parse(d)

    def test_bad_guard(self):
        d = raw()
        self.sig(d, "latency_p95")["guard"]["on_fail"] = "explode"
        with self.assertRaises(sg.SignalsConfigError):
            parse(d)

    def test_render_missing_value(self):
        with self.assertRaises(sg.SignalsConfigError):
            sg.render("a @x@ @y@", x=1)

    def test_duplicate_json_keys_are_refused(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dup.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"a": 1, "a": 2}')
            with self.assertRaises(ModelError):
                load_json_file(path)
            with self.assertRaises(sg.SignalsConfigError):
                sg.load_signals(path)


if __name__ == "__main__":
    unittest.main()
