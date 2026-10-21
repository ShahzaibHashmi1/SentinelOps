"""The eight M3 faults and the baseline, replayed through the REAL rules.json, the real
ThresholdDetector and the real Correlator, with fake sources (no network, no Docker).
The numbers are the ones the owner measured (docs/CHAOS.md, owner's runs)."""
import unittest

import _bootstrap  # noqa: F401
from fakes import ALL6, APP4, Harness, by, scalar
from app.model import OPEN, RESOLVED
from app.sources import SOURCE_NO_DATA, SOURCE_WARMING_UP, Sample

TRAFFIC10 = by("service", {"gateway": 10.0})
ALL_TRAFFIC = by("service", {s: 5.0 for s in APP4})


def rule_ids(incident):
    return {s.rule_id for s in incident.symptoms}


def inject(h, apply, seconds=90):
    """60 s healthy, then the fault. Returns (incident, seconds from the inject to opened_at)."""
    h.run(60)
    assert h.incident is None
    at = h.clock.utc
    apply(h)
    h.run(seconds)
    inc = h.incident
    return inc, (inc.opened_at - at).total_seconds()


def recover(h, seconds=120):
    h.healthy()
    h.run(seconds)


def f10(h):
    h.set("R02", by("service", {s: 1 for s in ALL6 if s != "postgres"}), guard=scalar(1))
    h.set("R05", by("service", {"gateway": 9.19}), traffic=TRAFFIC10)
    h.set("R06", [Sample({"job": "orders", "dependency": "postgres"}, 0.5),
                  Sample({"job": "inventory", "dependency": "postgres"}, 0.3)])
    h.set("R07", by("service", {"gateway": 2.1, "orders": 2.0, "inventory": 2.0, "payments": 0.03}), traffic=ALL_TRAFFIC)


def f13(h):
    h.set("R02", by("service", {s: 1 for s in ALL6 if s != "redis"}), guard=scalar(1))
    h.set("R06", [Sample({"job": "inventory", "dependency": "redis"}, 0.5),
                  Sample({"job": "payments", "dependency": "redis"}, 0.2)])
    # the gateway 5xx % stays 0 (docs/CHAOS.md F13)


def f08(h):
    h.set("R06", [Sample({"job": "orders", "dependency": "postgres"}, 0.18)])
    h.set("R05", by("service", {"gateway": 6.26}), traffic=TRAFFIC10)
    h.set("R09", by("service", {"orders": 12}))


def f11(h):
    h.set("R07", by("service", {"inventory": 1.507, "gateway": 0.6}), traffic=ALL_TRAFFIC)
    h.set("R05", by("service", {"gateway": 8.864}), traffic=TRAFFIC10)


def f18(h):
    h.set("R08", by("service", {"gateway": 31.77}))
    h.set("R07", by("service", {"gateway": 0.955}), traffic=by("service", {s: 30.0 for s in APP4}))
    h.set("R03", by("service", {**{s: 15 for s in ALL6}, "gateway": 94.0}))


def f06(h):
    h.set("R01", by("job", {**{s: 1 for s in APP4}, "inventory": 0}))
    h.set("R09", by("service", {"inventory": 2}))
    h.set("R05", by("service", {"gateway": 17.7}), traffic=TRAFFIC10)


def f01(h):
    h.set("R03", by("service", {**{s: 15 for s in ALL6}, "orders": 90.92}))


def f02(h):
    h.set("R04", by("service", {**{s: 25 for s in ALL6}, "payments": 83.54}))


class BaselineTests(unittest.TestCase):
    def test_ten_minutes_of_normal_load_raise_nothing(self):
        h = Harness()
        h.set("R03", by("service", {"gateway": 40, "orders": 44, "inventory": 30, "payments": 20, "postgres": 6, "redis": 3}))
        h.set("R04", by("service", {**{s: 26 for s in ALL6}, "payments": 33.4}))
        h.set("R07", by("service", {"gateway": 0.203, "orders": 0.18, "payments": 0.1, "inventory": 0.01}), traffic=ALL_TRAFFIC)
        h.set("R08", by("service", {"gateway": 18.97}))
        h.run(600)
        self.assertIsNone(h.incident)
        self.assertEqual(h.events, [])

    def test_idle_stack_raises_nothing_and_records_no_gap(self):
        h = Harness()
        idle = by("service", {s: 0.0 for s in APP4})
        h.set("R05", by("service", {"gateway": 100.0}), traffic=by("service", {"gateway": 0.2}))   # 1 failed request
        h.set("R07", [Sample({"service": s}, float("nan")) for s in APP4], traffic=idle)            # histogram of nothing
        h.set("R08", by("service", {"gateway": 0.0}))
        h.run(300)
        self.assertIsNone(h.incident)
        self.assertEqual(h.events, [])

    def test_values_exactly_at_the_thresholds_do_not_fire(self):
        h = Harness()
        h.set("R03", by("service", {**{s: 15 for s in ALL6}, "orders": 85.0}))
        h.set("R04", by("service", {**{s: 25 for s in ALL6}, "payments": 80.0}))
        h.set("R05", by("service", {"gateway": 5.0}), traffic=TRAFFIC10)
        h.set("R06", [Sample({"job": "orders", "dependency": "redis"}, 0.1)])
        h.set("R07", by("service", {"gateway": 0.5}), traffic=ALL_TRAFFIC)
        h.set("R08", by("service", {"gateway": 30.0}))
        h.run(120)
        self.assertIsNone(h.incident)

    def test_prometheus_restart_with_missing_series_raises_nothing(self):
        h = Harness()
        h.run(30)
        h.prom.state = SOURCE_WARMING_UP          # restarted: the client reports warming_up for the warm-up time
        h.set("R01", [])
        h.set("R02", [], guard=[])
        h.run(30)
        h.prom.state = "ok"
        h.healthy()
        h.run(120)
        self.assertIsNone(h.incident)

    def test_cadvisor_down_is_no_data_not_a_missing_container(self):
        h = Harness()
        h.run(30)
        h.set("R02", [], guard=scalar(0))          # up{job="cadvisor"} is 0
        h.run(120)
        self.assertIsNone(h.incident)

    def test_loki_not_ready_raises_nothing(self):
        h = Harness()
        h.loki.state = SOURCE_NO_DATA              # /ready answers 503 for minutes after a start
        h.run(300)
        self.assertIsNone(h.incident)


class FaultTests(unittest.TestCase):
    def check(self, apply, expected_ids, primary, candidates=(), absent=(), max_delay=15):
        h = Harness()
        inc, delay = inject(h, apply)
        self.assertIsNotNone(inc)
        self.assertEqual(inc.incident_id, "INC-0001")
        self.assertEqual(h.correlator.next_incident_seq, 2, "exactly one incident")
        self.assertGreaterEqual(delay, 10)
        self.assertLessEqual(delay, max_delay)
        self.assertTrue(set(expected_ids) <= rule_ids(inc), (expected_ids, rule_ids(inc)))
        for rid in absent:
            self.assertNotIn(rid, rule_ids(inc))
        self.assertEqual(inc.primary_service, primary)
        for name in candidates:
            self.assertIn(name, inc.candidate_services)
        self.assertEqual(inc.status, OPEN)
        recover(h)
        self.assertEqual(h.incident.status, RESOLVED)
        self.assertEqual(h.correlator.next_incident_seq, 2)
        self.assertFalse(any(s.active for s in h.incident.symptoms))
        return h

    def test_f10_postgres_down(self):
        self.check(f10, {"R02", "R05"}, "postgres", candidates=["postgres"])

    def test_f13_redis_down_gateway_stays_green(self):
        h = self.check(f13, {"R02", "R06"}, "redis", candidates=["redis"], absent=["R05"])
        self.assertIn("redis", h.incident.candidate_services)

    def test_f08_invalid_credential_has_no_primary(self):
        self.check(f08, {"R06", "R05"}, None, candidates=["orders", "postgres"])

    def test_f11_lock_contention_has_no_primary(self):
        self.check(f11, {"R07", "R05"}, None, candidates=["inventory", "gateway"])

    def test_f18_surge_has_no_primary_and_names_the_gateway(self):
        h = self.check(f18, {"R08", "R07"}, None, candidates=["gateway"])
        self.assertGreaterEqual(len(rule_ids(h.incident)), 2)
        self.assertIn("R03", rule_ids(h.incident))     # the saturated gateway CPU is part of the same incident

    def test_f06_missing_variable_names_inventory_and_attaches_log_evidence(self):
        h = self.check(f06, {"R01", "R09"}, "inventory", candidates=["inventory"])
        evidence = [s for s in h.incident.symptoms if s.rule_id == "R09"][0]
        self.assertEqual((evidence.role, evidence.opens_incident, evidence.source), ("evidence", False, "loki"))

    def test_f01_cpu_saturation_names_orders(self):
        self.check(f01, {"R03"}, "orders", candidates=["orders"])

    def test_f02_memory_growth_names_payments(self):
        self.check(f02, {"R04"}, "payments", candidates=["payments"])

    def test_stopping_the_whole_stack_is_a_real_outage(self):
        h = Harness()
        h.run(30)
        h.set("R01", by("job", {s: 0 for s in APP4}))
        h.run(60)
        inc = h.incident
        self.assertEqual({s.service for s in inc.symptoms if s.rule_id == "R01"}, set(APP4))
        self.assertIsNone(inc.primary_service)

    def test_log_evidence_first_then_metrics(self):
        h = Harness()
        h.run(30)
        h.set("R09", by("service", {"inventory": 2}))
        h.run(10)                                   # qualified, waiting
        self.assertIsNone(h.incident)
        h.set("R01", by("job", {**{s: 1 for s in APP4}, "inventory": 0}))
        h.run(20)
        inc = h.incident
        self.assertEqual(rule_ids(inc), {"R01", "R09"})
        first = {s.rule_id: s.first_seen for s in inc.symptoms}
        self.assertLess(first["R09"], first["R01"])


class SourceOutageDuringIncidentTests(unittest.TestCase):
    def test_prometheus_warm_up_freezes_the_incident_and_records_a_gap(self):
        h = Harness()
        inc, _ = inject(h, f10, seconds=60)
        before = {s.rule_id: (s.active, s.last_seen) for s in inc.symptoms}
        h.prom.state = SOURCE_WARMING_UP
        h.run(90)                                   # longer than the resolve time
        self.assertEqual(h.incident.status, OPEN)
        # frozen: nothing cleared, nothing refreshed (R09/loki rules are not in this incident)
        for s in h.incident.symptoms:
            self.assertEqual((s.active, s.last_seen), before[s.rule_id])
        gaps = [g for g in h.incident.data_gaps if g.source == "prometheus"]
        self.assertEqual(len(gaps), 1)
        self.assertIsNone(gaps[0].end)
        h.prom.state = "ok"
        recover(h, 150)
        self.assertEqual(h.incident.status, RESOLVED)
        self.assertIsNotNone([g for g in h.incident.data_gaps if g.source == "prometheus"][0].end)

    def test_loki_outage_pauses_resolution_but_not_detection(self):
        h = Harness()
        h.loki.state = SOURCE_NO_DATA
        inc, delay = inject(h, f10, seconds=60)
        self.assertIsNotNone(inc)                   # Prometheus rules still open the incident
        self.assertLessEqual(delay, 15)
        h.healthy()
        h.run(150)                                  # metrics are fine, Loki is still down
        self.assertEqual(h.incident.status, OPEN)
        self.assertEqual([g.source for g in h.incident.data_gaps], ["loki"])
        h.loki.state = "ok"
        h.run(90)
        self.assertEqual(h.incident.status, RESOLVED)
        self.assertIsNotNone(h.incident.data_gaps[0].end)

    def test_a_second_fault_inside_the_window_reopens_the_incident(self):
        h = Harness()
        inject(h, f01, seconds=60)
        recover(h, 85)
        self.assertEqual(h.incident.status, RESOLVED)
        f02(h)
        h.run(30)
        self.assertEqual((h.incident.incident_id, h.incident.status, h.incident.reopen_count), ("INC-0001", OPEN, 1))
        self.assertEqual(rule_ids(h.incident), {"R03", "R04"})
        self.assertEqual(h.incident.primary_service, None)    # orders (R03) and payments (R04): two root services


if __name__ == "__main__":
    unittest.main()
