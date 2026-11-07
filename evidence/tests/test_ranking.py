import copy
import io
import unittest
from contextlib import redirect_stdout

import _bootstrap  # noqa: F401
import fakes
from _bootstrap import RANKING_PATH
from fakes import DEPS, RANKING, box, change, down, hint, logs, rank_scenario, sig, stack, svc

from app import ranking as rk
from app.model import load_json_file


def names(result):
    return [s.service for s in result.suspects]


def comp(result, service, key):
    return next(s for s in result.suspects if s.service == service).components[key]


class ConfigTests(unittest.TestCase):
    def test_real_config(self):
        self.assertEqual(list(RANKING.components), list(rk.COMPONENT_ORDER))
        self.assertEqual(RANKING.margin, 10)
        for c in RANKING.components.values():
            self.assertTrue(c.reason)
            self.assertGreater(c.cap, 0)

    def test_every_weight_has_a_reason(self):
        raw = load_json_file(RANKING_PATH)
        for name, item in raw["components"].items():
            self.assertTrue(item["provenance"]["reason"].strip(), name)

    def mutate(self, fn):
        raw = copy.deepcopy(load_json_file(RANKING_PATH))
        fn(raw)
        return raw

    def test_rejections(self):
        cases = {
            "missing component": lambda r: r["components"].pop("precedence"),
            "extra component": lambda r: r["components"].update({"magic": r["components"]["precedence"]}),
            "empty reason": lambda r: r["components"]["precedence"]["provenance"].update({"reason": " "}),
            "measured without evidence": lambda r: r["components"]["precedence"]["provenance"].update({"kind": "measured"}),
            "bad kind": lambda r: r["components"]["precedence"]["provenance"].update({"kind": "guess"}),
            "cap zero": lambda r: r["components"]["precedence"].update({"cap": 0}),
            "cap above 100": lambda r: r["components"]["precedence"].update({"cap": 101}),
            "unknown param": lambda r: r["components"]["precedence"]["params"].update({"x": 1}),
            "missing param": lambda r: r["components"]["precedence"]["params"].pop("tie_s"),
            "fraction above 1": lambda r: r["components"]["anomaly_strength"]["params"].update({"floor": 1.5}),
            "full_ratio at 1": lambda r: r["components"]["anomaly_strength"]["params"].update({"full_ratio": 1.0}),
            "bad change kind": lambda r: r["components"]["recent_change"]["params"].update({"kinds": ["exploded"]}),
            "candidate above cap": lambda r: r["components"]["detector_hint"]["params"].update({"candidate_points": 99}),
            "negative margin": lambda r: r.update({"no_single_root_margin": -1}),
            "extra top key": lambda r: r.update({"x": 1}),
            "bool as number": lambda r: r["components"]["precedence"].update({"cap": True}),
        }
        for label, fn in cases.items():
            with self.subTest(label), self.assertRaises(rk.RankingConfigError):
                rk.parse_ranking(self.mutate(fn))

    def test_measured_with_evidence_is_accepted(self):
        raw = self.mutate(lambda r: r["components"]["precedence"]["provenance"].update({"kind": "measured", "evidence": "run 2026-11-10"}))
        self.assertEqual(rk.parse_ranking(raw).components["precedence"].kind, "measured")

    def test_table_and_cli(self):
        self.assertEqual(len(rk.format_table(RANKING)), 2 + 9)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(rk.main(["x", RANKING_PATH]), 0)
        self.assertIn("9 components valid", buf.getvalue())
        self.assertIn("design weights: 9 of 9", buf.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rk.main(["x", "/no/such.json"]), 1)


class ComponentTests(unittest.TestCase):
    H = hint()

    def test_no_evidence_no_suspects(self):
        r = rank_scenario(stack(), self.H)
        self.assertEqual(r.suspects, ())
        self.assertTrue(r.no_single_root)
        self.assertEqual(r.confidence, 0.0)

    def test_strength_scales_between_floor_and_cap(self):
        lo = rank_scenario(stack(orders=[sig("cpu_pct_of_limit", ratio=1.0)]), self.H)
        mid = rank_scenario(stack(orders=[sig("cpu_pct_of_limit", ratio=1.5)]), self.H)
        hi = rank_scenario(stack(orders=[sig("cpu_pct_of_limit", ratio=9.0)]), self.H)
        self.assertEqual(comp(lo, "orders", "anomaly_strength"), 3.8)  # 15 x 0.25
        self.assertEqual(comp(mid, "orders", "anomaly_strength"), 9.4)  # 15 x (0.25 + 0.75 x 0.5)
        self.assertEqual(comp(hi, "orders", "anomaly_strength"), 15.0)

    def test_down_signal_earns_the_full_strength(self):
        r = rank_scenario(stack(postgres=[down("container_present")]), self.H)
        self.assertEqual(comp(r, "postgres", "anomaly_strength"), 15.0)

    def test_strength_uses_the_worst_signal(self):
        r = rank_scenario(stack(orders=[sig("cpu_pct_of_limit", ratio=1.0), sig("latency_p95", ratio=3.0)]), self.H)
        self.assertEqual(comp(r, "orders", "anomaly_strength"), 15.0)

    def test_precedence_tie_and_decay(self):
        s = stack(orders=[sig("latency_p95", first=0)], inventory=[sig("latency_p95", first=5)], payments=[sig("latency_p95", first=20)],
                  gateway=[sig("latency_p95", first=100)])
        r = rank_scenario(s, self.H)
        self.assertEqual(comp(r, "orders", "precedence"), 10.0)
        self.assertEqual(comp(r, "inventory", "precedence"), 10.0)  # within tie_s of the earliest
        self.assertEqual(comp(r, "payments", "precedence"), 5.0)  # lag 20: 1 - (20 - 5) / 30
        self.assertEqual(comp(r, "gateway", "precedence"), 0.0)  # far later

    def test_precedence_single_service(self):
        r = rank_scenario(stack(orders=[sig("latency_p95", first=7)]), self.H)
        self.assertEqual(comp(r, "orders", "precedence"), 10.0)

    def test_hard_evidence_variants(self):
        cases = {
            "missing": (stack(redis=[down("container_present")]), "redis", 30.0),
            "target down": (stack(inventory=[down("up")]), "inventory", 30.0),
            "exited": (stack(orders=svc("orders", [sig("latency_p95")], container=box("orders", "exited"))), "orders", 30.0),
            "restarting": (stack(orders=svc("orders", [sig("latency_p95")], container=box("orders", "restarting"))), "orders", 30.0),
            "oom": (stack(orders=svc("orders", [sig("latency_p95")], container=box("orders", oom_killed=True))), "orders", 30.0),
            "crash loop": (stack(orders=[sig("restarts", window_max=3.0)]), "orders", 30.0),
            "one restart": (stack(orders=[sig("restarts", window_max=1.0)]), "orders", 15.0),
            "running and healthy": (stack(orders=svc("orders", [sig("latency_p95")], container=box("orders", "running", oom_killed=False))), "orders", 0.0),
        }
        for label, (s, name, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(comp(rank_scenario(s, self.H), name, "hard_container_evidence"), expected)

    def test_recent_change_window_and_kinds(self):
        def pts_for(kind, at_s, first=100):
            return rank_scenario(stack(orders=svc("orders", [sig("latency_p95", first=first)], changes=[change("orders", kind, at_s)])), self.H)

        self.assertEqual(comp(pts_for("recreated", 90), "orders", "recent_change"), 15.0)
        self.assertEqual(comp(pts_for("config_changed", 20), "orders", "recent_change"), 15.0)  # 80 s before
        self.assertEqual(comp(pts_for("recreated", 100 - 120), "orders", "recent_change"), 15.0)  # exactly 120 s
        self.assertEqual(comp(pts_for("recreated", 100 - 121), "orders", "recent_change"), 0.0)  # too old
        self.assertEqual(comp(pts_for("recreated", 110), "orders", "recent_change"), 15.0)  # 10 s after: poller delay
        self.assertEqual(comp(pts_for("recreated", 116), "orders", "recent_change"), 0.0)  # beyond the poller slack
        self.assertEqual(comp(pts_for("stopped", 90), "orders", "recent_change"), 0.0)  # wrong kind
        self.assertEqual(comp(pts_for("restarted", 90), "orders", "recent_change"), 0.0)

    def test_recent_change_needs_an_anomaly_of_the_same_service(self):
        s = stack(orders=svc("orders", [sig("latency_p95", first=100)]),
                  payments=svc("payments", [], changes=[change("payments", "recreated", 90)]))
        r = rank_scenario(s, self.H)
        self.assertNotIn("payments", names(r))

    def test_recent_change_reason_text(self):
        r = rank_scenario(stack(orders=svc("orders", [sig("latency_p95", first=100)], changes=[change("orders", "recreated", 90)])), self.H)
        reasons = next(s for s in r.suspects if s.service == "orders").reasons
        self.assertIn("change record 'recreated' 10 s before its first anomaly", reasons)

    def test_origin_position(self):
        r = rank_scenario(stack(orders=[sig("latency_p95")]), self.H)
        self.assertEqual(comp(r, "orders", "origin_position"), 10.0)
        r = rank_scenario(stack(orders=[sig("latency_p95")], inventory=[sig("latency_p95")]), self.H)
        self.assertEqual(comp(r, "orders", "origin_position"), 0.0)
        self.assertEqual(comp(r, "inventory", "origin_position"), 10.0)

    def test_effect_demotion(self):
        r = rank_scenario(stack(orders=[sig("latency_p95")], inventory=[sig("latency_p95")]), self.H)
        self.assertEqual(comp(r, "orders", "effect_demotion"), -20.0)
        self.assertEqual(comp(r, "inventory", "effect_demotion"), 0.0)
        self.assertLess(r.suspects[1].score, r.suspects[0].score)
        self.assertEqual(names(r), ["inventory", "orders"])

    def test_demotion_does_not_hide_an_anomalous_service(self):
        r = rank_scenario(stack(gateway=[sig("latency_p95", ratio=1.0)], orders=[sig("latency_p95")]), self.H)
        self.assertIn("gateway", names(r))
        self.assertEqual(next(s for s in r.suspects if s.service == "gateway").score, 0.0)

    def test_shared_dependency_needs_two_implicated_callers(self):
        both = stack(inventory=[sig("dependency_latency_p95", "postgres")], orders=[sig("dependency_error_rate", "postgres")])
        self.assertEqual(comp(rank_scenario(both, self.H), "postgres", "shared_dependency"), 10.0)
        one = stack(orders=[sig("dependency_error_rate", "postgres")])
        self.assertEqual(comp(rank_scenario(one, self.H), "postgres", "shared_dependency"), 0.0)

    def test_shared_dependency_ignores_callers_that_do_not_name_it(self):
        # inventory and payments are anomalous because of redis; postgres is healthy and not named
        s = stack(redis=[down("container_present")], inventory=[sig("dependency_error_rate", "redis")], payments=[sig("dependency_error_rate", "redis")])
        self.assertNotIn("postgres", names(rank_scenario(s, self.H)))

    def test_shared_dependency_when_the_dependency_is_anomalous_itself(self):
        s = stack(postgres=[sig("cpu_pct_of_limit")], inventory=[sig("latency_p95")], payments=[sig("latency_p95")])
        self.assertEqual(comp(rank_scenario(s, self.H), "postgres", "shared_dependency"), 10.0)

    def test_log_evidence_parts(self):
        own = rank_scenario(stack(orders=svc("orders", [sig("latency_p95")], logs=logs(10, 0))), self.H)
        self.assertEqual(comp(own, "orders", "log_evidence"), 3.0)
        same = rank_scenario(stack(orders=svc("orders", [sig("latency_p95")], logs=logs(5, 5))), self.H)
        self.assertEqual(comp(same, "orders", "log_evidence"), 0.0)
        dep_err = rank_scenario(stack(orders=[sig("dependency_error_rate", "postgres")]), self.H)
        self.assertEqual(comp(dep_err, "postgres", "log_evidence"), 7.0)
        dep_lat = rank_scenario(stack(inventory=[sig("dependency_latency_p95", "postgres")]), self.H)
        self.assertEqual(comp(dep_lat, "postgres", "log_evidence"), 4.0)

    def test_log_evidence_is_capped(self):
        s = stack(orders=[sig("dependency_error_rate", "postgres"), sig("dependency_latency_p95", "postgres")],
                  postgres=svc("postgres", [down("container_present")], logs=logs(10, 0)))
        self.assertEqual(comp(rank_scenario(s, self.H), "postgres", "log_evidence"), 10.0)

    def test_log_evidence_ignores_no_data_logs(self):
        from app.model import LogEvidence
        s = stack(orders=svc("orders", [sig("latency_p95")], logs=LogEvidence("no_data")))
        self.assertEqual(comp(rank_scenario(s, self.H), "orders", "log_evidence"), 0.0)

    def test_detector_hint(self):
        s = stack(orders=[sig("latency_p95")], payments=[sig("latency_p95")], gateway=[sig("latency_p95")])
        r = rank_scenario(s, hint("orders", ["payments"]))
        self.assertEqual(comp(r, "orders", "detector_hint"), 8.0)
        self.assertEqual(comp(r, "payments", "detector_hint"), 4.0)
        self.assertEqual(comp(r, "gateway", "detector_hint"), 0.0)

    def test_score_is_clipped_to_0_100(self):
        s = scenario_max()
        r = rank_scenario(s, hint("postgres", ["postgres"]))
        for sus in r.suspects:
            self.assertGreaterEqual(sus.score, 0)
            self.assertLessEqual(sus.score, 100)


def scenario_max():
    return stack(
        postgres=svc("postgres", [down("container_present"), sig("restarts", window_max=9.0), sig("cpu_pct_of_limit", ratio=9)],
                     container=box("postgres", "exited", oom_killed=True), logs=logs(99, 0),
                     changes=[change("postgres", "recreated", -1)]),
        orders=[sig("dependency_error_rate", "postgres"), sig("dependency_latency_p95", "postgres")],
        inventory=[sig("dependency_error_rate", "postgres")],
        payments=[sig("dependency_error_rate", "postgres")],
    )


class RankingBehaviourTests(unittest.TestCase):
    def test_ties_are_broken_by_name(self):
        s = stack(payments=[sig("latency_p95", ratio=2.0)], inventory=[sig("latency_p95", ratio=2.0)])
        r = rank_scenario(s, hint())
        self.assertEqual(r.suspects[0].score, r.suspects[1].score)
        self.assertEqual(names(r), ["inventory", "payments"])

    def test_ranks_are_consecutive_from_one(self):
        s, h = fakes.scenario_f10()
        r = rank_scenario(s, h)
        self.assertEqual([x.rank for x in r.suspects], list(range(1, len(r.suspects) + 1)))

    def test_deterministic(self):
        s, h = fakes.scenario_f10()
        a, b = rank_scenario(s, h), rank_scenario(s, h)
        self.assertEqual([x.to_dict() for x in a.suspects], [x.to_dict() for x in b.suspects])

    def test_components_are_all_present_with_their_names(self):
        s, h = fakes.scenario_f10()
        for sus in rank_scenario(s, h).suspects:
            self.assertEqual(list(sus.components), list(rk.COMPONENT_ORDER))

    def test_confidence_is_rank1_minus_rank2(self):
        s, h = fakes.scenario_f10()
        r = rank_scenario(s, h)
        self.assertAlmostEqual(r.confidence, r.suspects[0].score - r.suspects[1].score, places=1)

    def test_single_suspect_confidence(self):
        r = rank_scenario(stack(orders=[sig("latency_p95", ratio=3.0)]), hint())
        self.assertEqual(len(r.suspects), 1)
        self.assertEqual(r.confidence, r.suspects[0].score)
        self.assertFalse(r.no_single_root)

    def test_no_single_root_margin_boundary(self):
        # two origins with a score gap of exactly the margin (10): within the margin -> true
        s = stack(payments=[sig("latency_p95", ratio=3.0)], inventory=[sig("latency_p95", ratio=3.0)])
        r = rank_scenario(s, hint("inventory"))  # inventory +8
        self.assertEqual(round(r.suspects[0].score - r.suspects[1].score, 1), 8.0)
        self.assertTrue(r.no_single_root)
        r2 = rank_scenario(s, hint("inventory", ["payments"], []))  # +8 and +4 -> gap 4
        self.assertTrue(r2.no_single_root)
        s3 = stack(payments=[sig("latency_p95", ratio=3.0)], inventory=[sig("latency_p95", ratio=3.0), down("up")])
        r3 = rank_scenario(s3, hint())
        self.assertFalse(r3.no_single_root)

    def two_origins(self):
        return stack(payments=[sig("latency_p95", ratio=3.0)], inventory=[sig("latency_p95", ratio=3.0)])

    def gap_of(self, services, h):
        from dataclasses import replace
        base = rank_scenario(services, h)  # the gap does not depend on the margin
        return base.suspects[0].score - base.suspects[1].score, replace

    def test_no_single_root_gap_exactly_equal_to_the_margin_is_true(self):
        s, h = self.two_origins(), hint("inventory")
        gap, replace = self.gap_of(s, h)
        cfg = replace(RANKING, margin=gap)
        from app.ranking import rank
        anomalous = [n for n, e in s.items() if e.is_anomalous()]
        shared = fakes.GRAPH.shared_dependencies(anomalous, 2)
        self.assertGreater(gap, 0)
        self.assertTrue(rank(s, DEPS, shared, h, cfg).no_single_root)

    def test_no_single_root_gap_just_above_the_margin_is_false(self):
        s, h = self.two_origins(), hint("inventory")
        gap, replace = self.gap_of(s, h)
        cfg = replace(RANKING, margin=gap - 0.05)
        from app.ranking import rank
        anomalous = [n for n, e in s.items() if e.is_anomalous()]
        shared = fakes.GRAPH.shared_dependencies(anomalous, 2)
        self.assertFalse(rank(s, DEPS, shared, h, cfg).no_single_root)

    def test_load_symptom_forces_no_single_root(self):
        s = stack(gateway=[sig("request_rate", ratio=3.0)] + [down("container_present")])
        r = rank_scenario(s, hint(None, [], [("R08", "gateway", "load", 0)]))
        self.assertGreater(r.confidence, 10)
        self.assertTrue(r.no_single_root)

    def test_reasons_are_deterministic_sentences(self):
        s, h = fakes.scenario_f10()
        top = rank_scenario(s, h).suspects[0]
        self.assertEqual(top.service, "postgres")
        self.assertIn("container missing (no memory series)", top.reasons)
        self.assertIn("earliest anomalous service", top.reasons)
        self.assertIn("named primary_service by the detector", top.reasons)
        for line in top.reasons:
            self.assertIsInstance(line, str)
            self.assertLess(len(line), 200)


class FaultShapeTests(unittest.TestCase):
    """The shapes of the M3 faults with made-up numbers: the design weights must put the root first."""

    def test_f10_postgres_down(self):
        r = rank_scenario(*fakes.scenario_f10())
        self.assertEqual(r.suspects[0].service, "postgres")
        self.assertFalse(r.no_single_root)
        self.assertGreater(r.confidence, 50)

    def test_f13_redis_down(self):
        r = rank_scenario(*fakes.scenario_f13())
        self.assertEqual(r.suspects[0].service, "redis")
        self.assertNotIn("postgres", names(r))

    def test_f06_missing_env_variable(self):
        r = rank_scenario(*fakes.scenario_f06())
        self.assertEqual(r.suspects[0].service, "inventory")
        self.assertEqual(comp(r, "inventory", "recent_change"), 15.0)

    def test_f08_wrong_credential(self):
        r = rank_scenario(*fakes.scenario_f08())
        self.assertEqual(r.suspects[0].service, "orders")
        self.assertEqual(comp(r, "orders", "recent_change"), 15.0)

    def test_f01_cpu(self):
        self.assertEqual(rank_scenario(*fakes.scenario_f01()).suspects[0].service, "orders")

    def test_f02_memory(self):
        self.assertEqual(rank_scenario(*fakes.scenario_f02()).suspects[0].service, "payments")

    def test_f11_lock_postgres_in_top_three(self):
        r = rank_scenario(*fakes.scenario_f11())
        self.assertIn("postgres", names(r)[:3])

    def test_f18_traffic_spike(self):
        r = rank_scenario(*fakes.scenario_f18())
        self.assertTrue(r.no_single_root)
        self.assertIn("gateway", names(r))


if __name__ == "__main__":
    unittest.main()
