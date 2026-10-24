import unittest
from datetime import timedelta

import _bootstrap  # noqa: F401
from fakes import T0, Driver
from app.model import OPEN, RESOLVED, RuleMeta

METAS = [
    RuleMeta("A", "alpha", "high", "root_signal", True, 10),
    RuleMeta("A2", "alpha_two", "medium", "root_signal", True, 10),
    RuleMeta("B", "bravo", "medium", "effect", True, 10),
    RuleMeta("D", "dep", "medium", "dependency_effect", True, 10),
    RuleMeta("LD", "surge", "low", "load", True, 15),
    RuleMeta("L", "logs", "low", "evidence", False, 10),
    RuleMeta("Z", "instant", "low", "effect", True, 0),
]
FIRE = {"value": 90.0}


def t(seconds):
    return T0 + timedelta(seconds=seconds)


def driver(**kw):
    return Driver(METAS, **kw)


def fire(drv, n, rule="A", subject="orders", **kw):
    """n firing steps of one rule."""
    out = None
    for _ in range(n):
        out = drv.step(drv.obs(rule, subject, "firing", **{"value": 90.0, **kw}))
    return out


def ok(drv, n, rule="A", subject="orders"):
    out = None
    for _ in range(n):
        out = drv.step(drv.obs(rule, subject, "ok", value=10.0))
    return out


def no_data(drv, n, rule="A", subject="orders", source="prometheus"):
    out = None
    for _ in range(n):
        out = drv.step(drv.obs(rule, subject, "no_data", source=source))
    return out


class HoldTests(unittest.TestCase):
    def test_incident_opens_only_after_for_s(self):
        d = driver()
        fire(d, 2)                                   # t=0 and t=5
        self.assertIsNone(d.incident)
        res = fire(d, 1)                             # t=10: held for 10 s
        inc = d.incident
        self.assertIsNotNone(inc)
        self.assertEqual(inc.status, OPEN)
        self.assertEqual(inc.incident_id, "INC-0001")
        self.assertEqual(inc.opened_at, t(10))
        self.assertEqual(inc.symptoms[0].first_seen, t(0))      # the delay stays visible
        self.assertEqual(inc.symptoms[0].last_seen, t(10))
        self.assertEqual([e["event"] for e in res.events], ["incident_opened", "symptom_added"])
        self.assertEqual(res.changed, [inc])

    def test_an_ok_resets_the_hold(self):
        d = driver()
        fire(d, 2)
        ok(d, 1)                                     # t=10
        fire(d, 2)                                   # t=15, 20: not enough
        self.assertIsNone(d.incident)
        fire(d, 1)                                   # t=25: held since t=15
        self.assertEqual(d.incident.symptoms[0].first_seen, t(15))

    def test_no_data_resets_the_hold(self):
        d = driver()
        fire(d, 2)
        no_data(d, 1)
        fire(d, 2)
        self.assertIsNone(d.incident)
        fire(d, 1)
        self.assertEqual(d.incident.symptoms[0].first_seen, t(15))

    def test_a_missing_observation_resets_the_hold(self):
        d = driver()
        fire(d, 2)
        d.step()                                     # the rule yielded nothing in this cycle
        fire(d, 2)
        self.assertIsNone(d.incident)

    def test_hold_is_per_rule_and_subject(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("A", "orders", "firing", value=90.0), d.obs("A", "gateway", "ok", value=1.0))
        self.assertEqual([s.service for s in d.incident.symptoms], ["orders"])

    def test_for_s_zero_opens_immediately(self):
        d = driver()
        d.step(d.obs("Z", "gateway", "firing", value=1.0))
        self.assertIsNotNone(d.incident)

    def test_scheduler_jitter_does_not_cost_a_whole_interval(self):
        d = driver(interval=4.8)
        fire(d, 3)                                   # elapsed 9.6 s + 0.5 tolerance >= 10
        self.assertIsNotNone(d.incident)

    def test_the_tolerance_is_exactly_half_a_second(self):
        d = driver(interval=4.75)
        fire(d, 3)                                   # 9.5 s elapsed + 0.5 s tolerance = 10 s: held
        self.assertIsNotNone(d.incident)
        d = driver(interval=4.7)
        fire(d, 3)                                   # 9.4 s + 0.5 s < 10 s: not yet
        self.assertIsNone(d.incident)

    def test_unknown_rule_is_ignored_with_an_event(self):
        d = driver()
        res = d.step(d.obs("NOPE", "x", "firing", value=1.0))
        self.assertIsNone(d.incident)
        self.assertEqual(res.events, [{"event": "unknown_rule_ignored", "rule_id": "NOPE"}])


class AttachTests(unittest.TestCase):
    def test_later_symptoms_attach_to_the_open_incident(self):
        d = driver()
        fire(d, 3)
        for _ in range(3):
            d.step(d.obs("A", "orders", "firing", value=90.0), d.obs("B", "gateway", "firing", value=6.0))
        inc = d.incident
        self.assertEqual(inc.incident_id, "INC-0001")
        self.assertEqual({s.rule_id for s in inc.symptoms}, {"A", "B"})
        self.assertEqual(len(inc.symptoms), 2)

    def test_evidence_alone_never_opens_an_incident(self):
        d = driver()
        for _ in range(30):
            res = d.step(d.obs("L", "orders", "firing", value=5.0, source="loki"))
            self.assertEqual(res.changed, [])
        self.assertIsNone(d.incident)

    def test_evidence_does_not_reopen_a_resolved_incident(self):
        d = driver()
        fire(d, 3)
        ok(d, 13)
        self.assertEqual(d.incident.status, RESOLVED)
        for _ in range(8):
            d.step(d.obs("A", "orders", "ok", value=1.0), d.obs("L", "orders", "firing", value=5.0, source="loki"))
        self.assertEqual(d.incident.status, RESOLVED)

    def test_evidence_attaches_to_an_open_incident(self):
        d = driver()
        fire(d, 3)
        for _ in range(3):
            d.step(d.obs("A", "orders", "firing", value=90.0), d.obs("L", "orders", "firing", value=3.0, source="loki"))
        self.assertEqual({s.rule_id for s in d.incident.symptoms}, {"A", "L"})

    def test_waiting_evidence_attaches_when_an_incident_opens_and_keeps_its_first_seen(self):
        d = driver()
        # L fires from t=0, A from t=5: L qualifies at t=10, A at t=15 and opens the incident
        d.step(d.obs("L", "orders", "firing", value=3.0, source="loki"))
        for _ in range(5):
            d.step(d.obs("L", "orders", "firing", value=3.0, source="loki"), d.obs("A", "orders", "firing", value=90.0))
        inc = d.incident
        self.assertEqual(inc.opened_at, t(15))
        first = {s.rule_id: s.first_seen for s in inc.symptoms}
        self.assertEqual(first, {"L": t(0), "A": t(5)})

    def test_waiting_evidence_that_stopped_firing_is_not_attached(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("L", "orders", "firing", value=3.0, source="loki"))      # qualified at t=10
        d.step(d.obs("L", "orders", "ok", value=0.0, source="loki"))              # t=15
        fire(d, 3)
        self.assertEqual({s.rule_id for s in d.incident.symptoms}, {"A"})

    def test_evidence_that_never_met_its_hold_is_not_attached(self):
        d = driver()
        d.step(d.obs("L", "orders", "firing", value=3.0, source="loki"))
        d.step(d.obs("L", "orders", "firing", value=3.0, source="loki"), d.obs("A", "orders", "firing", value=90.0))
        for _ in range(2):
            d.step(d.obs("L", "orders", "ok", value=0.0, source="loki"), d.obs("A", "orders", "firing", value=90.0))
        self.assertEqual({s.rule_id for s in d.incident.symptoms}, {"A"})


class SymptomTests(unittest.TestCase):
    def test_peak_and_last_value(self):
        d = driver()
        fire(d, 3, value=90.0)
        for v in (95.0, 91.0):
            fire(d, 1, value=v)
        s = d.incident.symptoms[0]
        self.assertEqual((s.peak_value, s.last_value), (95.0, 91.0))

    def test_state_rule_keeps_the_latest_value(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("A", "redis", "firing", value=0.0, comparator="==", threshold=0.0))
        s = d.incident.symptoms[0]
        self.assertEqual((s.peak_value, s.last_value, s.comparator, s.threshold), (0.0, 0.0, "==", 0.0))

    def test_a_symptom_that_stops_becomes_inactive_and_keeps_its_last_seen(self):
        d = driver()
        fire(d, 4)                                   # last firing at t=15
        ok(d, 1)
        s = d.incident.symptoms[0]
        self.assertFalse(s.active)
        self.assertEqual(s.last_seen, t(15))

    def test_a_symptom_that_fires_again_is_reactivated_not_duplicated(self):
        d = driver()
        fire(d, 3)
        ok(d, 2)
        fire(d, 2)                                   # the hold must be met again
        self.assertFalse(d.incident.symptoms[0].active)
        res = fire(d, 1)
        inc = d.incident
        self.assertEqual(len(inc.symptoms), 1)
        self.assertTrue(inc.symptoms[0].active)
        self.assertEqual(inc.symptoms[0].flaps, 1)
        self.assertEqual(inc.symptoms[0].first_seen, t(0))
        self.assertIn("symptom_reactivated", [e["event"] for e in res.events])

    def test_dependency_name_is_taken_from_the_detail(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("D", "orders", "firing", value=0.4, detail="dependency=postgres (0.4)"))
        s = d.incident.symptoms[0]
        self.assertEqual(s.dependencies, ["postgres"])

    def test_a_changing_dependency_is_remembered(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("D", "orders", "firing", value=0.4, detail="dependency=postgres (0.4)"))
        d.step(d.obs("D", "orders", "firing", value=0.6, detail="dependency=redis (0.6)"))
        self.assertEqual(d.incident.symptoms[0].dependencies, ["postgres", "redis"])
        self.assertIn("redis", d.incident.candidate_services)

    def test_dependency_detail_is_only_read_for_the_dependency_role(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("B", "orders", "firing", value=6.0, detail="dependency=postgres"))
        self.assertEqual(d.incident.candidate_services, ["orders"])


class ResolveTests(unittest.TestCase):
    def test_resolves_after_resolve_after_s_without_an_active_symptom(self):
        d = driver()
        fire(d, 4)                                   # opened t=10, last firing t=15
        ok(d, 12)                                    # first ok at t=20 ... t=75
        self.assertEqual(d.incident.status, OPEN)
        res = ok(d, 1)                               # t=80: 60 s after the first quiet cycle
        inc = d.incident
        self.assertEqual(inc.status, RESOLVED)
        self.assertEqual(inc.resolved_at, t(80))
        self.assertIn("incident_resolved", [e["event"] for e in res.events])
        self.assertEqual(res.changed, [inc])

    def test_an_active_symptom_keeps_the_incident_open(self):
        d = driver()
        fire(d, 40)
        self.assertEqual(d.incident.status, OPEN)

    def test_a_returning_symptom_restarts_the_resolve_timer(self):
        d = driver()
        fire(d, 4)
        ok(d, 6)                                     # quiet for 25 s
        fire(d, 3)                                   # reactivated after its hold
        self.assertTrue(d.incident.symptoms[0].active)
        ok(d, 12)
        self.assertEqual(d.incident.status, OPEN)
        ok(d, 1)
        self.assertEqual(d.incident.status, RESOLVED)

    def test_a_data_gap_pauses_the_resolve_timer(self):
        d = driver()
        fire(d, 4)
        ok(d, 7)                                     # first ok t=20, quiet 30 at t=50
        no_data(d, 12)                               # t=55 ... t=110: paused
        self.assertEqual(d.incident.status, OPEN)
        for _ in range(7):                           # t=115: quiet 30 (the gap interval is not credited), t=145: 60
            self.assertEqual(d.incident.status, OPEN)
            ok(d, 1)
        self.assertEqual(d.incident.status, RESOLVED)
        self.assertEqual(d.incident.resolved_at, t(145))

    def test_no_data_never_resolves_an_incident(self):
        d = driver()
        fire(d, 4)
        no_data(d, 60)
        self.assertEqual(d.incident.status, OPEN)
        self.assertTrue(d.incident.symptoms[0].active)

    def test_a_gap_in_the_other_source_also_pauses(self):
        d = driver()
        fire(d, 4)
        for _ in range(30):
            d.step(d.obs("A", "orders", "ok", value=1.0), d.obs("L", "orders", "no_data", source="loki"))
        self.assertEqual(d.incident.status, OPEN)


class ReopenTests(unittest.TestCase):
    def resolved(self):
        d = driver()
        fire(d, 4)
        ok(d, 13)
        self.assertEqual(d.incident.status, RESOLVED)
        return d

    def test_a_returning_opener_within_the_window_reopens_the_same_incident(self):
        d = self.resolved()
        ok(d, 6)                                     # 30 s after the resolve
        res = fire(d, 3)
        inc = d.incident
        self.assertEqual(inc.incident_id, "INC-0001")
        self.assertEqual((inc.status, inc.reopen_count, inc.resolved_at), (OPEN, 1, None))
        self.assertEqual(len(inc.symptoms), 1)
        self.assertEqual(inc.symptoms[0].flaps, 1)
        self.assertIn("incident_reopened", [e["event"] for e in res.events])

    def test_a_symptom_after_the_window_is_a_new_incident(self):
        d = self.resolved()
        first = d.incident
        ok(d, 25)                                    # 125 s after the resolve
        fire(d, 3)
        self.assertEqual(d.incident.incident_id, "INC-0002")
        self.assertEqual(d.incident.reopen_count, 0)
        self.assertIsNot(d.incident, first)
        self.assertEqual(first.status, RESOLVED)
        self.assertEqual(len(first.symptoms), 1)

    def test_a_different_rule_within_the_window_also_reopens(self):
        d = self.resolved()
        for _ in range(3):
            d.step(d.obs("B", "gateway", "firing", value=6.0))
        self.assertEqual((d.incident.incident_id, d.incident.reopen_count), ("INC-0001", 1))
        self.assertEqual({s.rule_id for s in d.incident.symptoms}, {"A", "B"})

    def test_reopen_window_uses_the_stored_resolved_time(self):
        d = driver(reopen_within_s=10)
        fire(d, 4)
        ok(d, 13)
        ok(d, 4)
        fire(d, 3)
        self.assertEqual(d.incident.incident_id, "INC-0002")


class NoDataTests(unittest.TestCase):
    def test_no_data_freezes_the_symptom_and_records_a_gap(self):
        d = driver()
        fire(d, 4, value=91.5)                       # last firing at t=15
        res = no_data(d, 1)                          # t=20
        inc = d.incident
        s = inc.symptoms[0]
        self.assertTrue(s.active)
        self.assertEqual((s.last_seen, s.last_value, s.peak_value), (t(15), 91.5, 91.5))
        self.assertEqual(len(inc.data_gaps), 1)
        self.assertEqual((inc.data_gaps[0].source, inc.data_gaps[0].start, inc.data_gaps[0].end),
                         ("prometheus", t(20), None))
        self.assertIn("data_gap_opened", [e["event"] for e in res.events])
        self.assertEqual(res.changed, [inc])

    def test_the_gap_closes_when_data_returns(self):
        d = driver()
        fire(d, 4)
        no_data(d, 3)                                # t=20, 25, 30
        res = fire(d, 1)                             # t=35
        gap = d.incident.data_gaps[0]
        self.assertEqual((gap.start, gap.end), (t(20), t(35)))
        self.assertIn("data_gap_closed", [e["event"] for e in res.events])
        self.assertEqual(d.incident.symptoms[0].last_seen, t(35))

    def test_no_data_alone_creates_and_resolves_nothing(self):
        d = driver()
        for _ in range(30):
            res = no_data(d, 1)
            self.assertEqual(res.changed, [])
        self.assertIsNone(d.incident)

    def test_a_gap_that_is_open_when_the_incident_opens_is_part_of_it(self):
        d = driver()
        no_data(d, 2, rule="L", source="loki")       # loki gap open since t=0
        for _ in range(3):                           # the incident opens at t=20 while the gap is still open
            d.step(d.obs("A", "orders", "firing", value=90.0), d.obs("L", "orders", "no_data", source="loki"))
        gaps = d.incident.data_gaps
        self.assertEqual([(g.source, g.start, g.end) for g in gaps], [("loki", t(0), None)])

    def test_a_closed_gap_before_the_incident_is_not_attached(self):
        d = driver()
        no_data(d, 2, rule="L", source="loki")
        d.step(d.obs("L", "orders", "ok", value=0.0, source="loki"))
        fire(d, 3, )
        self.assertEqual(d.incident.data_gaps, [])

    def test_two_sources_have_separate_gaps(self):
        d = driver()
        fire(d, 4)
        d.step(d.obs("A", "orders", "no_data", source="prometheus"), d.obs("L", "orders", "no_data", source="loki"))
        d.step(d.obs("A", "orders", "firing", value=90.0), d.obs("L", "orders", "no_data", source="loki"))
        gaps = {g.source: g.end for g in d.incident.data_gaps}
        self.assertIsNotNone(gaps["prometheus"])
        self.assertIsNone(gaps["loki"])


class DerivedFieldTests(unittest.TestCase):
    def two_step(self, d, specs, n=3):
        for _ in range(n):
            d.step(*[d.obs(*s[:3], **s[3]) for s in specs])

    def test_one_root_signal_service_is_primary(self):
        d = driver()
        self.two_step(d, [("A", "postgres", "firing", {"value": 0.0}), ("B", "gateway", "firing", {"value": 9.0})])
        self.assertEqual(d.incident.primary_service, "postgres")

    def test_two_root_signal_services_give_no_primary(self):
        d = driver()
        self.two_step(d, [("A", "orders", "firing", {"value": 90.0}), ("A2", "payments", "firing", {"value": 90.0})])
        self.assertIsNone(d.incident.primary_service)

    def test_the_same_service_in_two_root_rules_is_still_primary(self):
        d = driver()
        self.two_step(d, [("A", "orders", "firing", {"value": 90.0}), ("A2", "orders", "firing", {"value": 90.0})])
        self.assertEqual(d.incident.primary_service, "orders")

    def test_a_load_symptom_means_no_primary(self):
        d = driver()
        self.two_step(d, [("A", "gateway", "firing", {"value": 95.0}), ("LD", "gateway", "firing", {"value": 31.0})], n=4)
        self.assertEqual({s.role for s in d.incident.symptoms}, {"root_signal", "load"})
        self.assertIsNone(d.incident.primary_service)

    def test_effects_only_mean_no_primary(self):
        d = driver()
        self.two_step(d, [("B", "gateway", "firing", {"value": 9.0}), ("D", "orders", "firing", {"value": 0.4, "detail": "dependency=postgres (0.4)"})])
        self.assertIsNone(d.incident.primary_service)

    def test_a_root_signal_without_a_service_is_not_a_primary(self):
        d = driver()
        self.two_step(d, [("A", None, "firing", {"value": 90.0})])
        self.assertIsNone(d.incident.primary_service)

    def test_candidate_and_affected_services_ordered_by_first_seen_then_name(self):
        d = driver()
        d.step(d.obs("A", "orders", "firing", value=90.0))
        d.step(d.obs("A", "orders", "firing", value=90.0),
               d.obs("D", "orders", "firing", value=0.4, detail="dependency=postgres (0.4)"))
        for _ in range(8):
            d.step(d.obs("A", "orders", "firing", value=90.0),
                   d.obs("D", "orders", "firing", value=0.4, detail="dependency=postgres (0.4)"),
                   d.obs("B", "gateway", "firing", value=6.0))
        inc = d.incident
        # first_seen: A orders t=0, D (names postgres) t=5, B gateway t=10
        self.assertEqual(inc.candidate_services, ["orders", "postgres", "gateway"])
        self.assertEqual(inc.affected_services, ["orders", "gateway"])

    def test_equal_first_seen_is_ordered_by_name(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("A", "redis", "firing", value=0.0), d.obs("A2", "inventory", "firing", value=90.0),
                   d.obs("B", "gateway", "firing", value=6.0))
        self.assertEqual(d.incident.candidate_services, ["gateway", "inventory", "redis"])

    def test_severity_is_the_highest_of_the_symptoms(self):
        d = driver()
        self.two_step(d, [("B", "gateway", "firing", {"value": 6.0})])
        self.assertEqual(d.incident.severity, "medium")
        self.two_step(d, [("B", "gateway", "firing", {"value": 6.0}), ("A", "orders", "firing", {"value": 90.0})])
        self.assertEqual(d.incident.severity, "high")

    def test_title_is_deterministic_and_short(self):
        d = driver()
        self.two_step(d, [("A", "orders", "firing", {"value": 90.0}), ("B", "gateway", "firing", {"value": 6.0})])
        self.assertEqual(d.incident.title, "alpha orders, bravo gateway")
        d2 = driver()
        self.two_step(d2, [("A", "a", "firing", {"value": 1.0}), ("A2", "b", "firing", {"value": 1.0}),
                           ("B", "c", "firing", {"value": 1.0}), ("D", "d", "firing", {"value": 1.0}),
                           ("LD", "e", "firing", {"value": 1.0})], n=4)
        self.assertTrue(d2.incident.title.endswith(" and 2 more"), d2.incident.title)

    def test_the_title_names_the_symptoms_that_open_incidents_not_the_evidence(self):
        d = driver()
        # log evidence L is the first one to fire (t=0), the opening rules A and B follow (t=5 and t=10)
        d.step(d.obs("L", "gateway", "firing", value=3.0, source="loki"))
        d.step(d.obs("L", "gateway", "firing", value=3.0, source="loki"), d.obs("A", "postgres", "firing", value=0.0))
        for _ in range(6):
            d.step(d.obs("L", "gateway", "firing", value=3.0, source="loki"), d.obs("L", "inventory", "firing", value=3.0, source="loki"),
                   d.obs("A", "postgres", "firing", value=0.0), d.obs("B", "gateway", "firing", value=9.0))
        inc = d.incident
        self.assertEqual({s.rule_id for s in inc.symptoms}, {"A", "B", "L"})
        self.assertEqual(inc.title, "alpha postgres, bravo gateway")
        self.assertNotIn("logs", inc.title)

    def test_the_title_counts_only_the_opening_symptoms_in_and_N_more(self):
        d = driver()
        for _ in range(4):
            d.step(d.obs("A", "a", "firing", value=1.0), d.obs("A2", "b", "firing", value=1.0), d.obs("B", "c", "firing", value=1.0),
                   d.obs("D", "d", "firing", value=1.0), d.obs("L", "a", "firing", value=1.0, source="loki"),
                   d.obs("L", "b", "firing", value=1.0, source="loki"), d.obs("L", "c", "firing", value=1.0, source="loki"))
        self.assertEqual(d.incident.title, "alpha a, alpha_two b, bravo c and 1 more")

    def test_a_title_without_any_opening_symptom_falls_back_to_all_symptoms(self):
        d = driver()
        for _ in range(3):
            d.step(d.obs("A", "x", "firing", value=1.0))
        evidence_only = [s for s in d.incident.symptoms]
        for s in evidence_only:
            s.opens_incident = False
        self.assertEqual(d.correlator._title(evidence_only), "alpha x")

    def test_updated_at_follows_the_last_change(self):
        d = driver()
        fire(d, 3)
        self.assertEqual(d.incident.updated_at, t(10))
        fire(d, 2)
        self.assertEqual(d.incident.updated_at, t(20))

    def test_to_api_has_the_fixed_fields(self):
        d = driver()
        fire(d, 3)
        api = d.incident.to_api()
        self.assertEqual(list(api), ["incident_id", "schema_version", "status", "opened_at", "updated_at",
                                     "resolved_at", "reopen_count", "severity", "title", "primary_service",
                                     "candidate_services", "affected_services", "symptoms", "data_gaps"])
        self.assertEqual(api["schema_version"], 1)
        self.assertEqual(api["opened_at"], "2026-10-18T12:00:10.000Z")
        self.assertEqual(list(api["symptoms"][0]), ["symptom_id", "rule_id", "detector", "source", "service", "role",
                                                     "severity", "opens_incident", "first_seen", "last_seen",
                                                     "active", "flaps", "comparator", "threshold", "peak_value",
                                                     "last_value", "detail"])
        self.assertEqual(api["symptoms"][0]["symptom_id"], "SYM-00001")


if __name__ == "__main__":
    unittest.main()
