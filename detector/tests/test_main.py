import contextlib
import io
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import _bootstrap  # noqa: F401
from _bootstrap import RULES_JSON
from fakes import ALL6, APP4
from fakehttp import FakeBackend, FakeTransport, vector
from app import main as main_module
from app.logging_setup import configure
from app.main import App, Settings, SettingsError
from app.rules import load_rules
from app.store import Store


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = Settings.from_environ({})
        self.assertEqual((s.prometheus_url, s.loki_url, s.eval_interval_s, s.warmup_s, s.resolve_after_s,
                          s.reopen_within_s, s.db_path, s.log_level),
                         ("http://prometheus:9090", "http://loki:3100", 5.0, 30.0, 60.0, 120.0, "/data/detector.db", "INFO"))

    def test_environment_overrides(self):
        s = Settings.from_environ({"PROMETHEUS_URL": "http://localhost:9090", "LOKI_URL": "https://loki.example:3100",
                                   "EVAL_INTERVAL_S": "2.5", "WARMUP_S": "0", "RESOLVE_AFTER_S": "90",
                                   "REOPEN_WITHIN_S": "0", "DB_PATH": "/tmp/x.db", "LOG_LEVEL": "debug",
                                   "APP_VERSION": "1.2.3"})
        self.assertEqual((s.prometheus_url, s.loki_url, s.eval_interval_s, s.warmup_s, s.resolve_after_s,
                          s.reopen_within_s, s.db_path, s.log_level, s.version),
                         ("http://localhost:9090", "https://loki.example:3100", 2.5, 0.0, 90.0, 0.0, "/tmp/x.db", "DEBUG", "1.2.3"))

    def test_empty_values_fall_back_to_the_default(self):
        self.assertEqual(Settings.from_environ({"EVAL_INTERVAL_S": "  ", "LOG_LEVEL": ""}).eval_interval_s, 5.0)

    def test_all_problems_are_reported_together(self):
        with self.assertRaises(SettingsError) as ctx:
            Settings.from_environ({"EVAL_INTERVAL_S": "0", "WARMUP_S": "-1", "RESOLVE_AFTER_S": "abc",
                                   "REOPEN_WITHIN_S": "nan", "LOG_LEVEL": "TRACE", "PROMETHEUS_URL": "prometheus:9090"})
        text = str(ctx.exception)
        for name in ("EVAL_INTERVAL_S", "WARMUP_S", "RESOLVE_AFTER_S", "REOPEN_WITHIN_S", "LOG_LEVEL", "PROMETHEUS_URL"):
            self.assertIn(name, text)
        self.assertEqual(len(ctx.exception.problems), 6)

    def test_public_settings_have_no_secret_names(self):
        keys = set(Settings().public())
        self.assertEqual(keys, {"prometheus_url", "loki_url", "eval_interval_s", "warmup_s", "resolve_after_s",
                                "reopen_within_s", "db_path", "log_level", "http_timeout_s"})


def healthy_transport():
    rules = load_rules(RULES_JSON)
    prom, loki = FakeBackend(), FakeBackend()
    by = {r.id: r for r in rules}

    def put(backend, rule_id, items, traffic=None, guard=None):
        rule = by[rule_id]
        backend.answers[rule.query] = vector(*items)
        if traffic is not None:
            backend.answers[rule.min_traffic.query] = vector(*traffic)
        if guard is not None:
            backend.answers[rule.guard] = vector(*guard)

    put(prom, "R01", [({"job": s}, 1) for s in APP4])
    put(prom, "R02", [({"service": s}, 1) for s in ALL6], guard=[({}, 1)])
    put(prom, "R03", [({"service": s}, 15) for s in ALL6])
    put(prom, "R04", [({"service": s}, 25) for s in ALL6])
    put(prom, "R05", [({"service": "gateway"}, 0)], traffic=[({"service": "gateway"}, 10)])
    put(prom, "R07", [({"service": s}, 0.02) for s in APP4], traffic=[({"service": s}, 5) for s in APP4])
    put(prom, "R08", [({"service": "gateway"}, 19)])
    return FakeTransport(prometheus=prom, loki=loki), prom, loki


class AppWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stream = io.StringIO()
        self.logger = configure("DEBUG", "test", self.stream)

    def tearDown(self):
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)
        self.tmp.cleanup()

    def get(self, app, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{app.port}{path}", timeout=5) as r:
            return r.status, r.read()

    def test_the_whole_detector_runs_with_fake_servers(self):
        transport, prom, loki = healthy_transport()
        settings = Settings(eval_interval_s=0.05, warmup_s=0.0, db_path=str(Path(self.tmp.name) / "d.db"),
                            prometheus_url="http://prometheus:9090", loki_url="http://loki:3100", version="7.7.7")
        app = App(settings, load_rules(RULES_JSON), Store(settings.db_path), self.logger, transport=transport,
                  port=0, host="127.0.0.1")
        app.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if self.get(app, "/ready")[0] == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.02)
            status, body = self.get(app, "/internal/v1/status")
            data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(data["version"], "7.7.7")
            self.assertEqual(data["sources"]["prometheus"]["state"], "ok")
            self.assertEqual(data["sources"]["loki"]["state"], "ok")
            self.assertEqual({r["state"] for r in data["rules"]}, {"ok"})
            self.assertGreaterEqual(data["cycles"], 1)
            metrics = self.get(app, "/metrics")[1].decode()
            self.assertIn('detector_rule_state{rule_id="R03",subject="orders"} 0', metrics)
            self.assertIn('detector_rule_value{rule_id="R03",subject="orders"} 15.0', metrics)
            self.assertIn('detector_source_status{source="loki"} 1', metrics)
            self.assertEqual(json.loads(self.get(app, "/internal/v1/incidents")[1]), {"incidents": []})
        finally:
            app.stop()

    def test_an_incident_survives_a_restart(self):
        transport, prom, loki = healthy_transport()
        db = str(Path(self.tmp.name) / "d.db")
        settings = Settings(eval_interval_s=0.05, warmup_s=0.0, db_path=db, resolve_after_s=3600.0)
        rules = load_rules(RULES_JSON)
        by = {r.id: r for r in rules}
        prom.answers[by["R01"].query] = vector(*[({"job": s}, 0 if s == "inventory" else 1) for s in APP4])
        app = App(settings, rules, Store(db), self.logger, transport=transport, port=0, host="127.0.0.1")
        # speed up: no wait for the 10 s hold, replace the rule meta of the correlator
        app.correlator._metas["R01"] = app.correlator._metas["R01"].__class__("R01", "target_down", "high",
                                                                              "root_signal", True, 0.0)
        app.start()
        try:
            deadline = time.monotonic() + 5
            incidents = []
            while time.monotonic() < deadline and not incidents:
                incidents = json.loads(self.get(app, "/internal/v1/incidents")[1])["incidents"]
                time.sleep(0.02)
            self.assertEqual(len(incidents), 1)
            self.assertEqual(incidents[0]["primary_service"], "inventory")
        finally:
            app.stop()
        # restart on the same database
        app2 = App(settings, rules, Store(db), self.logger, transport=transport, port=0, host="127.0.0.1")
        self.assertEqual(app2.restored.incident_id, "INC-0001")
        self.assertEqual(app2.restored.status, "open")
        self.assertEqual(app2.correlator.next_incident_seq, 2)
        app2.stop()


class MainFunctionTests(unittest.TestCase):
    def run_main(self, env, **patches):
        stream = io.StringIO()
        logger_out = []
        original = main_module.RULES_PATH
        try:
            for key, value in patches.items():
                setattr(main_module, key, value)
            with contextlib.redirect_stdout(stream):
                code = main_module.main(env)
        finally:
            main_module.RULES_PATH = original
            logger = main_module.logging_setup.logging.getLogger("detector")
            for h in list(logger.handlers):
                logger.removeHandler(h)
        lines = [json.loads(l) for l in stream.getvalue().splitlines() if l.strip()]
        return code, lines

    def test_invalid_settings_exit_2_with_a_json_error_line(self):
        code, lines = self.run_main({"EVAL_INTERVAL_S": "0", "LOG_LEVEL": "LOUD"})
        self.assertEqual(code, 2)
        self.assertEqual(lines[-1]["event"], "settings_invalid")
        self.assertEqual(lines[-1]["level"], "ERROR")
        self.assertEqual(len(lines[-1]["problems"]), 2)

    def test_invalid_rules_file_exit_3_with_the_problems_in_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "rules.json"
            text = RULES_JSON.read_text(encoding="utf-8").replace('"threshold": 85,', '"threshold": 60,')
            bad.write_text(text, encoding="utf-8")
            code, lines = self.run_main({"DB_PATH": str(Path(tmp) / "d.db")}, RULES_PATH=bad)
        self.assertEqual(code, 3)
        self.assertEqual(lines[-1]["event"], "rules_invalid")
        self.assertTrue(any("margin rule violated" in p for p in lines[-1]["problems"]))

    def test_an_unusable_database_path_exit_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, lines = self.run_main({"DB_PATH": str(Path(tmp) / "no" / "such" / "dir" / "d.db")})
        self.assertEqual(code, 4)
        self.assertEqual(lines[-1]["event"], "store_unavailable")


if __name__ == "__main__":
    unittest.main()
