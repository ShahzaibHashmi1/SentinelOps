import unittest

import _bootstrap  # noqa: F401
import fakes
from fakes import SIGNALS, at, pts

from app import anomaly as an
from app.model import Point

CPU = SIGNALS.by_name("cpu_pct_of_limit")
UP = SIGNALS.by_name("up")
PRESENT = SIGNALS.by_name("container_present")
RESTARTS = SIGNALS.by_name("restarts")
ERR5 = SIGNALS.by_name("error_ratio_5xx")
LAT = SIGNALS.by_name("latency_p95")
RATE = SIGNALS.by_name("request_rate")
DEPERR = SIGNALS.by_name("dependency_error_rate")


def ev(defn, service, window, baseline=(), **kw):
    return an.evaluate_series(defn, service, kw.pop("label", None), window, baseline, **kw)


class HighIsBadTests(unittest.TestCase):
    def test_absolute_threshold_without_baseline(self):
        o = ev(CPU, "orders", pts([20, 50, 90, 80]))
        self.assertTrue(o.result.anomalous)
        self.assertEqual(o.result.window_max, 90)
        self.assertEqual(o.result.first_anomalous_at, at(10))
        self.assertEqual(o.result.threshold, 85)

    def test_strictly_greater(self):
        o = ev(CPU, "orders", pts([85, 85, 85]))
        self.assertFalse(o.result.anomalous)
        self.assertIsNone(o.result.first_anomalous_at)

    def test_first_step_that_passes(self):
        o = ev(CPU, "orders", pts([10, 86, 20, 99]))
        self.assertEqual(o.result.first_anomalous_at, at(5))

    def test_baseline_raises_the_threshold_when_clean(self):
        # baseline max 70 -> 1.5 x 70 = 105 beats the absolute 85
        o = ev(CPU, "orders", pts([100]), pts([70, 60]), baseline_clean=True)
        self.assertFalse(o.result.anomalous)
        self.assertEqual(o.result.threshold, 105)
        self.assertEqual(o.result.baseline_max, 70)

    def test_unclean_baseline_uses_only_the_absolute_threshold(self):
        o = ev(CPU, "orders", pts([100]), pts([70, 60]), baseline_clean=False)
        self.assertTrue(o.result.anomalous)
        self.assertEqual(o.result.threshold, 85)

    def test_small_baseline_keeps_the_absolute_threshold(self):
        o = ev(CPU, "orders", pts([86]), pts([20]), baseline_clean=True)
        self.assertEqual(o.result.threshold, 85)
        self.assertTrue(o.result.anomalous)

    def test_ratio(self):
        o = ev(CPU, "orders", pts([170]))
        self.assertAlmostEqual(o.result.ratio, 2.0)

    def test_values_above_100_are_normal_for_cpu(self):
        o = ev(CPU, "orders", pts([110.1]))
        self.assertTrue(o.result.anomalous)

    def test_empty_window_is_no_value_not_healthy_not_failure(self):
        o = ev(CPU, "orders", [])
        self.assertEqual(o.state, "ok")
        self.assertIsNone(o.result.window_max)
        self.assertFalse(o.result.anomalous)

    def test_empty_counts_as_zero_when_configured(self):
        o = ev(RATE, "gateway", [])
        self.assertEqual(o.result.window_max, 0.0)
        self.assertEqual(o.result.baseline_max, 0.0)

    def test_signal_without_threshold_is_reported_without_verdict(self):
        o = ev(RATE, "orders", pts([999]))
        self.assertFalse(o.result.anomalous)
        self.assertEqual(o.result.window_max, 999)
        self.assertIsNone(o.result.threshold)

    def test_request_rate_gateway_surge(self):
        o = ev(RATE, "gateway", pts([19, 25, 31.77]), pts([18.97]), baseline_clean=True)
        # 1.5 x 18.97 = 28.455 < 30: the absolute threshold decides
        self.assertTrue(o.result.anomalous)
        self.assertEqual(o.result.threshold, 30)
        self.assertEqual(o.result.first_anomalous_at, at(10))

    def test_unsorted_points_are_sorted(self):
        window = list(reversed(pts([10, 90, 95])))
        self.assertEqual(ev(CPU, "orders", window).result.first_anomalous_at, at(5))


class GuardTests(unittest.TestCase):
    def test_quiet_guard_drops_steps_without_traffic(self):
        window = pts([100, 100, 100])  # 100 % 5xx
        guard = pts([0.1, 0.2, 2.0])  # traffic below 0.5 req/s on the first two steps
        o = ev(ERR5, "gateway", window, guard_window=guard, guard_baseline=[])
        self.assertTrue(o.result.anomalous)
        self.assertEqual(o.result.first_anomalous_at, at(10))

    def test_quiet_guard_without_traffic_is_ok_and_quiet(self):
        o = ev(ERR5, "gateway", pts([100, 100]), guard_window=pts([0.0, 0.1]), guard_baseline=[])
        self.assertEqual(o.state, "ok")
        self.assertFalse(o.result.anomalous)

    def test_unavailable_guard_is_no_data(self):
        o = ev(ERR5, "gateway", pts([100]), guard_window=None)
        self.assertEqual(o.state, "no_data")
        self.assertIsNone(o.result)

    def test_no_data_guard_failure_for_container_present(self):
        # every step has value 0 and the guard (count of series) is missing: cannot tell cAdvisor failure from outage
        o = ev(PRESENT, "postgres", pts([0, 0, 0]), guard_window=[], guard_baseline=[])
        self.assertEqual(o.state, "no_data")
        self.assertIn("guard", o.detail)

    def test_container_present_with_guard_ok(self):
        o = ev(PRESENT, "postgres", pts([1, 1, 0, 0]), pts([1, 1]), guard_window=pts([5, 5, 5, 5]), guard_baseline=pts([5, 5]))
        self.assertTrue(o.result.anomalous)
        self.assertEqual(o.result.first_anomalous_at, at(10))

    def test_baseline_without_guard_data_is_ignored(self):
        o = ev(ERR5, "gateway", pts([9]), pts([100]), guard_window=pts([5]), guard_baseline=None)
        self.assertIsNone(o.result.baseline_max)


class DownTests(unittest.TestCase):
    def test_up_zero_is_anomalous(self):
        o = ev(UP, "inventory", pts([1, 1, 0, 0]))
        self.assertTrue(o.result.anomalous)
        self.assertEqual(o.result.first_anomalous_at, at(10))
        self.assertIsNone(o.result.ratio)

    def test_up_one_is_not(self):
        self.assertFalse(ev(UP, "inventory", pts([1, 1, 1])).result.anomalous)

    def test_empty_is_not_presence_zero(self):
        o = ev(UP, "inventory", [])
        self.assertEqual(o.state, "ok")
        self.assertFalse(o.result.anomalous)
        self.assertIsNone(o.result.window_max)

    def test_window_max_is_reported_for_down_signals(self):
        self.assertEqual(ev(UP, "inventory", pts([1, 0])).result.window_max, 1)


class RestartTests(unittest.TestCase):
    BASE = 1_791_142_090.0

    def series(self, values):
        return [Point(fakes.T0.timestamp() + 5 * i, v) for i, v in enumerate(values) if v is not None]

    def test_start_time_increase_is_a_restart(self):
        o = ev(RESTARTS, "payments", self.series([self.BASE, self.BASE, self.BASE + 31, self.BASE + 31]))
        self.assertTrue(o.result.anomalous)
        self.assertEqual(o.result.window_max, 1)
        self.assertEqual(o.result.first_anomalous_at, at(10))

    def test_crash_loop_counts_every_increase(self):
        o = ev(RESTARTS, "inventory", self.series([self.BASE, self.BASE + 10, self.BASE + 25, self.BASE + 50]))
        self.assertEqual(o.result.window_max, 3)
        self.assertEqual(o.result.first_anomalous_at, at(5))

    def test_no_restart(self):
        o = ev(RESTARTS, "payments", self.series([self.BASE] * 6))
        self.assertFalse(o.result.anomalous)
        self.assertEqual(o.result.window_max, 0)

    def test_gap_then_later_start_time_counts(self):
        # the container vanished and came back with a later start time
        o = ev(RESTARTS, "postgres", self.series([self.BASE, None, None, self.BASE + 40]))
        self.assertEqual(o.result.window_max, 1)

    def test_increase_across_the_baseline_edge_counts(self):
        base = [Point(fakes.T0.timestamp() - 5, self.BASE)]
        o = ev(RESTARTS, "payments", self.series([self.BASE + 20]), base)
        self.assertEqual(o.result.window_max, 1)

    def test_sub_second_noise_is_not_a_restart(self):
        o = ev(RESTARTS, "payments", self.series([self.BASE, self.BASE + 0.2, self.BASE + 0.4]))
        self.assertEqual(o.result.window_max, 0)

    def test_baseline_restarts_raise_the_threshold(self):
        base = [Point(fakes.T0.timestamp() - 30 + 5 * i, v) for i, v in enumerate([self.BASE, self.BASE + 10, self.BASE + 20, self.BASE + 30])]
        o = ev(RESTARTS, "payments", self.series([self.BASE + 30, self.BASE + 60]), base, baseline_clean=True)
        self.assertEqual(o.result.baseline_max, 3)
        self.assertEqual(o.result.threshold, 4.5)
        self.assertFalse(o.result.anomalous)


class GroupedTests(unittest.TestCase):
    def test_one_outcome_per_label_sorted(self):
        out = an.evaluate_grouped(DEPERR, "orders", {"postgres": pts([0, 0.4]), "inventory": pts([0, 0])}, {"postgres": pts([0])})
        self.assertEqual([o.name for o in out], ["dependency_error_rate:inventory", "dependency_error_rate:postgres"])
        self.assertFalse(out[0].result.anomalous)
        self.assertTrue(out[1].result.anomalous)
        self.assertEqual(out[1].result.label, "postgres")

    def test_label_only_in_baseline_is_reported(self):
        out = an.evaluate_grouped(DEPERR, "orders", {}, {"redis": pts([0.0])})
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].result.window_max)

    def test_empty_groups_give_nothing(self):
        self.assertEqual(an.evaluate_grouped(DEPERR, "orders", {}, {}), [])


class NoDataTests(unittest.TestCase):
    def test_failed_query_is_neither_healthy_nor_anomalous(self):
        o = an.no_data(CPU, None, "timeout after 3 s")
        self.assertEqual(o.state, "no_data")
        self.assertIsNone(o.result)

    def test_split_outcomes(self):
        ok = ev(CPU, "orders", pts([10]))
        bad = an.no_data(LAT, None, "HTTP 503")
        results, gaps = an.split_outcomes([ok, bad])
        self.assertEqual([r.name for r in results], ["cpu_pct_of_limit"])
        self.assertEqual(gaps, [("latency_p95", "HTTP 503")])


class ToEntryTests(unittest.TestCase):
    def test_entry_has_exactly_the_contract_fields(self):
        e = ev(CPU, "orders", pts([90])).result.to_entry()
        self.assertEqual(list(e), ["name", "unit", "window_max", "baseline_max", "anomalous", "first_anomalous_at"])
        self.assertEqual(e["first_anomalous_at"], "2026-11-03T10:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
