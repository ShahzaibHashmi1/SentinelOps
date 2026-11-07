import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
from _bootstrap import EVIDENCE, TOPOLOGY_PATH

from app import main as mn
from app.graph import GraphError
from app.signals import SignalsConfigError
from app.store import StoreError
from app.topology import TopologyError


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = mn.parse_settings({})
        self.assertEqual((s.prometheus_url, s.loki_url, s.detector_url), ("http://prometheus:9090", "http://loki:3100", "http://detector:9100"))
        self.assertEqual((s.interval_s, s.warmup_s, s.settle_s, s.refresh_s, s.change_poll_s), (5.0, 30.0, 15.0, 60.0, 10.0))
        self.assertEqual((s.db_path, s.topology_path, s.config_dir, s.log_level, s.version, s.port),
                         ("/data/evidence.db", "/app/docs/topology.yaml", "/app/config", "INFO", "dev", 9200))

    def test_all_settings_can_be_given(self):
        s = mn.parse_settings({"PROMETHEUS_URL": "http://p:1", "LOKI_URL": "https://l", "DETECTOR_URL": "http://d:2", "EVIDENCE_INTERVAL_S": "2",
                               "WARMUP_S": "0", "SETTLE_S": "1.5", "REFRESH_S": "30", "CHANGE_POLL_S": "3", "DB_PATH": "/x/e.db", "TOPOLOGY_PATH": "/t",
                               "CONFIG_DIR": "/c", "LOG_LEVEL": "debug", "APP_VERSION": "1.0.0", "EVIDENCE_PORT": "9300"})
        self.assertEqual((s.interval_s, s.warmup_s, s.settle_s, s.refresh_s, s.change_poll_s, s.port, s.log_level), (2.0, 0.0, 1.5, 30.0, 3.0, 9300, "DEBUG"))

    def test_blank_values_use_the_default(self):
        self.assertEqual(mn.parse_settings({"WARMUP_S": " ", "PROMETHEUS_URL": ""}).warmup_s, 30.0)

    def test_bounds_are_inclusive(self):
        for env, low, high in (("EVIDENCE_INTERVAL_S", "1", "60"), ("WARMUP_S", "0", "600"), ("SETTLE_S", "0", "300"), ("REFRESH_S", "5", "3600"), ("CHANGE_POLL_S", "1", "60")):
            for value in (low, high):
                mn.parse_settings({env: value})
            with self.assertRaises(mn.SettingsError):
                mn.parse_settings({env: str(float(low) - 0.5)})
            with self.assertRaises(mn.SettingsError):
                mn.parse_settings({env: str(float(high) + 1)})

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(mn.SettingsError) as ctx:
            mn.parse_settings({"EVIDENCE_INTERVAL_S": "abc", "PROMETHEUS_URL": "prometheus:9090", "LOG_LEVEL": "loud", "EVIDENCE_PORT": "0", "REFRESH_S": "1"})
        text = str(ctx.exception)
        for part in ("EVIDENCE_INTERVAL_S must be a number", "PROMETHEUS_URL", "LOG_LEVEL", "EVIDENCE_PORT", "REFRESH_S must be from 5 to 3600"):
            self.assertIn(part, text)

    def test_bad_urls_and_ports(self):
        for env in ({"LOKI_URL": "ftp://x/y"}, {"DETECTOR_URL": "http://"}, {"EVIDENCE_PORT": "70000"}, {"EVIDENCE_PORT": "x"}):
            with self.subTest(env), self.assertRaises(mn.SettingsError):
                mn.parse_settings(env)

    def test_public_settings_have_no_secret_like_keys(self):
        public = mn.parse_settings({}).public()
        self.assertTrue(all(not any(w in key for w in ("password", "secret", "token")) for key in public))


class WiringCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "config")
        shutil.copytree(os.path.join(EVIDENCE, "config"), self.config)
        self.env = {"DB_PATH": os.path.join(self.tmp.name, "data", "evidence.db"), "TOPOLOGY_PATH": TOPOLOGY_PATH, "CONFIG_DIR": self.config,
                    "EVIDENCE_DOCKER_SOURCE": "off", "WARMUP_S": "0"}
        self.stream = io.StringIO()
        self.logger = mn.setup_logging("INFO", "t", self.stream)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **env):
        merged = dict(self.env, **env)
        return mn.build_runtime(mn.parse_settings(merged), merged, self.logger)


class BuildRuntimeTests(WiringCase):
    def test_wiring(self):
        rt = self.build()
        try:
            self.assertEqual(type(rt.runtime_source).__name__, "DockerSource")  # loaded by name from the configuration
            self.assertEqual(rt.runtime_source.status().state, "off")
            self.assertEqual(rt.ctx.topology.services, ("gateway", "inventory", "orders", "payments", "postgres", "redis"))
            self.assertEqual(rt.ctx.settings["interval_s"], 5.0)
            self.assertTrue(os.path.exists(self.env["DB_PATH"]))
            self.assertEqual(rt.loop.settle_s, 15.0)
        finally:
            rt.collector.close()
            rt.store.close()

    def test_runtime_source_on_without_a_socket_is_no_data_not_an_error(self):
        rt = self.build(EVIDENCE_DOCKER_SOURCE="on", EVIDENCE_DOCKER_SOCKET=os.path.join(self.tmp.name, "none.sock"))
        try:
            self.assertEqual(rt.poller.poll_once(rt.loop.clock.utcnow()), 0)
            self.assertEqual(rt.runtime_source.status().state, "no_data")
        finally:
            rt.collector.close()
            rt.store.close()

    def test_invalid_runtime_switch(self):
        with self.assertRaises(ValueError):
            self.build(EVIDENCE_DOCKER_SOURCE="maybe")

    def test_a_failed_build_leaves_no_database_open(self):
        # an open sqlite file cannot be deleted on Windows: a build that fails (here a bad runtime switch) must not leave one open
        opened = []
        real = sqlite3.connect

        def tracking(*args, **kwargs):
            conn = real(*args, **kwargs)
            opened.append(conn)
            return conn

        with mock.patch("sqlite3.connect", tracking):
            with self.assertRaises(ValueError):
                self.build(EVIDENCE_DOCKER_SOURCE="maybe")
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("select 1")

    def test_missing_or_bad_topology(self):
        with self.assertRaises(TopologyError):
            self.build(TOPOLOGY_PATH=os.path.join(self.tmp.name, "none.yaml"))
        bad = os.path.join(self.tmp.name, "cycle.yaml")
        with open(bad, "w") as fh:
            fh.write("a:\n  - b\nb:\n  - a\n")
        with self.assertRaises(GraphError):
            self.build(TOPOLOGY_PATH=bad)

    def test_bad_configuration(self):
        with open(os.path.join(self.config, "signals.json"), "w") as fh:
            fh.write("{}")
        with self.assertRaises(SignalsConfigError):
            self.build()

    def test_signals_must_name_known_services(self):
        path = os.path.join(self.config, "signals.json")
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["signals"][5]["thresholds"][0]["services"] = ["nope"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        with self.assertRaises(SignalsConfigError):
            self.build()

    def test_unusable_database_path(self):
        blocker = os.path.join(self.tmp.name, "file")
        with open(blocker, "w"):
            pass
        with self.assertRaises(StoreError):
            self.build(DB_PATH=os.path.join(blocker, "sub", "e.db"))


class MainExitCodeTests(WiringCase):
    def run_main(self, **env):
        merged = dict(self.env, **env)
        out = io.StringIO()
        with mock.patch.dict(os.environ, merged, clear=True), contextlib.redirect_stdout(out):
            code = mn.main()
        events = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return code, events

    def test_invalid_settings_exit_2(self):
        code, events = self.run_main(EVIDENCE_INTERVAL_S="abc")
        self.assertEqual((code, events[-1]["event"]), (2, "settings_invalid"))
        self.assertIn("EVIDENCE_INTERVAL_S", events[-1]["error"])

    def test_invalid_runtime_switch_exits_2(self):
        code, events = self.run_main(EVIDENCE_DOCKER_SOURCE="maybe")
        self.assertEqual((code, events[-1]["event"]), (2, "settings_invalid"))

    def test_bad_topology_exits_3(self):
        code, events = self.run_main(TOPOLOGY_PATH=os.path.join(self.tmp.name, "none.yaml"))
        self.assertEqual((code, events[-1]["event"]), (3, "config_invalid"))

    def test_bad_config_exits_3(self):
        with open(os.path.join(self.config, "ranking.json"), "w") as fh:
            fh.write("[]")
        code, events = self.run_main()
        self.assertEqual((code, events[-1]["event"]), (3, "config_invalid"))

    def test_unusable_database_exits_4(self):
        blocker = os.path.join(self.tmp.name, "file")
        with open(blocker, "w"):
            pass
        code, events = self.run_main(DB_PATH=os.path.join(blocker, "sub", "e.db"))
        self.assertEqual((code, events[-1]["event"]), (4, "store_unavailable"))


if __name__ == "__main__":
    unittest.main()
