import math
import unittest

import _bootstrap  # noqa: F401
from fakes import APP4, ALL6, T0, FakeSource, by, scalar
from app.model import FIRING, NO_DATA, OK
from app.rules import Rule, parse_rules
from app.sources import SOURCE_NO_DATA, SOURCE_WARMING_UP, QueryFailed, Sample
from app.thresholds import ThresholdDetector, compare


def make_rule(**changes):
    raw = {"id": "R90", "name": "test_rule", "detector": "threshold", "source": "prometheus", "query": "Q",
           "subject_label": "service", "comparator": ">", "threshold": 85, "for_s": 10, "severity": "medium",
           "role": "root_signal", "opens_incident": True, "on_missing": "ok", "expected_subjects": None,
           "guard": None, "min_traffic": None,
           "provenance": {"baseline_max": 0, "fault_value_min": 90, "margin_ratio": None, "derived_from": "t"},
           "description": "d"}
    raw.update(changes)
    return parse_rules({"schema_version": 1, "rules": [raw]})[0]


def run(rule, prom=None, loki=None):
    prom = prom or FakeSource("prometheus")
    loki = loki or FakeSource("loki")
    det = ThresholdDetector([rule], {"prometheus": prom, "loki": loki})
    return det, prom, loki


class CompareTests(unittest.TestCase):
    def test_greater_than_is_strict(self):
        self.assertTrue(compare(">", 85.0001, 85))
        self.assertFalse(compare(">", 85, 85))
        self.assertFalse(compare(">", 84.99, 85))

    def test_equal_zero(self):
        self.assertTrue(compare("==", 0.0, 0))
        self.assertFalse(compare("==", 1.0, 0))
        self.assertFalse(compare("==", 0.001, 0))

    def test_unknown_comparator(self):
        with self.assertRaises(ValueError):
            compare(">=", 1, 1)


class EvaluationTests(unittest.TestCase):
    def test_firing_ok_and_boundary_per_subject(self):
        det, prom, _ = run(make_rule())
        prom.data["Q"] = by("service", {"orders": 90.92, "gateway": 85, "payments": 20})
        obs = {o.subject: o for o in det.evaluate(T0)}
        self.assertEqual(obs["orders"].state, FIRING)
        self.assertEqual(obs["orders"].value, 90.92)
        self.assertEqual(obs["gateway"].state, OK)        # exactly the threshold does not fire
        self.assertEqual(obs["payments"].state, OK)
        for o in obs.values():
            self.assertEqual((o.rule_id, o.detector, o.source, o.threshold, o.comparator, o.ts),
                             ("R90", "threshold", "prometheus", 85.0, ">", T0))

    def test_one_observation_per_subject_sorted(self):
        det, prom, _ = run(make_rule())
        prom.data["Q"] = by("service", {"payments": 1, "gateway": 2, "orders": 3})
        self.assertEqual([o.subject for o in det.evaluate(T0)], ["gateway", "orders", "payments"])

    def test_values_above_100_are_not_clamped(self):
        det, prom, _ = run(make_rule())
        prom.data["Q"] = by("service", {"orders": 110.1})
        self.assertEqual(det.evaluate(T0)[0].value, 110.1)

    def test_series_without_the_subject_label_is_ignored(self):
        det, prom, _ = run(make_rule())
        prom.data["Q"] = [Sample({}, 99.0), Sample({"service": "orders"}, 1.0)]
        self.assertEqual([(o.subject, o.state) for o in det.evaluate(T0)], [("orders", OK)])

    def test_scalar_rule_has_subject_none(self):
        det, prom, _ = run(make_rule(subject_label=None))
        prom.data["Q"] = scalar(90)
        obs = det.evaluate(T0)
        self.assertEqual([(o.subject, o.state, o.value) for o in obs], [(None, FIRING, 90.0)])

    def test_empty_result_of_a_scalar_rule_is_ok_not_an_error(self):
        det, prom, _ = run(make_rule(subject_label=None))
        prom.data["Q"] = []
        o = det.evaluate(T0)[0]
        self.assertEqual((o.subject, o.state, o.value), (None, OK, None))

    def test_state_rule_equal_zero(self):
        rule = make_rule(comparator="==", threshold=0, provenance={"baseline_max": 1, "fault_value_min": 0,
                                                                  "margin_ratio": None, "derived_from": "t"})
        det, prom, _ = run(rule)
        prom.data["Q"] = by("service", {"gateway": 1, "orders": 0})
        obs = {o.subject: o.state for o in det.evaluate(T0)}
        self.assertEqual(obs, {"gateway": OK, "orders": FIRING})


class OnMissingTests(unittest.TestCase):
    ZERO = dict(comparator="==", threshold=0, on_missing="zero", expected_subjects=ALL6,
                provenance={"baseline_max": 1, "fault_value_min": 0, "margin_ratio": None, "derived_from": "t"})

    def test_zero_missing_subject_is_evaluated_as_zero(self):
        det, prom, _ = run(make_rule(**self.ZERO))
        prom.data["Q"] = by("service", {s: 1 for s in ALL6 if s != "postgres"})   # F10: the series is gone
        obs = {o.subject: o for o in det.evaluate(T0)}
        self.assertEqual(len(obs), 6)
        self.assertEqual((obs["postgres"].state, obs["postgres"].value), (FIRING, 0.0))
        self.assertIn("missing", obs["postgres"].detail)
        self.assertTrue(all(obs[s].state == OK for s in ALL6 if s != "postgres"))

    def test_zero_with_a_completely_empty_result(self):
        det, prom, _ = run(make_rule(**self.ZERO))
        prom.data["Q"] = []
        self.assertTrue(all(o.state == FIRING for o in det.evaluate(T0)))

    def test_ok_missing_subject_is_ok(self):
        det, prom, _ = run(make_rule(expected_subjects=["orders", "gateway"]))
        prom.data["Q"] = by("service", {"gateway": 10})
        obs = {o.subject: o for o in det.evaluate(T0)}
        self.assertEqual((obs["orders"].state, obs["orders"].value), (OK, None))

    def test_a_known_subject_that_disappears_is_not_dropped(self):
        det, prom, _ = run(make_rule())          # no expected_subjects
        prom.data["Q"] = by("service", {"orders": 95})
        self.assertEqual(det.evaluate(T0)[0].state, FIRING)
        prom.data["Q"] = []                      # orders disappears from the result
        obs = det.evaluate(T0)
        self.assertEqual([(o.subject, o.state) for o in obs], [("orders", OK)])


class GuardTests(unittest.TestCase):
    def test_guard_false_gives_no_data_for_every_subject(self):
        rule = make_rule(guard="G", on_missing="zero", expected_subjects=["a", "b"], comparator="==", threshold=0,
                         provenance={"baseline_max": 1, "fault_value_min": 0, "margin_ratio": None, "derived_from": "t"})
        det, prom, _ = run(rule)
        prom.data["G"] = scalar(0)
        prom.data["Q"] = by("service", {"a": 0})
        obs = det.evaluate(T0)
        self.assertEqual({(o.subject, o.state) for o in obs}, {("a", NO_DATA), ("b", NO_DATA)})
        self.assertIn("guard", obs[0].detail)
        self.assertNotIn("Q", prom.queries)       # the main query is not even asked

    def test_guard_with_empty_result_is_false(self):
        det, prom, _ = run(make_rule(guard="G", expected_subjects=["a"]))
        prom.data["G"] = []
        self.assertEqual(det.evaluate(T0)[0].state, NO_DATA)

    def test_guard_true_lets_the_rule_run(self):
        det, prom, _ = run(make_rule(guard="G", expected_subjects=["a"]))
        prom.data["G"] = scalar(1)
        prom.data["Q"] = by("service", {"a": 99})
        self.assertEqual(det.evaluate(T0)[0].state, FIRING)

    def test_guard_query_failure_gives_no_data(self):
        det, prom, _ = run(make_rule(guard="G", expected_subjects=["a"]))
        prom.data["G"] = QueryFailed("timeout")
        o = det.evaluate(T0)[0]
        self.assertEqual(o.state, NO_DATA)
        self.assertIn("timeout", o.detail)


class RateGuardTests(unittest.TestCase):
    RULE = dict(subject_label="service", expected_subjects=["gateway"],
                min_traffic={"query": "T", "min": 0.5, "subject_label": "service"})

    def test_below_minimum_traffic_is_ok_even_when_the_value_is_high(self):
        det, prom, _ = run(make_rule(threshold=5, **self.RULE))
        prom.data["Q"] = by("service", {"gateway": 100.0})        # one failed request at a tiny rate
        prom.data["T"] = by("service", {"gateway": 0.2})
        o = det.evaluate(T0)[0]
        self.assertEqual((o.state, o.value), (OK, None))
        self.assertIn("below the minimum", o.detail)

    def test_at_the_minimum_the_rule_is_evaluated(self):
        det, prom, _ = run(make_rule(threshold=5, **self.RULE))
        prom.data["Q"] = by("service", {"gateway": 6.26})
        prom.data["T"] = by("service", {"gateway": 0.5})
        self.assertEqual(det.evaluate(T0)[0].state, FIRING)

    def test_no_traffic_series_counts_as_no_traffic(self):
        det, prom, _ = run(make_rule(threshold=5, **self.RULE))
        prom.data["Q"] = by("service", {"gateway": 100.0})
        prom.data["T"] = []
        self.assertEqual(det.evaluate(T0)[0].state, OK)

    def test_traffic_is_judged_per_subject(self):
        det, prom, _ = run(make_rule(threshold=0.5, expected_subjects=["gateway", "inventory"],
                                     min_traffic={"query": "T", "min": 1, "subject_label": "service"}))
        prom.data["Q"] = by("service", {"gateway": 0.955, "inventory": 2.0})
        prom.data["T"] = by("service", {"gateway": 30.0, "inventory": 0.1})
        obs = {o.subject: o.state for o in det.evaluate(T0)}
        self.assertEqual(obs, {"gateway": FIRING, "inventory": OK})

    def test_traffic_query_failure_gives_no_data(self):
        det, prom, _ = run(make_rule(threshold=5, **self.RULE))
        prom.data["Q"] = by("service", {"gateway": 6.0})
        prom.data["T"] = QueryFailed("HTTP 500")
        self.assertEqual(det.evaluate(T0)[0].state, NO_DATA)


class NoDataTests(unittest.TestCase):
    def test_source_not_ok_gives_no_data_without_asking(self):
        for state in (SOURCE_NO_DATA, SOURCE_WARMING_UP):
            det, prom, _ = run(make_rule(expected_subjects=["orders", "gateway"]))
            prom.state, prom.detail = state, "just restarted"
            obs = det.evaluate(T0)
            self.assertEqual({o.state for o in obs}, {NO_DATA}, state)
            self.assertIn(state, obs[0].detail)
            self.assertEqual(prom.queries, [])

    def test_loki_not_ok_does_not_touch_prometheus_rules(self):
        det, prom, loki = run(make_rule(expected_subjects=["orders"]))
        loki.state = SOURCE_NO_DATA
        prom.data["Q"] = by("service", {"orders": 99})
        self.assertEqual(det.evaluate(T0)[0].state, FIRING)

    def test_failed_query_gives_no_data(self):
        det, prom, _ = run(make_rule(expected_subjects=["orders"]))
        prom.data["Q"] = QueryFailed("timed out after 3 s")
        o = det.evaluate(T0)[0]
        self.assertEqual((o.state, o.value), (NO_DATA, None))
        self.assertIn("query failed: timed out after 3 s", o.detail)

    def test_no_data_without_any_known_subject_still_yields_one_observation(self):
        det, prom, _ = run(make_rule())
        prom.state = SOURCE_NO_DATA
        obs = det.evaluate(T0)
        self.assertEqual([(o.subject, o.state) for o in obs], [(None, NO_DATA)])

    def test_nan_and_infinity_are_no_data(self):
        det, prom, _ = run(make_rule())
        prom.data["Q"] = [Sample({"service": "a"}, math.nan), Sample({"service": "b"}, math.inf),
                          Sample({"service": "c"}, 1.0)]
        obs = {o.subject: o.state for o in det.evaluate(T0)}
        self.assertEqual(obs, {"a": NO_DATA, "b": NO_DATA, "c": OK})

    def test_nan_with_too_little_traffic_is_ok(self):
        det, prom, _ = run(make_rule(min_traffic={"query": "T", "min": 1, "subject_label": "service"}))
        prom.data["Q"] = [Sample({"service": "a"}, math.nan)]
        prom.data["T"] = by("service", {"a": 0.0})
        self.assertEqual(det.evaluate(T0)[0].state, OK)


class DetailTests(unittest.TestCase):
    def test_extra_labels_go_into_the_detail(self):
        rule = make_rule(subject_label="job", threshold=0.1, expected_subjects=["orders", "inventory"],
                         provenance={"baseline_max": 0, "fault_value_min": 0.18, "margin_ratio": None, "derived_from": "t"})
        det, prom, _ = run(rule)
        prom.data["Q"] = [Sample({"job": "orders", "dependency": "postgres"}, 0.41),
                          Sample({"job": "orders", "dependency": "redis"}, 0.05),
                          Sample({"job": "inventory", "dependency": "redis"}, 0.5)]
        obs = {o.subject: o for o in det.evaluate(T0)}
        self.assertEqual(obs["orders"].detail, "dependency=postgres (0.41)")      # redis 0.05 does not fire
        self.assertEqual(obs["orders"].value, 0.41)
        self.assertEqual(obs["inventory"].detail, "dependency=redis (0.5)")

    def test_two_firing_dependencies_are_both_named_highest_first(self):
        rule = make_rule(subject_label="job", threshold=0.1, expected_subjects=["orders"],
                         provenance={"baseline_max": 0, "fault_value_min": 0.18, "margin_ratio": None, "derived_from": "t"})
        det, prom, _ = run(rule)
        prom.data["Q"] = [Sample({"job": "orders", "dependency": "redis"}, 0.2),
                          Sample({"job": "orders", "dependency": "postgres"}, 0.9)]
        o = det.evaluate(T0)[0]
        self.assertEqual(o.detail, "dependency=postgres (0.9); dependency=redis (0.2)")
        self.assertEqual(o.value, 0.9)

    def test_plain_series_have_no_detail(self):
        det, prom, _ = run(make_rule())
        prom.data["Q"] = [Sample({"service": "a", "instance": "a:8000", "job": "a", "__name__": "up"}, 99.0)]
        self.assertEqual(det.evaluate(T0)[0].detail, "")


class ConstructionTests(unittest.TestCase):
    def test_a_rule_with_an_unconfigured_source_is_an_error(self):
        with self.assertRaises(ValueError):
            ThresholdDetector([make_rule()], {"loki": FakeSource("loki")})

    def test_detector_has_a_name(self):
        det, _, _ = run(make_rule())
        self.assertEqual(det.name, "threshold")


if __name__ == "__main__":
    unittest.main()
