import io
import json
import logging
import re
import unittest

import _bootstrap  # noqa: F401
from app import logging_setup
from app.logging_setup import JsonFormatter, configure, log_event, valid_level
from app.metrics import Metrics, MetricsSnapshot

EXPECTED_NAMES = [
    "detector_evaluation_cycles_total", "detector_last_evaluation_timestamp_seconds",
    "detector_evaluation_duration_seconds", "detector_source_status", "detector_rule_state",
    "detector_rule_value", "detector_incidents_open", "detector_incidents_total", "detector_query_errors_total"]


def snapshot(**changes):
    base = dict(cycles=7, last_evaluation_timestamp_s=1760000000.5, duration_s=0.042,
                source_ok={"prometheus": True, "loki": False},
                rule_states={("R03", "orders"): "firing", ("R03", "gateway"): "ok", ("R02", "redis"): "no_data"},
                rule_values={("R03", "orders"): 90.92, ("R03", "gateway"): 40.0},
                incidents_open=1, incidents_total=3, query_errors={"prometheus": 2, "loki": 0})
    base.update(changes)
    return MetricsSnapshot(**base)


def render(snap):
    m = Metrics()
    m.publish(snap)
    return m.render()


class MetricsTests(unittest.TestCase):
    def lines(self, text):
        return [l for l in text.splitlines() if l and not l.startswith("#")]

    def test_all_metrics_have_help_and_type(self):
        text = render(snapshot())
        for name in EXPECTED_NAMES:
            self.assertIn(f"# HELP {name} ", text)
            self.assertRegex(text, rf"# TYPE {name} (counter|gauge)\n")
        for name in ("detector_evaluation_cycles_total", "detector_incidents_total", "detector_query_errors_total"):
            self.assertIn(f"# TYPE {name} counter", text)
        for name in ("detector_rule_state", "detector_rule_value", "detector_source_status", "detector_incidents_open"):
            self.assertIn(f"# TYPE {name} gauge", text)

    def test_sample_lines(self):
        lines = self.lines(render(snapshot()))
        self.assertIn("detector_evaluation_cycles_total 7", lines)
        self.assertIn("detector_last_evaluation_timestamp_seconds 1760000000.5", lines)
        self.assertIn("detector_evaluation_duration_seconds 0.042", lines)
        self.assertIn('detector_source_status{source="prometheus"} 1', lines)
        self.assertIn('detector_source_status{source="loki"} 0', lines)
        self.assertIn('detector_rule_state{rule_id="R03",subject="orders"} 1', lines)
        self.assertIn('detector_rule_state{rule_id="R03",subject="gateway"} 0', lines)
        self.assertIn('detector_rule_state{rule_id="R02",subject="redis"} -1', lines)
        self.assertIn('detector_rule_value{rule_id="R03",subject="orders"} 90.92', lines)
        self.assertIn("detector_incidents_open 1", lines)
        self.assertIn("detector_incidents_total 3", lines)
        self.assertIn('detector_query_errors_total{source="prometheus"} 2', lines)

    def test_rule_value_series_is_left_out_when_there_is_no_value(self):
        text = render(snapshot())
        self.assertNotIn('detector_rule_value{rule_id="R02",subject="redis"}', text)       # no_data: no value
        self.assertIn('detector_rule_state{rule_id="R02",subject="redis"} -1', text)
        self.assertEqual(len([l for l in self.lines(text) if l.startswith("detector_rule_value")]), 2)

    def test_no_label_is_called_service(self):
        text = render(snapshot())
        self.assertIsNone(re.search(r"[{,]service=", text))
        self.assertIsNone(re.search(r"exported_service", text))

    def test_labels_are_escaped(self):
        text = render(snapshot(rule_states={("R01", 'a"b\\c\nd'): "ok"}, rule_values={}))
        self.assertIn('detector_rule_state{rule_id="R01",subject="a\\"b\\\\c\\nd"} 0', text)

    def test_a_missing_subject_is_an_empty_label(self):
        self.assertIn('detector_rule_state{rule_id="R05",subject=""} 0', render(snapshot(rule_states={("R05", None): "ok"})))

    def test_empty_snapshot_before_the_first_cycle(self):
        text = render(MetricsSnapshot())
        self.assertIn("detector_evaluation_cycles_total 0", text)
        self.assertIn("detector_incidents_open 0", text)
        self.assertEqual([l for l in self.lines(text) if "{" in l], [])

    def test_output_is_sorted_and_ends_with_a_newline(self):
        text = render(snapshot())
        self.assertTrue(text.endswith("\n"))
        states = [l for l in self.lines(text) if l.startswith("detector_rule_state")]
        self.assertEqual(states, sorted(states, key=lambda l: l.split("{")[1]))
        self.assertEqual(text, render(snapshot()))

    def test_every_line_is_valid_exposition_syntax(self):
        pattern = re.compile(r'^(#.*|[a-z_]+(\{[a-z_]+="[^"\\]*(\\.[^"\\]*)*"(,[a-z_]+="[^"\\]*(\\.[^"\\]*)*")*\})? -?[0-9.e+-]+)$')
        for line in render(snapshot()).splitlines():
            self.assertRegex(line, pattern)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.logger = configure("DEBUG", "1.0.0", self.stream)

    def tearDown(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)

    def last(self):
        return json.loads(self.stream.getvalue().strip().splitlines()[-1])

    def test_fields_and_order_like_the_m1_services(self):
        log_event(self.logger, logging.INFO, "incident_opened", incident_id="INC-0001", n=2)
        line = self.stream.getvalue().strip()
        data = json.loads(line)
        self.assertEqual(list(data)[:6], ["ts", "level", "service", "version", "request_id", "event"])
        self.assertEqual((data["level"], data["service"], data["version"], data["request_id"], data["event"]),
                         ("INFO", "detector", "1.0.0", None, "incident_opened"))
        self.assertEqual((data["incident_id"], data["n"]), ("INC-0001", 2))
        self.assertRegex(data["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertNotIn("\n", line)

    def test_levels(self):
        for level, name in ((logging.DEBUG, "DEBUG"), (logging.WARNING, "WARNING"), (logging.ERROR, "ERROR")):
            log_event(self.logger, level, "x")
            self.assertEqual(self.last()["level"], name)

    def test_level_filter(self):
        configure("WARNING", "1.0.0", self.stream)
        log_event(self.logger, logging.INFO, "hidden")
        log_event(self.logger, logging.WARNING, "shown")
        self.assertEqual([json.loads(l)["event"] for l in self.stream.getvalue().splitlines()], ["shown"])

    def test_configure_twice_does_not_duplicate_lines(self):
        configure("INFO", "1.0.0", self.stream)
        log_event(self.logger, logging.INFO, "once")
        self.assertEqual(len(self.stream.getvalue().splitlines()), 1)

    def test_non_ascii_and_unserialisable_values(self):
        log_event(self.logger, logging.INFO, "x", title="caf\u00e9", obj=object())
        self.assertEqual(self.last()["title"], "caf\u00e9")
        self.assertIn("object", self.last()["obj"])

    def test_valid_level(self):
        self.assertTrue(valid_level("info"))
        self.assertFalse(valid_level("TRACE"))


if __name__ == "__main__":
    unittest.main()
