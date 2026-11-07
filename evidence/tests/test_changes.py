import io
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401

from app import changes as ch
from app.logging_setup import setup_logging
from app.metrics import Metrics
from app.model import ChangeRecord, ContainerObservation, fmt_ts

NOW = datetime(2026, 11, 3, 10, 0, 12, tzinfo=timezone.utc)
OLD = "2026-11-03T09:00:00.000000000Z"


def obs(service="orders", **kw):
    base = dict(service=service, created=OLD, status="running", started_at="2026-11-03T09:00:01Z", finished_at="0001-01-01T00:00:00Z",
                restart_count=0, health="healthy", config_hash="a" * 12)
    base.update(kw)
    return ContainerObservation(**base)


def kinds(records):
    return [r.kind for r in records]


class OwnTimeTests(unittest.TestCase):
    def test_uses_the_container_time(self):
        self.assertEqual(fmt_ts(ch.own_time("2026-11-03T10:00:03.5Z", NOW)), "2026-11-03T10:00:03.500Z")
        self.assertEqual(fmt_ts(ch.own_time("2026-11-03T10:00:03.123456789Z", NOW)), "2026-11-03T10:00:03.123Z")

    def test_fallbacks_to_now(self):
        for value in (None, "", "garbage", "0001-01-01T00:00:00Z", "1999-12-31T23:59:59Z", "2026-11-03T10:05:00Z"):
            with self.subTest(value):
                self.assertEqual(ch.own_time(value, NOW), NOW)

    def test_small_clock_skew_is_accepted(self):
        self.assertEqual(fmt_ts(ch.own_time("2026-11-03T10:00:40Z", NOW)), "2026-11-03T10:00:40.000Z")


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.d = ch.ChangeDetector()
        self.d.update([obs()], NOW)

    def test_first_observation_gives_nothing(self):
        self.assertEqual(ch.ChangeDetector().update([obs(), obs("payments")], NOW), [])

    def test_no_change_gives_nothing(self):
        self.assertEqual(self.d.update([obs()], NOW), [])

    def test_recreated_takes_its_time_from_created_not_from_the_poll(self):
        new = obs(created="2026-11-03T10:00:03.500000000Z", started_at="2026-11-03T10:00:04Z")
        records = self.d.update([new], NOW)
        self.assertEqual(kinds(records), ["recreated"])
        self.assertEqual(fmt_ts(records[0].ts), "2026-11-03T10:00:03.500Z")  # NOW is 8.5 s later
        self.assertEqual(records[0].old, {"created": OLD, "status": "running"})
        self.assertEqual(records[0].new, {"created": "2026-11-03T10:00:03.500000000Z", "status": "running"})

    def test_recreated_with_a_new_config_hash_adds_config_changed_with_the_same_time(self):
        new = obs(created="2026-11-03T10:00:03Z", config_hash="b" * 12)
        records = self.d.update([new], NOW)
        self.assertEqual(sorted(kinds(records)), ["config_changed", "recreated"])
        self.assertEqual(records[0].ts, records[1].ts)
        cfg = [r for r in records if r.kind == "config_changed"][0]
        self.assertEqual((cfg.old, cfg.new), ({"config_hash": "a" * 12}, {"config_hash": "b" * 12}))

    def test_recreate_ignores_other_differences(self):
        new = obs(created="2026-11-03T10:00:03Z", restart_count=0, health="starting", status="running")
        self.assertEqual(kinds(self.d.update([new], NOW)), ["recreated"])

    def test_stopped_uses_finished_at(self):
        records = self.d.update([obs(status="exited", finished_at="2026-11-03T10:00:09.25Z")], NOW)
        self.assertEqual(kinds(records), ["stopped"])
        self.assertEqual(fmt_ts(records[0].ts), "2026-11-03T10:00:09.250Z")
        self.assertEqual((records[0].old, records[0].new), ({"status": "running"}, {"status": "exited"}))

    def test_stopped_without_a_usable_finish_time_uses_now(self):
        self.assertEqual(self.d.update([obs(status="dead")], NOW)[0].ts, NOW)

    def test_started_uses_started_at(self):
        self.d.update([obs(status="exited", finished_at="2026-11-03T10:00:01Z")], NOW)
        records = self.d.update([obs(status="running", started_at="2026-11-03T10:00:07.5Z")], NOW)
        self.assertEqual(kinds(records), ["started"])
        self.assertEqual(fmt_ts(records[0].ts), "2026-11-03T10:00:07.500Z")

    def test_restarted_counts_the_restart_count(self):
        records = self.d.update([obs(restart_count=2, started_at="2026-11-03T10:00:06Z")], NOW)
        self.assertEqual(kinds(records), ["restarted"])
        self.assertEqual(fmt_ts(records[0].ts), "2026-11-03T10:00:06.000Z")
        self.assertEqual((records[0].old, records[0].new), ({"restart_count": 0}, {"restart_count": 2}))

    def test_restart_count_going_down_is_not_a_restart(self):
        self.d.update([obs(restart_count=3)], NOW)
        self.assertEqual(self.d.update([obs(restart_count=0)], NOW), [])

    def test_health_change(self):
        records = self.d.update([obs(health="unhealthy")], NOW)
        self.assertEqual(kinds(records), ["health_changed"])
        self.assertEqual(records[0].ts, NOW)

    def test_config_hash_change_without_recreate(self):
        records = self.d.update([obs(config_hash="c" * 12)], NOW)
        self.assertEqual(kinds(records), ["config_changed"])
        self.assertEqual(records[0].ts, NOW)

    def test_paused_and_restarting_make_no_status_record(self):
        for status in ("paused", "restarting"):
            self.assertEqual(self.d.update([obs(status=status)], NOW), [], status)
            self.d.update([obs()], NOW)

    def test_missing_values_make_no_record(self):
        self.assertEqual(self.d.update([ContainerObservation("orders")], NOW), [])

    def test_a_service_that_disappears_and_returns_recreated_is_detected(self):
        self.d.update([obs("payments")], NOW)
        self.d.update([], NOW)  # nothing observed for a while
        records = self.d.update([obs(created="2026-11-03T10:00:03Z")], NOW)
        self.assertEqual(kinds(records), ["recreated"])

    def test_records_are_sorted_by_time(self):
        self.d.update([obs("payments")], NOW)
        records = self.d.update([obs("payments", status="exited", finished_at="2026-11-03T10:00:09Z"),
                                 obs(created="2026-11-03T10:00:03Z")], NOW)
        self.assertEqual([(r.service, r.kind) for r in records], [("orders", "recreated"), ("payments", "stopped")])

    def test_records_only_hold_allow_listed_fields_and_short_hashes(self):
        records = self.d.update([obs(created="2026-11-03T10:00:03Z", config_hash="d" * 12)], NOW)
        for rec in records:
            for side in (rec.old, rec.new):
                self.assertLessEqual(set(side), {"created", "status", "restart_count", "config_hash", "health"})
                self.assertLessEqual(len(side.get("config_hash", "")), 12)
        with self.assertRaises(Exception):
            ChangeRecord("orders", "config_changed", NOW, {"config_hash": "e" * 64}, {})


class FakeSource:
    def __init__(self):
        self.script = []
        self.calls = 0
        self.raise_next = False

    def observe(self, now):
        self.calls += 1
        if self.raise_next:
            raise RuntimeError("boom")
        return self.script.pop(0) if self.script else []


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.src = FakeSource()
        self.metrics = Metrics(["detector"])
        self.stream = io.StringIO()
        self.log = setup_logging("DEBUG", "t", self.stream)
        self.poller = ch.ChangePoller(self.src, self.metrics, self.log)

    def test_first_poll_no_records_then_a_change(self):
        self.src.script = [[obs()], [obs(restart_count=1)]]
        self.assertEqual(self.poller.poll_once(NOW), 0)
        self.assertEqual(self.poller.poll_once(NOW), 1)
        records = self.poller.drain()
        self.assertEqual(kinds(records), ["restarted"])
        self.assertEqual(self.poller.drain(), [])

    def test_metrics_and_log_per_record(self):
        self.src.script = [[obs()], [obs(restart_count=1, health="unhealthy")]]
        self.poller.poll_once(NOW)
        self.poller.poll_once(NOW)
        text = self.metrics.render()
        self.assertIn('evidence_changes_total{kind="restarted"} 1', text)
        self.assertIn('evidence_changes_total{kind="health_changed"} 1', text)
        events = [json.loads(line) for line in self.stream.getvalue().splitlines()]
        self.assertEqual(sorted(e["kind"] for e in events if e["event"] == "change_detected"), ["health_changed", "restarted"])

    def test_snapshot_is_the_latest_observation_and_empty_when_unavailable(self):
        self.src.script = [[obs(), obs("payments")], None]
        self.poller.poll_once(NOW)
        self.assertEqual(sorted(self.poller.snapshot()), ["orders", "payments"])
        self.poller.poll_once(NOW)
        self.assertEqual(self.poller.snapshot(), {})  # never stale data

    def test_records_survive_a_gap_in_observations(self):
        self.src.script = [[obs()], None, [obs(created="2026-11-03T10:00:03Z")]]
        for _ in range(3):
            self.poller.poll_once(NOW)
        self.assertEqual(kinds(self.poller.drain()), ["recreated"])

    def test_requeue_keeps_order_and_goes_first(self):
        a = ChangeRecord("orders", "recreated", NOW, {}, {})
        b = ChangeRecord("orders", "stopped", NOW, {}, {})
        c = ChangeRecord("orders", "started", NOW, {}, {})
        self.poller.requeue([a, b])
        self.poller.requeue([c])
        self.assertEqual(self.poller.drain(), [c, a, b])

    def test_the_pending_queue_is_bounded(self):
        self.poller.requeue([ChangeRecord("orders", "restarted", NOW + timedelta(seconds=i), {}, {}) for i in range(ch.MAX_PENDING + 50)])
        self.assertEqual(len(self.poller.drain()), ch.MAX_PENDING)

    def test_run_loop_polls_repeatedly_survives_errors_and_stops(self):
        stop = threading.Event()
        self.src.raise_next = True
        thread = threading.Thread(target=self.poller.run, args=(stop, 0.01), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while self.src.calls < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.src.raise_next = False
        calls_after_error = self.src.calls
        while self.src.calls < calls_after_error + 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(self.src.calls, calls_after_error + 2)
        self.assertIn("change_poll_failed", self.stream.getvalue())


if __name__ == "__main__":
    unittest.main()
