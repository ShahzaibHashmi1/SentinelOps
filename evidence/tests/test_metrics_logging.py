import io
import json
import logging
import re
import unittest

import _bootstrap  # noqa: F401

from app.logging_setup import log_event, setup_logging
from app.metrics import Metrics
from app.model import CHANGE_KINDS, TRIGGERS

SOURCES = ["detector", "prometheus", "loki", "docker", "topology"]


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.m = Metrics(SOURCES)

    def lines(self):
        return self.m.render().splitlines()

    def test_all_series_exist_before_any_event(self):
        text = self.m.render()
        for name in ("evidence_build_cycles_total", "evidence_last_cycle_timestamp_seconds", "evidence_cycle_duration_seconds",
                     "evidence_bundles_total", "evidence_build_duration_seconds", "evidence_source_status", "evidence_changes_total"):
            self.assertIn(f"# TYPE {name} ", text)
        for t in TRIGGERS:
            self.assertIn(f'evidence_bundles_total{{trigger="{t}"}} 0', text)
        for k in CHANGE_KINDS:
            self.assertIn(f'evidence_changes_total{{kind="{k}"}} 0', text)
        for s in SOURCES:
            self.assertIn(f'evidence_source_status{{source="{s}"}} 0', text)

    def test_counters_and_gauges(self):
        self.m.finish_cycle(1700000000.5, 0.25)
        self.m.finish_cycle(1700000005.5, 0.5)
        self.m.count_bundle("opened", 1.5)
        self.m.count_bundle("opened", 2.5)
        self.m.set_source("loki", True)
        self.m.count_change("recreated")
        text = self.m.render()
        self.assertIn("evidence_build_cycles_total 2", text)
        self.assertIn("evidence_last_cycle_timestamp_seconds 1700000005.5", text)
        self.assertIn("evidence_cycle_duration_seconds 0.5", text)
        self.assertIn('evidence_bundles_total{trigger="opened"} 2', text)
        self.assertIn("evidence_build_duration_seconds 2.5", text)
        self.assertIn('evidence_source_status{source="loki"} 1', text)
        self.assertIn('evidence_changes_total{kind="recreated"} 1', text)

    def test_the_text_format_is_valid(self):
        self.m.count_bundle("refresh", 0.1)
        sample = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[a-z_]+="[^"\\]*"(,[a-z_]+="[^"\\]*")*\})? -?[0-9.e+-]+$')
        for line in self.lines():
            self.assertTrue(line.startswith("# HELP ") or line.startswith("# TYPE ") or sample.match(line), line)
        self.assertTrue(self.m.render().endswith("\n"))

    def test_no_label_is_named_service(self):
        self.assertNotIn("service=", self.m.render())

    def test_source_names_come_from_the_configuration(self):
        text = Metrics(["a", "b"]).render()
        self.assertIn('evidence_source_status{source="a"} 0', text)
        self.assertNotIn('source="loki"', text)

    def test_label_values_are_escaped(self):
        m = Metrics(['we"ird\\name'])
        self.assertIn('source="we\\"ird\\\\name"', m.render())


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.log = setup_logging("INFO", "1.2.3", self.stream)

    def last(self):
        return json.loads(self.stream.getvalue().splitlines()[-1])

    def test_format(self):
        log_event(self.log, logging.INFO, "bundle_written", bundle_id="EVD-00001", build_ms=842)
        line = self.last()
        self.assertEqual(list(line)[:6], ["ts", "level", "service", "version", "request_id", "event"])
        self.assertEqual((line["service"], line["version"], line["request_id"], line["event"]), ("evidence", "1.2.3", None, "bundle_written"))
        self.assertEqual((line["bundle_id"], line["build_ms"]), ("EVD-00001", 842))
        self.assertRegex(line["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(line["level"], "INFO")

    def test_every_line_is_one_json_object(self):
        for i in range(3):
            log_event(self.log, logging.INFO, "e", n=i)
        self.assertEqual(len(self.stream.getvalue().splitlines()), 3)

    def test_secrets_in_fields_are_redacted_and_cut(self):
        log_event(self.log, logging.ERROR, "failed", error="connect postgres://app:S3cr3t@postgres/x password=hunter2 " + "x" * 800)
        line = self.last()
        self.assertNotIn("S3cr3t", json.dumps(line))
        self.assertNotIn("hunter2", json.dumps(line))
        self.assertLessEqual(len(line["error"]), 500)

    def test_reserved_names_cannot_be_overwritten(self):
        log_event(self.log, logging.INFO, "e", service="evil", ts="x", level="y", event="z")
        line = self.last()
        self.assertEqual((line["service"], line["event"]), ("evidence", "e"))
        self.assertEqual((line["field_service"], line["field_ts"]), ("evil", "x"))

    def test_level_filtering(self):
        log_event(self.log, logging.DEBUG, "quiet")
        self.assertEqual(self.stream.getvalue(), "")
        setup_logging("DEBUG", "1", self.stream)
        log_event(logging.getLogger("evidence"), logging.DEBUG, "loud")
        self.assertIn("loud", self.stream.getvalue())

    def test_unknown_level_falls_back_to_info_and_non_json_values_are_text(self):
        log = setup_logging("NOPE", "1", self.stream)
        self.assertEqual(log.level, logging.INFO)
        log_event(log, logging.INFO, "e", obj=object(), items=[1, "password=abc"], nested={"k": None})
        line = self.last()
        self.assertIsInstance(line["obj"], str)
        self.assertEqual(line["items"], [1, "password=[REDACTED]"])
        self.assertEqual(line["nested"], {"k": None})


if __name__ == "__main__":
    unittest.main()
