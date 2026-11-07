import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401
import fakes
import test_bundle
from fakes import GRAPH, RANKING, SIGNALS, T0, TOPOLOGY, at

from app import loop as lp
from app.changes import ChangePoller
from app.logging_setup import setup_logging
from app.metrics import Metrics
from app.model import ChangeRecord, ContainerObservation, fmt_ts
from app.sources import QueryFailed, SourceStatus
from app.store import Store, StoreReader

SETTLE, REFRESH, INTERVAL = 15.0, 60.0, 5.0


class Clock:
    def __init__(self, start=1200.0):
        self.t = start

    def mono(self):
        return self.t

    def utcnow(self):
        return T0 + timedelta(seconds=self.t)

    def advance(self, seconds):
        self.t += seconds


class FakeSource:
    def __init__(self, name, state="ok"):
        self.name, self.state, self.detail = name, state, ""
        self.begins = 0

    def begin_cycle(self, mono, now):
        self.begins += 1

    def status(self):
        return SourceStatus(self.state, None, self.detail)

    def is_ok(self):
        return self.state == "ok"


class FakeDetector(FakeSource):
    def __init__(self):
        super().__init__("detector")
        self.items = []
        self.error = None
        self.calls = 0

    def incidents(self, status=None, limit=50, since=None):
        self.calls += 1
        if self.error:
            raise QueryFailed(self.error)
        return list(self.items)


class FakeCollector:
    def __init__(self):
        self.calls = []
        self.error = None

    def collect(self, window, containers, changes):
        self.calls.append((window, dict(containers), {k: list(v) for k, v in changes.items()}))
        if self.error:
            raise self.error
        services, _hint = fakes.scenario_f10()
        return services


def inc(inc_id="INC-0001", status="open", first=1000, last=1100, active=True, extra_last=None, **kw):
    item = test_bundle.incident(first=first, last=last, status=status, inc_id=inc_id, resolved_at=(last + 65 if status == "resolved" else None), **kw)
    for i, sym in enumerate(item["symptoms"]):
        sym["symptom_id"] = f"{sym['rule_id']}:{sym['service']}"
        sym["active"] = active
        if extra_last:
            sym["last_seen"] = fmt_ts(at(extra_last))
    return item


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "e.db")
        self.store, self.reader = Store(path), StoreReader(path)
        self.clock = Clock()
        self.detector, self.prom, self.loki, self.runtime = FakeDetector(), FakeSource("prometheus"), FakeSource("loki"), FakeSource("docker")
        self.collector = FakeCollector()
        self.metrics = Metrics(SIGNALS.sources)
        self.state = lp.LoopState()
        self.stream = io.StringIO()
        self.logger = setup_logging("DEBUG", "t", self.stream)
        self.poller = None
        self.build_loop()

    def build_loop(self):
        self.loop = lp.BuildLoop(
            detector=self.detector, prom=self.prom, loki=self.loki, runtime=self.runtime, poller=self.poller, collector=self.collector,
            store=self.store, reader=self.reader, topology=TOPOLOGY, graph=GRAPH, signals_cfg=SIGNALS, ranking_cfg=RANKING, metrics=self.metrics,
            state=self.state, logger=self.logger, interval_s=INTERVAL, settle_s=SETTLE, refresh_s=REFRESH, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def cycle(self, advance=INTERVAL):
        self.loop.run_cycle()
        self.clock.advance(advance)

    def triggers(self, incident_id="INC-0001"):
        return [b["trigger"] for b in self.reader.list_bundles(incident_id)]

    def events(self, name):
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if json.loads(line)["event"] == name]


class TriggerTests(LoopCase):
    def test_opened_after_settle_seconds(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()  # t=1200: first seen
        self.assertEqual(self.triggers(), [])
        self.clock.advance(14)
        self.loop.run_cycle()
        self.assertEqual(self.triggers(), [])
        self.clock.advance(1)  # exactly SETTLE_S after the first sighting
        self.loop.run_cycle()
        self.assertEqual(self.triggers(), ["opened"])
        for _ in range(3):
            self.cycle()
        self.assertEqual(self.triggers(), ["opened"])  # not again

    def test_opened_bundle_is_valid_and_complete(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        bundle = self.reader.latest_bundle("INC-0001")
        self.assertEqual(bundle["bundle_id"], "EVD-00001")
        self.assertEqual(bundle["trigger"], "opened")
        self.assertEqual(bundle["suspects"][0]["service"], "postgres")
        self.assertEqual(list(bundle["sources"]), list(SIGNALS.sources))
        self.assertEqual(bundle["window"]["start"], fmt_ts(at(880)))
        self.assertEqual(bundle["window"]["end"], fmt_ts(at(1215)))
        self.assertGreaterEqual(bundle["build_ms"], 0)

    def test_refresh_needs_refresh_seconds_and_a_change(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()  # opened at t=1215
        for _ in range(30):  # 150 s with an unchanged incident
            self.cycle()
        self.assertEqual(self.triggers(), ["opened"])
        self.detector.items = [inc(extra_last=1300)]  # a last_seen moved
        self.cycle()
        self.assertEqual(self.triggers(), ["opened", "refresh"])
        self.detector.items = [inc(extra_last=1400)]
        for _ in range(11):  # 55 s: too early
            self.cycle()
        self.assertEqual(self.triggers(), ["opened", "refresh"])
        self.cycle()  # 60 s since the last bundle
        self.assertEqual(self.triggers(), ["opened", "refresh", "refresh"])

    def test_a_changed_symptom_set_also_counts(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        self.clock.advance(REFRESH)
        extra = [{"rule_id": "R07", "service": "gateway", "role": "effect", "first_seen": fmt_ts(at(1100)), "last_seen": fmt_ts(at(1100)),
                  "symptom_id": "R07:gateway", "active": True}]
        item = inc()
        item["symptoms"].extend(extra)
        self.detector.items = [item]
        self.loop.run_cycle()
        self.assertEqual(self.triggers(), ["opened", "refresh"])

    def test_resolved_once(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        self.detector.items = [inc(status="resolved", active=False)]
        self.cycle()
        self.assertEqual(self.triggers(), ["opened", "resolved"])
        for _ in range(4):
            self.cycle()
        self.assertEqual(self.triggers(), ["opened", "resolved"])

    def test_resolved_before_settle_still_gets_a_resolved_bundle(self):
        self.detector.items = [inc()]
        self.cycle()
        self.detector.items = [inc(status="resolved", active=False)]
        self.cycle()
        self.assertEqual(self.triggers(), ["resolved"])

    def test_old_resolved_history_is_never_back_filled(self):
        self.detector.items = [inc(status="resolved", active=False)]
        for _ in range(3):
            self.cycle()
        self.assertEqual(self.triggers(), [])
        self.assertEqual(len(self.events("incident_skipped_history")), 1)

    def test_reopened_incident_is_looked_at_again(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        self.detector.items = [inc(status="resolved", active=False)]
        self.cycle()  # the resolved bundle is written
        self.cycle()  # a further cycle marks the finished incident as done
        self.assertTrue(self.loop.tracks["INC-0001"].skipped)
        self.detector.items = [inc(extra_last=1500)]  # the detector reopened it
        self.clock.advance(REFRESH)
        self.loop.run_cycle()
        self.assertFalse(self.loop.tracks["INC-0001"].skipped)
        self.assertEqual(self.triggers(), ["opened", "resolved", "refresh"])

    def test_two_builds_per_cycle_at_most(self):
        self.detector.items = [inc(f"INC-000{i}") for i in range(1, 4)]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        total = sum(len(self.triggers(f"INC-000{i}")) for i in range(1, 4))
        self.assertEqual(total, 2)
        self.cycle()
        self.assertEqual(sum(len(self.triggers(f"INC-000{i}")) for i in range(1, 4)), 3)

    def test_bundle_ids_are_unique_across_incidents(self):
        self.detector.items = [inc("INC-0001"), inc("INC-0002")]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        ids = [b["bundle_id"] for i in ("INC-0001", "INC-0002") for b in self.reader.list_bundles(i)]
        self.assertEqual(sorted(ids), ["EVD-00001", "EVD-00002"])

    def test_restart_does_not_repeat_opened_and_refreshes_by_stored_age(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        self.build_loop()  # a new process: no memory
        self.clock.advance(10)
        self.loop.run_cycle()
        self.assertEqual(self.triggers(), ["opened"])  # the last bundle is only 10 s old
        self.clock.advance(REFRESH)
        self.loop.run_cycle()  # unknown signature counts as changed
        self.assertEqual(self.triggers(), ["opened", "refresh"])


class SourceTests(LoopCase):
    def ready(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)

    def test_prometheus_not_ok_defers_the_build_once_and_resumes(self):
        self.ready()
        self.prom.state = "warming_up"
        for _ in range(3):
            self.cycle()
        self.assertEqual(self.triggers(), [])
        self.assertEqual(len(self.events("build_deferred")), 1)
        self.assertEqual(self.collector.calls, [])
        self.prom.state = "ok"
        self.cycle()
        self.assertEqual(self.triggers(), ["opened"])

    def test_loki_and_runtime_not_ok_still_build_with_gaps_in_the_sources_section(self):
        self.loki.state, self.runtime.state = "no_data", "off"
        self.runtime.detail = "disabled by EVIDENCE_DOCKER_SOURCE=off"
        self.ready()
        self.loop.run_cycle()
        bundle = self.reader.latest_bundle("INC-0001")
        self.assertEqual(bundle["sources"]["loki"]["state"], "no_data")
        self.assertEqual(bundle["sources"]["docker"], {"state": "off", "detail": "disabled by EVIDENCE_DOCKER_SOURCE=off"})
        self.assertEqual({g["source"] for g in bundle["data_gaps"]}, {"loki", "docker"})

    def test_detector_not_ok_makes_no_call(self):
        self.detector.state = "no_data"
        self.cycle()
        self.assertEqual(self.detector.calls, 0)

    def test_incident_list_failure_is_logged_not_fatal(self):
        self.detector.error = "HTTP 500"
        self.cycle()
        self.assertEqual(len(self.events("incidents_unavailable")), 1)
        self.assertEqual(self.state.snapshot().cycles, 1)
        self.detector.error = None
        self.detector.items = [inc()]
        self.cycle()
        self.assertEqual(self.state.snapshot().cycles, 2)

    def test_begin_cycle_is_called_for_the_three_http_sources(self):
        self.cycle()
        self.assertEqual((self.detector.begins, self.prom.begins, self.loki.begins), (1, 1, 1))
        self.assertEqual(self.runtime.begins, 0)


class FailureTests(LoopCase):
    def ready(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)

    def test_a_failed_build_is_logged_retried_later_and_does_not_stop_the_loop(self):
        self.ready()
        self.collector.error = QueryFailed("boom")
        self.loop.run_cycle()
        self.assertEqual(self.triggers(), [])
        self.assertEqual(len(self.events("bundle_failed")), 1)
        self.collector.error = None
        self.cycle()  # inside the retry pause
        self.assertEqual(self.triggers(), [])
        self.clock.advance(lp.RETRY_AFTER_FAILURE_S)
        self.loop.run_cycle()
        self.assertEqual(self.triggers(), ["opened"])

    def test_one_bad_incident_does_not_block_the_others(self):
        good = inc("INC-0002")
        bad = inc("INC-0001")
        bad["symptoms"] = []
        self.detector.items = [bad, good]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        self.assertEqual(self.triggers("INC-0002"), ["opened"])
        self.assertEqual(self.triggers("INC-0001"), [])

    def test_an_unexpected_error_in_the_cycle_is_logged_and_counted(self):
        self.ready()
        self.collector.error = RuntimeError("unexpected")
        self.loop.run_cycle()
        self.assertEqual(self.state.snapshot().cycles, 2)
        failed = self.events("bundle_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("RuntimeError", failed[0]["error"])
        self.assertEqual(self.events("cycle_failed"), [])
        self.collector.error = None
        self.cycle()  # paused after the failure
        self.assertEqual(self.triggers(), [])

    def test_an_error_outside_the_build_is_a_failed_cycle_not_a_dead_loop(self):
        self.ready()
        self.detector.incidents = lambda **kw: (_ for _ in ()).throw(RuntimeError("detector client bug"))
        self.loop.run_cycle()
        self.assertEqual(len(self.events("cycle_failed")), 1)
        self.assertEqual(self.state.snapshot().cycles, 2)

    def test_garbage_incident_objects_are_skipped(self):
        self.detector.items = [{"incident_id": 5}, {"status": "open"}, {"incident_id": "INC-0001", "status": "weird"}]
        self.cycle()
        self.assertEqual(self.reader.counts()["total"], 0)

    def test_a_late_cycle_is_logged(self):
        original = self.collector.collect

        def slow(window, containers, changes):
            self.clock.advance(7)
            return original(window, containers, changes)

        self.collector.collect = slow
        self.ready()
        self.loop.run_cycle()
        self.assertEqual(len(self.events("cycle_late")), 1)


class ChangeFlowTests(LoopCase):
    def setUp(self):
        super().setUp()

        class Src:
            def __init__(self):
                self.obs = []

            def observe(self, now):
                return self.obs

            def status(self):
                return SourceStatus("ok")

        self.src = Src()
        self.poller = ChangePoller(self.src, self.metrics, self.logger)
        self.runtime = self.src
        self.build_loop()

    def test_change_records_are_written_by_the_loop_thread(self):
        o = ContainerObservation("orders", created="2026-11-03T09:00:00Z", status="running", restart_count=0)
        self.src.obs = [o]
        self.poller.poll_once(self.clock.utcnow())
        self.src.obs = [ContainerObservation("orders", created="2026-11-03T09:00:00Z", status="running", restart_count=1,
                                             started_at=fmt_ts(at(1195)))]
        self.poller.poll_once(self.clock.utcnow())
        self.cycle()
        rows = self.reader.list_changes()
        self.assertEqual([(r["service"], r["kind"]) for r in rows], [("orders", "restarted")])

    def test_stored_changes_reach_the_collector_and_the_snapshot_too(self):
        self.src.obs = [ContainerObservation("orders", created="2026-11-03T09:00:00Z", status="running", restart_count=0)]
        self.poller.poll_once(self.clock.utcnow())
        self.src.obs = [ContainerObservation("orders", created="2026-11-03T09:00:00Z", status="running", restart_count=2,
                                             started_at=fmt_ts(at(1190)))]
        self.poller.poll_once(self.clock.utcnow())
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        window, containers, changes = self.collector.calls[-1]
        self.assertEqual([c.kind for c in changes["orders"]], ["restarted"])
        self.assertEqual(containers["orders"].restart_count, 2)

    def test_a_refused_write_requeues_the_records(self):
        self.poller.requeue([ChangeRecord("orders", "restarted", at(1000), {}, {})])
        original = self.store.add_changes

        def broken(records):
            raise OSError("disk full")

        self.store.add_changes = broken
        self.cycle()
        self.assertEqual(len(self.events("store_write_failed")), 1)
        self.store.add_changes = original
        self.cycle()
        self.assertEqual(len(self.reader.list_changes()), 1)


class StateTests(LoopCase):
    def test_state_metrics_and_sources_after_a_cycle(self):
        self.loki.state = "no_data"
        self.cycle()
        snap = self.state.snapshot()
        self.assertEqual(snap.cycles, 1)
        self.assertEqual(snap.sources["loki"].state, "no_data")
        self.assertEqual(snap.sources["topology"].state, "ok")
        self.assertEqual(list(snap.sources), list(SIGNALS.sources))
        text = self.metrics.render()
        self.assertIn("evidence_build_cycles_total 1", text)
        self.assertIn('evidence_source_status{source="loki"} 0', text)
        self.assertIn('evidence_source_status{source="prometheus"} 1', text)

    def test_bundle_metrics(self):
        self.detector.items = [inc()]
        self.loop.run_cycle()
        self.clock.advance(SETTLE)
        self.loop.run_cycle()
        self.assertIn('evidence_bundles_total{trigger="opened"} 1', self.metrics.render())

    def test_open_incidents_tracked(self):
        self.detector.items = [inc("INC-0001"), inc("INC-0002", status="resolved", active=False)]
        self.cycle()
        self.assertEqual(self.state.snapshot().open_incidents_tracked, 1)

    def test_signature(self):
        a = lp.signature(inc())
        self.assertEqual(a, lp.signature(inc()))
        self.assertNotEqual(a, lp.signature(inc(extra_last=1500)))
        self.assertNotEqual(a, lp.signature(inc(active=False)))
        self.assertEqual(lp.signature({}), ((), ""))

    def test_to_change_record(self):
        row = {"service": "orders", "kind": "recreated", "ts": "2026-11-03T10:00:00.000Z", "old": {}, "new": {"created": "x"}}
        self.assertEqual(lp.to_change_record(row).kind, "recreated")
        self.assertIsNone(lp.to_change_record({"service": "orders"}))
        self.assertIsNone(lp.to_change_record(dict(row, kind="exploded")))

    def test_run_thread_stops(self):
        loop = self.loop
        loop.interval_s = 0.01

        class Real:
            def mono(self):
                return time.monotonic()

            def utcnow(self):
                return datetime.now(timezone.utc)

        loop.clock = Real()
        stop = threading.Event()
        thread = threading.Thread(target=loop.run, args=(stop,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while self.state.snapshot().cycles < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(self.state.snapshot().cycles, 3)


if __name__ == "__main__":
    unittest.main()
