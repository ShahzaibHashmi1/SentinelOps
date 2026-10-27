import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from _bootstrap import CATALOGUE_JSON, RULES_JSON
from app import rules as rules_module
from app.model import ROLES
from app.rules import RulesError, load_rules, parse_rules, provenance_table


def good_rule(**changes):
    rule = {
        "id": "R90", "name": "cpu_test", "detector": "threshold", "source": "prometheus",
        "query": "up", "subject_label": "service", "comparator": ">", "threshold": 85, "for_s": 10,
        "severity": "medium", "role": "root_signal", "opens_incident": True, "on_missing": "ok",
        "expected_subjects": ["orders"], "guard": None, "min_traffic": None,
        "provenance": {"baseline_max": 44, "fault_value_min": 90.92, "margin_ratio": 1.932,
                       "derived_from": "test"},
        "description": "test rule"}
    rule.update(changes)
    return rule


def doc(*rules):
    return {"schema_version": 1, "rules": list(rules) or [good_rule()]}


def ungroup(query):
    """sum by (service) (x) -> sum(x), max by (service) (x) -> max(x)"""
    return query.replace("sum by (service) (", "sum(").replace("max by (service) (", "max(")


def problems_of(document):
    try:
        parse_rules(document)
    except RulesError as exc:
        return exc.problems
    return []


class ValidationTests(unittest.TestCase):
    def test_good_rule_is_valid(self):
        self.assertEqual(problems_of(doc()), [])

    def test_every_required_field_is_required(self):
        for key in rules_module.REQUIRED_KEYS:
            rule = good_rule()
            del rule[key]
            self.assertTrue(any(f"missing fields ['{key}']" in p for p in problems_of(doc(rule))), key)

    def test_optional_fields_may_be_absent(self):
        rule = good_rule()
        del rule["guard"], rule["min_traffic"]
        self.assertEqual(problems_of(doc(rule)), [])

    def test_unknown_field_is_rejected(self):
        self.assertTrue(any("unknown fields ['extra']" in p for p in problems_of(doc(good_rule(extra=1)))))

    def test_bad_comparator(self):
        self.assertTrue(any("comparator" in p for p in problems_of(doc(good_rule(comparator=">=")))))

    def test_bad_source_severity_role_detector_on_missing(self):
        for key, value in (("source", "graphite"), ("severity", "urgent"), ("role", "boss"),
                           ("detector", "ewma"), ("on_missing", "fail")):
            self.assertTrue(any(key in p for p in problems_of(doc(good_rule(**{key: value})))), key)

    def test_threshold_and_for_s_must_be_numbers(self):
        self.assertTrue(problems_of(doc(good_rule(threshold="85"))))
        self.assertTrue(problems_of(doc(good_rule(threshold=True))))
        self.assertTrue(problems_of(doc(good_rule(for_s=-1))))
        self.assertTrue(problems_of(doc(good_rule(for_s="10"))))

    def test_opens_incident_must_be_boolean_and_evidence_never_opens(self):
        self.assertTrue(problems_of(doc(good_rule(opens_incident="yes"))))
        self.assertTrue(any("evidence" in p for p in problems_of(doc(good_rule(role="evidence", opens_incident=True)))))
        self.assertEqual(problems_of(doc(good_rule(role="evidence", opens_incident=False))), [])

    def test_zero_needs_expected_subjects_and_subject_label(self):
        self.assertTrue(any("needs expected_subjects" in p
                            for p in problems_of(doc(good_rule(on_missing="zero", expected_subjects=None)))))
        self.assertTrue(any("needs a subject_label" in p
                            for p in problems_of(doc(good_rule(on_missing="zero", subject_label=None)))))

    def test_expected_subjects_shape(self):
        self.assertTrue(problems_of(doc(good_rule(expected_subjects=[]))))
        self.assertTrue(problems_of(doc(good_rule(expected_subjects=["a", "a"]))))
        self.assertTrue(problems_of(doc(good_rule(expected_subjects="orders"))))

    def test_duplicate_ids(self):
        self.assertTrue(any("duplicate id" in p for p in problems_of(doc(good_rule(), good_rule()))))

    def test_bad_id(self):
        self.assertTrue(problems_of(doc(good_rule(id="cpu"))))

    def test_min_traffic_shape(self):
        ok = {"query": "q", "min": 0.5, "subject_label": "service"}
        self.assertEqual(problems_of(doc(good_rule(min_traffic=ok))), [])
        self.assertTrue(problems_of(doc(good_rule(min_traffic={"query": "q", "min": 0, "subject_label": None}))))
        self.assertTrue(problems_of(doc(good_rule(min_traffic={"query": "q"}))))

    def test_top_level_shape(self):
        self.assertTrue(problems_of([]))
        self.assertTrue(problems_of({"schema_version": 2, "rules": [good_rule()]}))
        self.assertTrue(problems_of({"schema_version": 1, "rules": []}))
        self.assertTrue(problems_of({"schema_version": 1, "rules": [good_rule()], "other": 1}))

    def test_all_problems_are_listed_not_just_the_first(self):
        found = problems_of(doc(good_rule(comparator="?", severity="x")))
        self.assertGreaterEqual(len(found), 2)


class MarginRuleTests(unittest.TestCase):
    def prov(self, **changes):
        p = {"baseline_max": 44, "fault_value_min": 90.92, "margin_ratio": 1.932, "derived_from": "test"}
        p.update(changes)
        return p

    def test_threshold_below_one_and_a_half_times_baseline_is_rejected(self):
        found = problems_of(doc(good_rule(threshold=60, provenance=self.prov(margin_ratio=1.364))))
        self.assertTrue(any("margin rule violated" in p and "baseline_max" in p for p in found))

    def test_threshold_exactly_one_and_a_half_times_baseline_is_accepted(self):
        self.assertEqual(problems_of(doc(good_rule(threshold=66, provenance=self.prov(margin_ratio=1.5)))), [])

    def test_threshold_above_first_fault_reading_is_rejected(self):
        found = problems_of(doc(good_rule(threshold=95, provenance=self.prov(margin_ratio=2.159))))
        self.assertTrue(any("above the first fault reading" in p for p in found))

    def test_margin_ratio_must_match(self):
        found = problems_of(doc(good_rule(provenance=self.prov(margin_ratio=3.0))))
        self.assertTrue(any("does not match" in p for p in found))

    def test_margin_ratio_required_with_positive_baseline(self):
        found = problems_of(doc(good_rule(provenance=self.prov(margin_ratio=None))))
        self.assertTrue(any("margin_ratio is required" in p for p in found))

    def test_zero_baseline_means_null_margin_ratio(self):
        ok = self.prov(baseline_max=0, fault_value_min=6.26, margin_ratio=None)
        self.assertEqual(problems_of(doc(good_rule(threshold=5, provenance=ok))), [])
        bad = self.prov(baseline_max=0, fault_value_min=6.26, margin_ratio=1.0)
        self.assertTrue(problems_of(doc(good_rule(threshold=5, provenance=bad))))

    def test_state_rule_has_no_margin_ratio(self):
        ok = self.prov(baseline_max=1, fault_value_min=0, margin_ratio=None)
        self.assertEqual(problems_of(doc(good_rule(comparator="==", threshold=0, provenance=ok))), [])
        bad = self.prov(baseline_max=1, fault_value_min=0, margin_ratio=0.0)
        self.assertTrue(problems_of(doc(good_rule(comparator="==", threshold=0, provenance=bad))))

    def test_missing_numbers_need_a_pending_marker(self):
        empty = self.prov(baseline_max=None, fault_value_min=None, margin_ratio=None, derived_from="not measured")
        self.assertTrue(any("PENDING" in p for p in problems_of(doc(good_rule(provenance=empty)))))
        pending = dict(empty, derived_from="PENDING: baseline not measured")
        self.assertEqual(problems_of(doc(good_rule(threshold=0, provenance=pending))), [])

    def test_provenance_shape(self):
        self.assertTrue(problems_of(doc(good_rule(provenance={"baseline_max": 1}))))
        self.assertTrue(problems_of(doc(good_rule(provenance=self.prov(extra=1)))))
        self.assertTrue(problems_of(doc(good_rule(provenance=self.prov(baseline_max="44")))))
        self.assertTrue(problems_of(doc(good_rule(provenance=self.prov(derived_from="")))))


class FileLoadingTests(unittest.TestCase):
    def test_missing_file_and_bad_json_give_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RulesError) as ctx:
                load_rules(Path(tmp) / "nope.json")
            self.assertIn("cannot read", str(ctx.exception))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(RulesError) as ctx:
                load_rules(bad)
            self.assertIn("not valid JSON", str(ctx.exception))

    def test_a_file_saved_with_a_utf8_bom_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bom.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc()).encode("utf-8"))
            self.assertEqual([r.id for r in load_rules(path)], ["R90"])

    def test_main_returns_1_for_an_invalid_file_and_0_for_the_shipped_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(doc(good_rule(comparator="?"))), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(rules_module.main([str(bad)]), 1)
            self.assertIn("RULES INVALID", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rules_module.main([str(RULES_JSON)]), 0)
        self.assertIn("9 rules valid", out.getvalue())
        self.assertTrue(out.getvalue().rstrip().endswith("pending provenance: none"))


class ShippedRulesTests(unittest.TestCase):
    """detector/rules/rules.json as delivered in CP1."""

    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules(RULES_JSON)
        cls.by_id = {r.id: r for r in cls.rules}

    def test_the_nine_rules_of_the_prompt(self):
        self.assertEqual([r.id for r in self.rules], [f"R0{i}" for i in range(1, 10)])
        self.assertEqual([r.name for r in self.rules],
                         ["target_down", "container_missing", "cpu_high", "memory_high", "gateway_5xx_pct",
                          "dependency_errors", "latency_p95", "throughput_surge", "log_errors"])

    def test_conditions_and_holds_match_the_prompt_table(self):
        table = {"R01": ("==", 0, 10, "root_signal", True), "R02": ("==", 0, 10, "root_signal", True),
                 "R03": (">", 85, 10, "root_signal", True), "R04": (">", 80, 10, "root_signal", True),
                 "R05": (">", 5, 15, "effect", True), "R06": (">", 0.1, 10, "dependency_effect", True),
                 "R07": (">", 0.5, 15, "effect", True), "R08": (">", 30, 15, "load", True),
                 "R09": (">", 0, 10, "evidence", False)}
        for rid, (cmp_, thr, hold, role, opens) in table.items():
            r = self.by_id[rid]
            self.assertEqual((r.comparator, r.threshold, r.for_s, r.role, r.opens_incident),
                             (cmp_, thr, hold, role, opens), rid)

    def test_all_roles_are_known_and_only_r09_is_evidence(self):
        self.assertTrue(all(r.role in ROLES for r in self.rules))
        self.assertEqual([r.id for r in self.rules if r.role == "evidence"], ["R09"])

    def test_sources(self):
        self.assertEqual([r.id for r in self.rules if r.source == "loki"], ["R09"])

    def test_r02_is_a_zero_rule_for_the_six_containers_with_a_guard(self):
        r = self.by_id["R02"]
        self.assertEqual(r.on_missing, "zero")
        self.assertEqual(sorted(r.expected_subjects), sorted(["gateway", "orders", "payments", "inventory", "postgres", "redis"]))
        self.assertIn('job="cadvisor"', r.guard)

    def test_rate_guards(self):
        self.assertEqual(self.by_id["R05"].min_traffic.min, 0.5)
        self.assertEqual(self.by_id["R07"].min_traffic.min, 1)
        self.assertIsNone(self.by_id["R08"].min_traffic)

    def test_margin_rule_numbers_of_the_prompt(self):
        r = self.by_id
        self.assertEqual((r["R03"].provenance.baseline_max, r["R03"].provenance.fault_value_min), (44, 90.92))
        self.assertEqual((r["R04"].provenance.baseline_max, r["R04"].provenance.fault_value_min), (33.55, 83.54))
        self.assertEqual(r["R04"].provenance.margin_ratio, 2.385)
        self.assertEqual((r["R07"].provenance.baseline_max, r["R07"].provenance.fault_value_min), (0.2107, 0.955))
        self.assertEqual(r["R07"].provenance.margin_ratio, 2.373)
        self.assertEqual((r["R09"].provenance.baseline_max, r["R09"].provenance.fault_value_min), (0, 1))
        self.assertEqual(r["R09"].threshold, 0)
        self.assertIsNone(r["R09"].provenance.margin_ratio)
        self.assertEqual((r["R08"].provenance.baseline_max, r["R08"].provenance.fault_value_min), (18.97, 31.77))
        self.assertEqual(r["R06"].provenance.fault_value_min, 0.18)
        self.assertEqual(r["R05"].provenance.fault_value_min, 6.26)
        self.assertEqual(r["R07"].provenance.fault_value_min, 0.955)
        # 1.5 x the highest baseline never exceeds the threshold, and the threshold never exceeds the fault
        for rule in self.rules:
            p = rule.provenance
            if rule.comparator == ">" and p.baseline_max is not None:
                self.assertGreaterEqual(rule.threshold + 1e-9, 1.5 * p.baseline_max, rule.id)
                self.assertLessEqual(rule.threshold, p.fault_value_min + 1e-9, rule.id)

    def test_derived_from_names_the_baseline_that_the_rule_uses(self):
        """The text and the number must not drift apart: 'baseline_max <number>' appears in derived_from."""
        for rule in self.rules:
            baseline = rule.provenance.baseline_max
            if rule.comparator == ">" and baseline:                      # R03, R04, R07, R08 (R05, R06, R09 have baseline 0)
                self.assertIn(f"baseline_max {baseline:g}", rule.provenance.derived_from, rule.id)
        checked = [r.id for r in self.rules if r.comparator == ">" and r.provenance.baseline_max]
        self.assertEqual(checked, ["R03", "R04", "R07", "R08"])

    def test_the_highest_measured_baselines_of_the_owners_runs_are_below_one_and_a_half_times_the_threshold(self):
        """Highest values of the full detector_check.py run (10 users): memory of payments 33.55 %, p95 of the gateway 0.2107 s."""
        r = self.by_id
        self.assertLessEqual(1.5 * 33.55, r["R04"].threshold)
        self.assertLessEqual(1.5 * 0.2107, r["R07"].threshold)
        self.assertEqual(r["R04"].provenance.baseline_max, 33.55)
        self.assertEqual(r["R07"].provenance.baseline_max, 0.2107)

    def test_no_rule_has_pending_provenance(self):
        """R09 was PENDING in CP1; its Loki baseline is measured now (0 lines), so nothing is pending."""
        self.assertEqual([r.id for r in self.rules if r.provenance.pending], [])
        for rule in self.rules:
            self.assertIsNotNone(rule.provenance.baseline_max, rule.id)
            self.assertIsNotNone(rule.provenance.fault_value_min, rule.id)

    def test_r09_provenance_says_where_the_fault_value_comes_from(self):
        text = self.by_id["R09"].provenance.derived_from
        self.assertFalse(text.startswith("PENDING"))
        self.assertIn("DIFFERENT query", text)       # fault_value_min 1 is F13 evidence of another query

    def test_provenance_table_lists_every_rule(self):
        text = provenance_table(self.rules)
        for rule in self.rules:
            self.assertIn(rule.id, text)
        self.assertNotIn("PENDING", text)

    def test_no_query_uses_a_service_label_on_the_scrape_job_or_the_detectors_own_metrics(self):
        for rule in self.rules:
            self.assertNotIn("exported_service", rule.query)
            self.assertNotIn("detector_", rule.query)

    def test_prometheus_queries_exclude_health_ready_metrics_routes_where_they_count_requests(self):
        for rid in ("R05", "R07", "R08"):
            rule = self.by_id[rid]
            self.assertIn('route!~"/(health|ready|metrics)"', rule.query, rid)

    @unittest.skipUnless(CATALOGUE_JSON.exists(), "chaos/catalogue.json not found")
    def test_queries_are_the_ones_of_the_m3_catalogue(self):
        catalogue = json.loads(CATALOGUE_JSON.read_text(encoding="utf-8"))
        symptoms = {f["id"]: f["expected_symptoms"] for f in catalogue}
        f10_5xx = next(s["query"] for s in symptoms["F10"] if "status=~" in s["query"])  # query A
        self.assertIn(f10_5xx, self.by_id["R05"].query)
        f18_rps = next(s["query"] for s in symptoms["F18"] if "histogram_quantile" not in s["query"])
        self.assertIn(f18_rps, self.by_id["R08"].query)
        self.assertIn(f18_rps, self.by_id["R05"].min_traffic.query)
        f10_container = next(s["query"] for s in symptoms["F10"] if "count(container_memory" in s["query"])
        self.assertIn("container_memory_working_set_bytes", f10_container)
        # R03/R04 are the F01/F02 queries without the service filter, grouped by service
        f01 = symptoms["F01"][0]["query"].replace('{service="$target"}', "")
        self.assertEqual(ungroup(self.by_id["R03"].query), f01)
        f02 = symptoms["F02"][0]["query"].replace('{service="$target"}', "")
        self.assertEqual(ungroup(self.by_id["R04"].query), f02)
        # R07 is the F11/F18 p95 query, grouped by service, without the service filter
        f18_p95 = next(s["query"] for s in symptoms["F18"] if "histogram_quantile" in s["query"])
        expected = f18_p95.replace('service="gateway",', "").replace("sum by (le)", "sum by (le, service)")
        self.assertEqual(self.by_id["R07"].query, expected)


if __name__ == "__main__":
    unittest.main()
