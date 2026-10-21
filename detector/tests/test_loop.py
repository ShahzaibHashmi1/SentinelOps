import io
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from _bootstrap import RULES_JSON
from fakes import ALL6, APP4, Harness, by, scalar
from fakehttp import FakeBackend, FakeTransport, vector
from app.correlator import Correlator
from app.logging_setup import configure
from app.loop import EvaluationLoop, LoopState
from app.metrics import Metrics
from app.model import OPEN, RESOLVED, Detector
from app.rules import load_rules
from app.sources import loki_source, prometheus_source
from app.store import Store, StoreReader
from app.thresholds import ThresholdDetector


class LoopFixture:
    """Real rules, real detector, real correlator, real store (temp file), fake sources and clock."""

    def __init__(self, tmp):
        self.h = Harness()                       # fake sources, detector, correlator, clock
        self.stream = io.StringIO()
        self.logger = configure("DEBUG", "test", self.stream)
        self.store = Store(Path(tmp) / "detector.db")
        self.db = str(Path(tmp) / "detector.db")
        self.state = LoopState()
        self.metrics = Metrics()
        clock = self.h.clock
        self.loop = EvaluationLoop([self.h.detector], [self.h.prom, self.h.loki], self.h.correlator, self.store, 5.0,
                                   self.state, self.metrics, self.logger, mono=lambda: clock.mono,
                                   utc=lambda: clock.utc, wall=lambda: 1760000000.0)

    def cycle(self, n=1):
        for _ in range(n):
            self.loop.run_cycle()
            self.h.clock.advance(5)

    def events(self):
        return [json.loads(l) for l in self.stream.getvalue().splitlines()]

    def incidents(self):
        with StoreReader(self.db) as r:
            return r.list_incidents()

    def close(self):
        self.store.close()
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)


def f10(h):
    h.set("R02", by("service", {s: 1 for s in ALL6 if s != "postgres"}), guard=scalar(1))
    h.set("R05", by("service", {"gateway": 9.19}), traffic=by("service", {"gateway": 10.0}))


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = LoopFixture(self.tmp.name)

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()

    def test_a_cycle_publishes_state_and_metrics(self):
        self.fx.cycle()
        snap = self.fx.state.snapshot()
        self.assertEqual(snap.cycles, 1)
        self.assertEqual(snap.last_cycle_at, self.fx.h.clock.utc.__class__(2026, 10, 18, 12, 0, 0, tzinfo=snap.last_cycle_at.tzinfo))
        self.assertEqual(set(snap.sources), {"prometheus", "loki"})
        self.assertEqual(len(snap.observations), 4 + 6 + 6 + 6 + 1 + 4 + 4 + 1 + 4)   # one per rule and subject
        text = self.fx.metrics.render()
        self.assertIn("detector_evaluation_cycles_total 1", text)
        self.assertIn('detector_source_status{source="prometheus"} 1', text)
        self.assertIn('detector_rule_state{rule_id="R01",subject="gateway"} 0', text)
        self.assertIn('detector_rule_value{rule_id="R01",subject="gateway"} 1.0', text)
        self.assertIn('detector_rule_state{rule_id="R09",subject="orders"} 0', text)
        self.assertIn("detector_incidents_open 0", text)
        self.assertIn("detector_incidents_total 0", text)
        self.assertIn("detector_last_evaluation_timestamp_seconds 1760000000.0", text)

    def test_no_value_means_no_value_series_but_the_state_stays(self):
        h = self.fx.h
        h.set("R05", by("service", {"gateway": 100.0}), traffic=by("service", {"gateway": 0.2}))   # too little traffic
        h.prom.data[h.by_id["R02"].guard] = scalar(0)                                           # cAdvisor down: no_data
        self.fx.cycle()
        text = self.fx.metrics.render()
        self.assertIn('detector_rule_state{rule_id="R05",subject="gateway"} 0', text)
        self.assertNotIn('detector_rule_value{rule_id="R05"', text)
        self.assertIn('detector_rule_state{rule_id="R02",subject="postgres"} -1', text)
        self.assertNotIn('detector_rule_value{rule_id="R02"', text)

    def test_an_incident_goes_through_the_store_and_the_metrics(self):
        f10(self.fx.h)
        self.fx.cycle(3)                                           # R02 held for 10 s at the third cycle
        incidents = self.fx.incidents()
        self.assertEqual([(i.incident_id, i.status, i.primary_service) for i in incidents], [("INC-0001", OPEN, "postgres")])
        text = self.fx.metrics.render()
        self.assertIn("detector_incidents_open 1", text)
        self.assertIn("detector_incidents_total 1", text)
        self.assertIn('detector_rule_state{rule_id="R02",subject="postgres"} 1', text)
        self.assertIn('detector_rule_value{rule_id="R02",subject="postgres"} 0.0', text)
        names = [e["event"] for e in self.fx.events()]
        self.assertIn("incident_opened", names)
        opened = [e for e in self.fx.events() if e["event"] == "incident_opened"][0]
        self.assertEqual((opened["incident_id"], opened["primary_service"], opened["severity"]), ("INC-0001", "postgres", "high"))
        self.fx.h.healthy()
        self.fx.cycle(16)
        self.assertEqual(self.fx.incidents()[0].status, RESOLVED)
        text = self.fx.metrics.render()
        self.assertIn("detector_incidents_open 0", text)
        self.assertIn("detector_incidents_total 1", text)
        self.assertIn("incident_resolved", [e["event"] for e in self.fx.events()])

    def test_a_failed_store_write_is_retried_in_the_next_cycle(self):
        real_save = self.fx.store.save
        calls = {"n": 0}

        def flaky(incidents):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise OSError("disk full")
            return real_save(incidents)

        self.fx.store.save = flaky
        f10(self.fx.h)
        self.fx.cycle(3)                                           # the incident opens, the write fails
        self.assertEqual(self.fx.incidents(), [])
        self.assertTrue(any(e["event"] == "store_write_failed" for e in self.fx.events()))
        self.fx.cycle(2)                                           # third attempt works
        self.assertEqual([i.incident_id for i in self.fx.incidents()], ["INC-0001"])
        saved = self.fx.incidents()[0]
        self.assertEqual(len(saved.symptoms), len(self.fx.h.correlator.incident.symptoms))

    def test_a_failed_write_of_a_final_state_is_retried_although_nothing_changes_any_more(self):
        """The resolve is the last change of an incident: only the retry can still write it."""
        real_save = self.fx.store.save
        calls = {"failed": 0}

        def fail_once_on_resolve(incidents):
            if calls["failed"] == 0 and any(i.status == RESOLVED for i in incidents):
                calls["failed"] += 1
                raise OSError("database is locked")
            return real_save(incidents)

        self.fx.store.save = fail_once_on_resolve
        f10(self.fx.h)
        self.fx.cycle(3)
        self.assertEqual([i.status for i in self.fx.incidents()], [OPEN])
        self.fx.h.healthy()
        self.fx.cycle(16)                                          # the resolve happens and its write fails
        self.assertEqual(calls["failed"], 1)
        self.assertEqual(self.fx.h.correlator.incident.status, RESOLVED)
        self.fx.cycle(2)                                           # nothing changed in these cycles: only the retry writes
        self.assertEqual([i.status for i in self.fx.incidents()], [RESOLVED])

    def test_a_crashing_detector_does_not_stop_the_cycle(self):
        class Broken(Detector):
            name = "broken"

            def evaluate(self, now):
                raise RuntimeError("boom")

        self.fx.loop.detectors.append(Broken())
        self.fx.cycle(2)
        self.assertEqual(self.fx.state.snapshot().cycles, 2)
        self.assertTrue(any(e["event"] == "detector_failed" and e["detector"] == "broken" for e in self.fx.events()))
        self.assertEqual(self.fx.incidents(), [])

    def test_source_state_changes_are_logged_once(self):
        self.fx.cycle()
        self.fx.h.loki.state = "no_data"
        self.fx.h.loki.detail = "readiness answered HTTP 503"
        self.fx.cycle(3)
        changes = [e for e in self.fx.events() if e["event"] == "source_state_changed" and e["source"] == "loki"]
        self.assertEqual([(c["state"], c["previous"]) for c in changes], [("ok", None), ("no_data", "ok")])
        self.assertEqual(changes[-1]["level"], "WARNING")
        self.assertIn("503", changes[-1]["detail"])


class RunForeverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Registered first, so it runs LAST: addCleanup runs in reverse order, and make() registers store.close later.
        # On Windows the directory cannot be deleted while the SQLite file is still open.
        self.addCleanup(self.tmp.cleanup)
        self.stream = io.StringIO()
        self.logger = configure("DEBUG", "test", self.stream)

    def tearDown(self):
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)

    def make(self, interval, detector):
        store = Store(Path(self.tmp.name) / "d.db")
        self.addCleanup(store.close)
        h = Harness()
        loop = EvaluationLoop([detector], [h.prom, h.loki], Correlator([r.meta() for r in h.rules]), store, interval,
                              LoopState(), Metrics(), self.logger)
        return loop

    def test_the_loop_cycles_until_stopped(self):
        h = Harness()
        loop = self.make(0.02, h.detector)
        stop = threading.Event()
        thread = threading.Thread(target=loop.run_forever, args=(stop,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while loop.state.snapshot().cycles < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(loop.state.snapshot().cycles, 4)

    def test_a_late_cycle_skips_ticks_and_is_logged_and_never_overlaps(self):
        running = {"now": 0, "max": 0}

        class Slow(Detector):
            name = "slow"

            def evaluate(self, now):
                running["now"] += 1
                running["max"] = max(running["max"], running["now"])
                time.sleep(0.16)
                running["now"] -= 1
                return []

        loop = self.make(0.05, Slow())
        stop = threading.Event()
        thread = threading.Thread(target=loop.run_forever, args=(stop,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while loop.state.snapshot().cycles < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(3)
        late = [json.loads(l) for l in self.stream.getvalue().splitlines() if '"cycle_late"' in l]
        self.assertTrue(late)
        self.assertGreaterEqual(late[0]["skipped_ticks"], 2)
        self.assertEqual(running["max"], 1)
        self.assertEqual(late[0]["level"], "WARNING")

    def test_missed_ticks_are_skipped_so_the_lateness_does_not_grow(self):
        class Slow(Detector):
            name = "slow"

            def evaluate(self, now):
                time.sleep(0.2)
                return []

        loop = self.make(0.05, Slow())
        stop = threading.Event()
        thread = threading.Thread(target=loop.run_forever, args=(stop,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 8
        while loop.state.snapshot().cycles < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()
        thread.join(3)
        late = [json.loads(l)["late_s"] for l in self.stream.getvalue().splitlines() if '"cycle_late"' in l]
        self.assertGreaterEqual(len(late), 3)
        # a cycle takes 0.2 s for a 0.05 s interval: about 0.15 s late each time. Without skipping the
        # missed ticks the lateness would add up (0.15, 0.30, 0.45, ...).
        self.assertLess(max(late), 0.3, late)

    def test_an_exception_outside_the_detectors_does_not_kill_the_thread(self):
        h = Harness()
        loop = self.make(0.02, h.detector)
        loop.correlator.process = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))
        stop = threading.Event()
        thread = threading.Thread(target=loop.run_forever, args=(stop,), daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())
        stop.set()
        thread.join(2)
        self.assertIn('"cycle_failed"', self.stream.getvalue())


# ---------------------------------------------------------------------------
# The real HttpSource clients with a fake transport: warm-up and no-data end to end
# ---------------------------------------------------------------------------

def to_vector(samples):
    return vector(*[(s.labels, s.value) for s in samples])


class SourceFlowFixture:
    """Real rules + real HttpSource (fake transport) + real correlator and store."""

    WARMUP = 30.0

    def __init__(self, tmp):
        self.rules = load_rules(RULES_JSON)
        self.by_id = {r.id: r for r in self.rules}
        self.prom, self.lokib = FakeBackend(), FakeBackend()
        transport = FakeTransport(prometheus=self.prom, loki=self.lokib)
        self.sources = [prometheus_source("http://prometheus:9090", self.WARMUP, transport=transport),
                        loki_source("http://loki:3100", self.WARMUP, transport=transport)]
        detector = ThresholdDetector(self.rules, {s.name: s for s in self.sources})
        self.correlator = Correlator([r.meta() for r in self.rules])
        self.store = Store(Path(tmp) / "d.db")
        self.db = str(Path(tmp) / "d.db")
        self.stream = io.StringIO()
        self.logger = configure("DEBUG", "test", self.stream)
        self.state, self.metrics = LoopState(), Metrics()
        from fakes import FakeClock
        self.clock = FakeClock()
        self.loop = EvaluationLoop([detector], self.sources, self.correlator, self.store, 5.0, self.state, self.metrics,
                                   self.logger, mono=lambda: self.clock.mono, utc=lambda: self.clock.utc)
        self.healthy()

    def answer(self, rule_id, samples, traffic=None, guard=None):
        rule = self.by_id[rule_id]
        backend = self.prom if rule.source == "prometheus" else self.lokib
        backend.answers[rule.query] = to_vector(samples)
        if traffic is not None:
            backend.answers[rule.min_traffic.query] = to_vector(traffic)
        if guard is not None:
            backend.answers[rule.guard] = to_vector(guard)

    def healthy(self):
        from app.sources import Sample
        self.answer("R01", [Sample({"job": s}, 1.0) for s in APP4])
        self.answer("R02", [Sample({"service": s}, 1.0) for s in ALL6], guard=[Sample({}, 1.0)])
        self.answer("R03", [Sample({"service": s}, 15.0) for s in ALL6])
        self.answer("R04", [Sample({"service": s}, 25.0) for s in ALL6])
        self.answer("R05", [Sample({"service": "gateway"}, 0.0)], traffic=[Sample({"service": "gateway"}, 10.0)])
        self.answer("R06", [])
        self.answer("R07", [Sample({"service": s}, 0.02) for s in APP4], traffic=[Sample({"service": s}, 5.0) for s in APP4])
        self.answer("R08", [Sample({"service": "gateway"}, 19.0)])
        self.answer("R09", [])

    def run(self, seconds):
        for _ in range(int(seconds // 5)):
            self.loop.run_cycle()
            self.clock.advance(5)

    def incidents(self):
        with StoreReader(self.db) as r:
            return r.list_incidents()

    def source_state(self, name):
        return self.state.snapshot().sources[name].state

    def close(self):
        self.store.close()
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)


class SourceFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = SourceFlowFixture(self.tmp.name)

    def tearDown(self):
        self.fx.close()
        self.tmp.cleanup()

    def test_a_healthy_stack_after_the_warmup_raises_nothing(self):
        self.fx.run(30)
        self.assertEqual((self.fx.source_state("prometheus"), self.fx.source_state("loki")), ("warming_up", "warming_up"))
        self.fx.run(120)
        self.assertEqual((self.fx.source_state("prometheus"), self.fx.source_state("loki")), ("ok", "ok"))
        self.assertEqual(self.fx.incidents(), [])

    def test_prometheus_restart_with_missing_series_creates_no_incident(self):
        """Down for 60 s, back with NO series for the first 20 s, warm-up 30 s: the missing series is not a missing container."""
        self.fx.run(120)                                         # normal operation
        self.fx.prom.reachable = False
        self.fx.run(60)
        self.assertEqual(self.fx.source_state("prometheus"), "no_data")
        self.assertIn('detector_source_status{source="prometheus"} 0', self.fx.metrics.render())
        self.fx.prom.reachable = True
        saved = dict(self.fx.prom.answers)
        self.fx.prom.answers.clear()                             # empty results everywhere (no scrape yet)
        self.fx.run(20)
        self.assertEqual(self.fx.source_state("prometheus"), "warming_up")
        self.fx.prom.answers.update(saved)
        self.fx.run(120)
        self.assertEqual(self.fx.source_state("prometheus"), "ok")
        self.assertEqual(self.fx.incidents(), [])
        self.assertIn('detector_source_status{source="prometheus"} 1', self.fx.metrics.render())

    def test_loki_503_for_minutes_creates_no_incident(self):
        self.fx.run(120)
        self.fx.lokib.ready_status = 503
        self.fx.run(180)
        self.assertEqual(self.fx.source_state("loki"), "no_data")
        status = {o.rule_id: o.state for o in self.fx.state.snapshot().observations if o.source == "loki"}
        self.assertEqual(set(status.values()), {"no_data"})
        self.fx.lokib.ready_status = 200
        self.fx.run(120)
        self.assertEqual(self.fx.incidents(), [])

    def test_a_prometheus_outage_during_an_incident_freezes_it(self):
        from app.sources import Sample
        self.fx.run(60)
        self.fx.answer("R02", [Sample({"service": s}, 1.0) for s in ALL6 if s != "postgres"], guard=[Sample({}, 1.0)])
        self.fx.run(30)
        self.assertEqual([i.status for i in self.fx.incidents()], [OPEN])
        self.fx.prom.reachable = False
        self.fx.run(120)                                         # longer than resolve_after_s (60 s)
        incident = self.fx.incidents()[0]
        self.assertEqual(incident.status, OPEN)
        self.assertTrue(any(s.active for s in incident.symptoms))
        self.assertEqual([g.source for g in incident.data_gaps], ["prometheus"])
        self.assertIsNone(incident.data_gaps[0].end)
        self.fx.prom.reachable = True
        self.fx.healthy()
        self.fx.run(200)
        incident = self.fx.incidents()[0]
        self.assertEqual(incident.status, RESOLVED)
        self.assertIsNotNone(incident.data_gaps[0].end)
        self.assertEqual(len(self.fx.incidents()), 1)

    def test_a_stopped_stack_opens_an_incident_only_after_warmup_and_hold(self):
        from app.sources import Sample
        self.fx.answer("R01", [Sample({"job": s}, 0.0) for s in APP4])      # every app target is down from the start
        self.fx.run(35)                                          # 30 s warm-up: nothing may open
        self.assertEqual(self.fx.incidents(), [])
        self.fx.run(20)                                          # warm-up over, then the 10 s hold
        incident = self.fx.incidents()[0]
        self.assertEqual({s.service for s in incident.symptoms if s.rule_id == "R01"}, set(APP4))
        delay = (incident.opened_at - self.fx.state.snapshot().last_cycle_at).total_seconds()
        self.assertLess(abs(delay), 60)

    def test_a_failed_single_query_makes_only_that_rule_no_data(self):
        rule = self.fx.by_id["R03"]
        self.fx.prom.answers[rule.query] = (400, {"status": "error", "error": "bad query"})
        self.fx.run(60)
        states = {}
        for o in self.fx.state.snapshot().observations:
            states.setdefault(o.rule_id, set()).add(o.state)
        self.assertEqual(states["R03"], {"no_data"})
        self.assertEqual(states["R01"], {"ok"})
        self.assertIn('detector_query_errors_total{source="prometheus"} ', self.fx.metrics.render())
        self.assertEqual(self.fx.source_state("prometheus"), "ok")
        self.assertEqual(self.fx.incidents(), [])


if __name__ == "__main__":
    unittest.main()
