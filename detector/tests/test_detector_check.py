"""Unit tests for the pure helpers of scripts/detector_check.py. No Docker, no network, standard library only."""
import ast
import importlib.util
import json
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401
from _bootstrap import CATALOGUE_JSON, REPO_DIR, RULES_JSON

SCRIPT = REPO_DIR / "scripts" / "detector_check.py"
spec = importlib.util.spec_from_file_location("detector_check", SCRIPT)
dc = importlib.util.module_from_spec(spec)
sys.modules["detector_check"] = dc
spec.loader.exec_module(dc)

T0 = datetime(2026, 10, 21, 1, 27, 50, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def event(kind, run_id="F10-0123abcd", fault="F10", target="postgres", at=T0, **extra):
    base = {"run_id": run_id, "event": kind, "fault_id": fault, "category": "Dependency", "target": target, "params": {},
            "ts": iso(at), "expected_root_cause": {"service": target, "type": "x"}, "expected_action": "restart_service",
            "acceptable_actions": ["restart_service"]}
    base.update(extra)
    return base


def good_events(**kw):
    return [event("inject", at=T0, **kw), event("clear", at=T0 + timedelta(seconds=90), **kw),
            event("recovered", at=T0 + timedelta(seconds=98), recovery_s=8.0, **kw)]


def symptom(rule, service, active=False, first=T0 + timedelta(seconds=12)):
    return {"symptom_id": "SYM-00001", "rule_id": rule, "detector": "threshold", "source": "prometheus", "service": service,
            "role": "root_signal", "severity": "high", "opens_incident": True, "first_seen": iso(first), "last_seen": iso(first),
            "active": active, "flaps": 0, "comparator": "==", "threshold": 0.0, "peak_value": 0.0, "last_value": 0.0, "detail": ""}


def incident(incident_id="INC-0002", opened=T0 + timedelta(seconds=22), resolved=T0 + timedelta(seconds=98 + 70),
             symptoms=None, primary="postgres", reopen=0, status="resolved", opened_iso=None):
    syms = symptoms if symptoms is not None else [symptom("R02", "postgres"), symptom("R05", "gateway"), symptom("R06", "orders")]
    return {"incident_id": incident_id, "schema_version": 1, "status": status, "opened_at": opened_iso or iso(opened),
            "updated_at": iso(opened), "resolved_at": iso(resolved) if resolved else None, "reopen_count": reopen,
            "severity": "high", "title": "container_missing postgres", "primary_service": primary,
            "candidate_services": ["postgres", "gateway", "orders"], "affected_services": ["postgres", "gateway"],
            "symptoms": syms, "data_gaps": []}


REFERENCE = T0 + timedelta(seconds=2)            # end of the inject: 2 s after the inject ts
RECOVERED = T0 + timedelta(seconds=98)
F10 = dc.EXPECTATIONS["F10"]


class TimeTests(unittest.TestCase):
    def test_parse_ts(self):
        self.assertEqual(dc.parse_ts("2026-10-21T01:27:55.974Z"), datetime(2026, 10, 21, 1, 27, 55, 974000, tzinfo=timezone.utc))
        self.assertEqual(dc.parse_ts("2026-10-21T06:27:55.974+05:00"), dc.parse_ts("2026-10-21T01:27:55.974Z"))
        self.assertEqual(dc.parse_ts("2026-10-21T01:27:55").tzinfo, timezone.utc)      # no zone: UTC, never local time

    def test_skew_from_date(self):
        now = datetime(2026, 10, 21, 1, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(dc.skew_from_date("Wed, 21 Oct 2026 01:30:02 GMT", now), 2.0)
        self.assertEqual(dc.skew_from_date("Wed, 21 Oct 2026 01:29:58 GMT", now), -2.0)
        self.assertIsNone(dc.skew_from_date(None, now))
        self.assertIsNone(dc.skew_from_date("not a date", now))


class GroundTruthTests(unittest.TestCase):
    def test_parse_event_lines(self):
        raw = (json.dumps(event("inject")) + "\n" + "{broken\n" + "\n" + json.dumps(event("clear")) + "\n").encode()
        events, bad = dc.parse_event_lines(raw)
        self.assertEqual([e["event"] for e in events], ["inject", "clear"])
        self.assertEqual(bad, [2])

    def test_run_events_keeps_file_order_and_the_one_run(self):
        mixed = [event("inject", run_id="F01-aaaaaaaa", fault="F01"), event("inject"), event("clear"), event("recovered", recovery_s=1),
                 event("clear", run_id="F01-aaaaaaaa", fault="F01")]
        self.assertEqual([e["event"] for e in dc.run_events(mixed, "F10-0123abcd")], ["inject", "clear", "recovered"])

    def test_a_good_run(self):
        self.assertEqual(dc.run_problems(good_events(), "F10", "postgres"), [])

    def test_equal_timestamps_are_fine(self):
        events = good_events()
        events[2]["ts"] = events[1]["ts"]
        self.assertEqual(dc.run_problems(events, "F10", "postgres"), [])

    def test_wrong_order_or_missing_events(self):
        events = good_events()
        self.assertIn("expected ['inject', 'clear', 'recovered']", dc.run_problems(events[:2], "F10", "postgres")[0])
        self.assertTrue(dc.run_problems([events[1], events[0], events[2]], "F10", "postgres"))
        aborted = [events[0], event("aborted", reason="interrupted by Ctrl+C"), events[1], events[2]]
        self.assertTrue(dc.run_problems(aborted, "F10", "postgres"))

    def test_late_flag_is_a_problem(self):
        events = good_events()
        events[1]["late"] = True
        self.assertIn("late flag", " ".join(dc.run_problems(events, "F10", "postgres")))

    def test_wrong_fault_target_run_id_and_timestamps(self):
        self.assertTrue(dc.run_problems(good_events(), "F13", "postgres"))
        self.assertTrue(dc.run_problems(good_events(), "F10", "redis"))
        events = good_events()
        events[1]["run_id"] = "F10-ffffffff"
        self.assertIn("run_id", " ".join(dc.run_problems(events, "F10", "postgres")))
        events = good_events()
        events[1]["ts"], events[2]["ts"] = events[2]["ts"], events[1]["ts"]
        self.assertIn("inject <= clear <= recovered", " ".join(dc.run_problems(events, "F10", "postgres")))
        events = good_events()
        events[0]["ts"] = "2026-10-21 01:27:50"
        self.assertIn("ISO 8601", " ".join(dc.run_problems(events, "F10", "postgres")))

    def test_injected_line(self):
        self.assertEqual(dc.parse_injected_line("injected F10 on postgres (run id F10-3f9a1c2e)"), ("F10", "postgres", "F10-3f9a1c2e"))
        self.assertIsNone(dc.parse_injected_line("run F10: warm-up 0 s, hold 90 s, cool-down 0 s (Ctrl+C clears the fault)"))
        self.assertIsNone(dc.parse_injected_line("cleared F10 (run id F10-3f9a1c2e)"))


class IncidentMatchingTests(unittest.TestCase):
    def test_snapshot_and_changes(self):
        old = incident("INC-0001")
        before = dc.incident_snapshot([old])
        self.assertEqual(before, {"INC-0001": ("resolved", 0)})
        self.assertEqual(dc.snapshot_changes(before, before), [])
        after = dc.incident_snapshot([old, incident("INC-0002", status="open", resolved=None)])
        self.assertEqual(dc.snapshot_changes(before, after), ["INC-0002 was created (open)"])
        reopened = dc.incident_snapshot([incident("INC-0001", status="open", resolved=None, reopen=1)])
        self.assertIn("INC-0001 changed", dc.snapshot_changes(before, reopened)[0])
        self.assertIn("disappeared", dc.snapshot_changes(before, {})[0])

    def test_older_incidents_are_ignored(self):
        before = dc.incident_snapshot([incident("INC-0001"), incident("INC-0002")])
        new, reopened = dc.incidents_for_run([incident("INC-0002"), incident("INC-0001")], before)
        self.assertEqual((new, reopened), ([], []))

    def test_the_new_incident_is_found_and_old_ones_stay_out(self):
        before = dc.incident_snapshot([incident("INC-0001")])
        run_incident = incident("INC-0002")
        new, reopened = dc.incidents_for_run([run_incident, incident("INC-0001")], before)
        self.assertEqual(([i["incident_id"] for i in new], reopened), (["INC-0002"], []))

    def test_a_reopened_old_incident_is_reported(self):
        before = dc.incident_snapshot([incident("INC-0001")])
        new, reopened = dc.incidents_for_run([incident("INC-0001", reopen=1, status="open", resolved=None)], before)
        self.assertEqual((new, reopened), ([], ["INC-0001"]))

    def test_two_new_incidents_are_sorted_by_opening_time(self):
        a = incident("INC-0003", opened=T0 + timedelta(seconds=60))
        b = incident("INC-0002", opened=T0 + timedelta(seconds=20))
        new, _ = dc.incidents_for_run([a, b], {})
        self.assertEqual([i["incident_id"] for i in new], ["INC-0002", "INC-0003"])


class IncidentChecksTests(unittest.TestCase):
    def checks(self, inc, expectation=F10):
        return {name: (ok, detail) for name, ok, detail in dc.incident_checks(inc, expectation, REFERENCE, T0, RECOVERED)}

    def test_a_good_incident_passes_every_check(self):
        checks = dc.incident_checks(incident(), F10, REFERENCE, T0, RECOVERED)
        self.assertEqual([c[0] for c in checks], ["opened within 50 s of the end of the inject",
                                                  "the symptoms include R02 (postgres), R05",
                                                  "primary_service is postgres",
                                                  "resolved within 180 s after the recovered event",
                                                  "not reopened and no symptom still active"])
        self.assertTrue(all(ok for _, ok, _ in checks), checks)
        self.assertIn("20.0 s after the end of the inject, 22.0 s after the inject ts", checks[0][2])
        self.assertIn("resolved 70.0 s after the recovered event", checks[3][2])

    def test_delay_over_the_limit_reports_the_numbers(self):
        inc = incident(opened=T0 + timedelta(seconds=63.2))
        ok, detail = self.checks(inc)["opened within 50 s of the end of the inject"]
        self.assertFalse(ok)
        self.assertIn("61.2 s after the end of the inject", detail)
        self.assertIn("limit 50 s", detail)

    def test_the_limit_counts_from_the_end_of_the_inject_not_from_the_inject_ts(self):
        at_limit = incident(opened=REFERENCE + timedelta(seconds=50))        # 52 s after the inject ts
        self.assertTrue(self.checks(at_limit)["opened within 50 s of the end of the inject"][0])
        over = incident(opened=REFERENCE + timedelta(seconds=50.1))
        self.assertFalse(self.checks(over)["opened within 50 s of the end of the inject"][0])

    def test_opened_before_the_inject_returned_is_a_failure_with_a_hint(self):
        ok, detail = self.checks(incident(opened=T0 + timedelta(seconds=1)))["opened within 50 s of the end of the inject"]
        self.assertFalse(ok)
        self.assertIn("clock", detail)

    def test_missing_rule_is_named(self):
        inc = incident(symptoms=[symptom("R02", "postgres")])
        ok, detail = next((ok, d) for n, ok, d in dc.incident_checks(inc, F10, REFERENCE, T0, RECOVERED) if n.startswith("the symptoms"))
        self.assertFalse(ok)
        self.assertIn("missing R05", detail)
        self.assertIn("R02 (postgres)", detail)

    def test_the_right_rule_on_the_wrong_service_does_not_count(self):
        inc = incident(symptoms=[symptom("R02", "redis"), symptom("R05", "gateway")])
        self.assertEqual(dc.missing_rules(inc, F10["rules"]), [("R02", "postgres")])

    def test_wrong_primary(self):
        for primary in (None, "redis"):
            ok, detail = self.checks(incident(primary=primary))["primary_service is postgres"]
            self.assertFalse(ok)
            self.assertIn(repr(primary), detail)

    def test_a_null_primary_can_be_the_expectation(self):
        expectation = {"target": "orders", "rules": [("R06", None)], "primary": None, "limit_s": 50}
        checks = dc.incident_checks(incident(primary=None), expectation, REFERENCE, T0, RECOVERED)
        self.assertEqual(checks[2][0:2], ("primary_service is null", True))

    def test_not_resolved_and_resolved_too_late(self):
        open_inc = incident(status="open", resolved=None)
        ok, detail = self.checks(open_inc)["resolved within 180 s after the recovered event"]
        self.assertEqual((ok, "open" in detail), (False, True))
        late = incident(resolved=RECOVERED + timedelta(seconds=181))
        ok, detail = self.checks(late)["resolved within 180 s after the recovered event"]
        self.assertFalse(ok)
        self.assertIn("181.0 s", detail)
        on_time = incident(resolved=RECOVERED + timedelta(seconds=180))
        self.assertTrue(self.checks(on_time)["resolved within 180 s after the recovered event"][0])

    def test_reopened_or_still_active(self):
        self.assertFalse(self.checks(incident(reopen=1))["not reopened and no symptom still active"][0])
        inc = incident(symptoms=[symptom("R02", "postgres", active=True), symptom("R05", "gateway")])
        ok, detail = self.checks(inc)["not reopened and no symptom still active"]
        self.assertFalse(ok)
        self.assertIn("R02 postgres", detail)

    def test_measurements(self):
        inc = incident(symptoms=[symptom("R02", "postgres", first=T0 + timedelta(seconds=12)),
                                 symptom("R05", "gateway", first=T0 + timedelta(seconds=30))])
        m = dc.measurements(inc, REFERENCE, T0, RECOVERED)
        self.assertEqual((m["incident_id"], m["delay_s"], m["delay_from_inject_ts_s"], m["first_symptom_s"], m["resolve_s"],
                          m["symptoms"], m["rules"], m["primary"]),
                         ("INC-0002", 20.0, 22.0, 10.0, 70.0, 2, ["R02", "R05"], "postgres"))
        self.assertIsNone(dc.measurements(incident(status="open", resolved=None), REFERENCE, T0, RECOVERED)["resolve_s"])


class BaselineHelpersTests(unittest.TestCase):
    def vector(self, items):
        return {"status": "success", "data": {"resultType": "vector",
                "result": [{"metric": m, "value": [1760000000, str(v)]} for m, v in items]}}

    def test_parse_vector(self):
        data = self.vector([({"rule_id": "R03", "subject": "orders"}, 34.3)])
        self.assertEqual(dc.parse_vector(data), [({"rule_id": "R03", "subject": "orders"}, 34.3)])
        self.assertEqual(dc.parse_vector(self.vector([])), [])
        with self.assertRaises(ValueError):
            dc.parse_vector({"status": "error", "error": "bad"})
        with self.assertRaises(ValueError):
            dc.parse_vector({"status": "success", "data": {"resultType": "matrix", "result": []}})

    def test_state_problems(self):
        m = lambda rid, sub: {"rule_id": rid, "subject": sub}
        max_state = dc.parse_vector(self.vector([(m("R03", "orders"), 1), (m("R03", "gateway"), 0), (m("R02", "redis"), 0)]))
        min_state = dc.parse_vector(self.vector([(m("R03", "orders"), 0), (m("R09", "orders"), -1), (m("R02", "redis"), 0)]))
        self.assertEqual(dc.state_problems(max_state, min_state), (["R03 orders"], ["R09 orders"]))
        quiet = dc.parse_vector(self.vector([(m("R03", "orders"), 0)]))
        self.assertEqual(dc.state_problems(quiet, quiet), ([], []))

    def test_value_rows(self):
        rules = [{"id": "R03", "name": "cpu_high", "comparator": ">", "threshold": 85.0},
                 {"id": "R06", "name": "dependency_errors", "comparator": ">", "threshold": 0.1},
                 {"id": "R01", "name": "target_down", "comparator": "==", "threshold": 0.0}]
        vector = dc.parse_vector(self.vector([({"rule_id": "R03", "subject": "orders"}, 34.3),
                                              ({"rule_id": "R03", "subject": "gateway"}, 30.4),
                                              ({"rule_id": "R01", "subject": "gateway"}, 1.0)]))
        rows = dc.value_rows(vector, rules)
        self.assertEqual(rows[0], ("R01", "target_down", "gateway", "1", "", "== 0"))
        self.assertEqual(rows[1], ("R03", "cpu_high", "gateway", "30.4", "36 %", "> 85"))
        self.assertEqual(rows[2], ("R03", "cpu_high", "orders", "34.3", "40 %", "> 85"))
        self.assertEqual(rows[3], ("R06", "dependency_errors", "-", "no value", "", "> 0.1"))

    def test_the_shipped_rules_give_a_row_for_every_rule(self):
        rules = [{"id": r["id"], "name": r["name"], "comparator": r["comparator"], "threshold": r["threshold"]}
                 for r in json.loads(RULES_JSON.read_text(encoding="utf-8"))["rules"]]
        rows = dc.value_rows([], rules)
        self.assertEqual([r[0] for r in rows], [f"R0{i}" for i in range(1, 10)])

    def test_format_table(self):
        lines = dc.format_table(["a", "bb"], [("1", "2"), ("333", "4")])
        self.assertEqual(lines, ["a    bb", "---  --", "1    2", "333  4"])


class StatusHelpersTests(unittest.TestCase):
    def status(self, prom="ok", loki="ok", rules=None):
        return {"sources": {"prometheus": {"state": prom}, "loki": {"state": loki}},
                "rules": rules if rules is not None else [
                    {"rule_id": "R01", "source": "prometheus", "state": "ok"},
                    {"rule_id": "R09", "source": "loki", "state": "ok"}]}

    def test_sources_not_ok(self):
        self.assertEqual(dc.sources_not_ok(self.status()), [])
        self.assertEqual(dc.sources_not_ok(self.status(prom="warming_up", loki="no_data")), ["loki (no_data)", "prometheus (warming_up)"])

    def test_rule_states_during_an_outage(self):
        status = self.status(prom="no_data", rules=[{"rule_id": "R01", "source": "prometheus", "state": "no_data"},
                                                    {"rule_id": "R02", "source": "prometheus", "state": "ok"},
                                                    {"rule_id": "R09", "source": "loki", "state": "ok"}])
        self.assertEqual(dc.rule_state_problems(status, "prometheus", "no_data"), ["R02 is ok"])
        self.assertEqual(dc.other_source_problems(status, "prometheus"), [])
        self.assertEqual(dc.other_source_problems(status, "loki"), ["R01 is no_data"])

    def test_warmup_path(self):
        self.assertEqual(dc.warmup_path(["no_data", "warming_up", "warming_up", "ok"]), ([], []))
        self.assertEqual(dc.warmup_path(["warming_up", "ok"]), ([], []))
        problems, _ = dc.warmup_path(["ok"])
        self.assertIn("never showed warming_up", problems[0])
        problems, _ = dc.warmup_path(["no_data", "ok", "warming_up"])
        self.assertIn("warm-up was skipped", " ".join(problems))
        problems, _ = dc.warmup_path(["no_data", "warming_up"])
        self.assertIn("never reached ok", " ".join(problems))
        problems, notes = dc.warmup_path(["warming_up", "no_data", "warming_up", "ok"])
        self.assertEqual(problems, [])
        self.assertIn("fell back to no_data", notes[0])


class TimingHelpersTests(unittest.TestCase):
    NOW = T0 + timedelta(seconds=1000)

    def test_settle_counts_from_the_recovered_event(self):
        recovered = self.NOW - timedelta(seconds=30)
        self.assertEqual(dc.settle_wait_s(self.NOW, recovered, 100, None, 120), 70.0)
        self.assertEqual(dc.settle_wait_s(self.NOW, self.NOW - timedelta(seconds=100), 100, None, 120), 0.0)
        self.assertEqual(dc.settle_wait_s(self.NOW, self.NOW - timedelta(seconds=500), 100, None, 120), 0.0)

    def test_the_reopen_guard_wins_when_it_is_longer(self):
        recovered = self.NOW - timedelta(seconds=70)
        resolved = self.NOW - timedelta(seconds=5)          # the incident resolved 5 s ago: wait 120 + 10 - 5
        self.assertEqual(dc.settle_wait_s(self.NOW, recovered, 100, resolved, 120), 125.0)
        self.assertEqual(dc.settle_wait_s(self.NOW, recovered, 100, resolved, 120, margin_s=0), 115.0)

    def test_no_wait_for_the_first_fault(self):
        self.assertEqual(dc.settle_wait_s(self.NOW, None, 100, None, 120), 0.0)

    def test_hold_is_the_limit_plus_a_margin(self):
        self.assertEqual(dc.hold_for(50, False), 90)
        self.assertEqual(dc.hold_for(50, True), 75)

    def test_wait_until_with_a_fake_clock(self):
        now = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        value, seconds = dc.wait_until(lambda: now[0] >= 12, 60, 5, clock=lambda: now[0], sleep=sleep)
        self.assertTrue(value)
        self.assertEqual((seconds, sleeps), (15.0, [5, 5, 5]))

    def test_wait_until_times_out_and_asks_once_more_at_the_deadline(self):
        now = [0.0]
        calls = []

        def predicate():
            calls.append(now[0])
            return False

        def sleep(seconds):
            now[0] += seconds
        value, seconds = dc.wait_until(predicate, 12, 5, clock=lambda: now[0], sleep=sleep)
        self.assertFalse(value)
        self.assertEqual(calls, [0.0, 5.0, 10.0, 12.0])
        self.assertEqual(seconds, 12.0)

    def test_wait_until_asks_at_least_once(self):
        value, _ = dc.wait_until(lambda: "yes", 0, 5)
        self.assertEqual(value, "yes")

    def test_the_estimate_is_longer_for_more_faults_and_shorter_with_quick(self):
        full = dc.estimate_run_s(["F10", "F13", "F06"], False, False)
        self.assertGreater(full, dc.estimate_run_s(["F10"], False, False))
        self.assertLess(dc.estimate_run_s(["F10", "F13", "F06"], True, False), full)
        self.assertLess(dc.estimate_run_s(["F10", "F13", "F06"], False, True), full)


class ComposeProfileTests(unittest.TestCase):
    BASE = ["docker", "compose", "-f", "docker-compose.yml"]

    def test_a_profile_is_required(self):
        with self.assertRaises(ValueError):
            dc.compose_command(self.BASE, [], ["stop", "prometheus"])

    def test_prometheus_and_loki_carry_the_observability_profile(self):
        for service in ("prometheus", "loki"):
            profiles, args = dc.compose_args_for(service, "stop", service)
            cmd = dc.compose_command(self.BASE, profiles, args)
            self.assertEqual(cmd, self.BASE + ["--profile", "observability", "stop", service])

    def test_the_detector_carries_the_detector_profile(self):
        profiles, args = dc.compose_args_for("detector", "ps", "-q", "detector")
        self.assertEqual(dc.compose_command(self.BASE, profiles, args), self.BASE + ["--profile", "detector", "ps", "-q", "detector"])

    def test_an_unknown_service_has_no_profile(self):
        with self.assertRaises(KeyError):
            dc.service_profile("gateway")

    def test_the_stack_listing_uses_both_profiles(self):
        cmd = dc.compose_command(self.BASE, ["observability", "detector"], ["ps", "--all", "--format", "json"])
        self.assertEqual(cmd[len(self.BASE):len(self.BASE) + 4], ["--profile", "observability", "--profile", "detector"])

    def test_the_script_only_uses_compose_verbs_that_are_not_faults(self):
        """Faults go through chaos.py only. Docker compose calls in the script are ps, stop, up (and the load run)."""
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        verbs = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("compose", "compose_service"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        verbs.add(arg.value)
        self.assertTrue({"ps", "stop", "up"} <= verbs, verbs)
        self.assertFalse(verbs & {"kill", "pause", "unpause", "restart", "rm", "exec", "down", "network", "run"}, verbs)

    def test_every_compose_service_call_names_a_service_with_a_known_profile(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        services = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "compose_service":
                if node.args and isinstance(node.args[0], ast.Constant):
                    services.add(node.args[0].value)
        self.assertTrue(services)
        self.assertTrue(services <= set(dc.SERVICE_PROFILE), services)


class ExpectationTests(unittest.TestCase):
    def test_the_table_of_the_prompt(self):
        self.assertEqual(dc.EXPECTATIONS["F10"], {"target": "postgres", "rules": [("R02", "postgres"), ("R05", None)],
                                                  "primary": "postgres", "limit_s": 50})
        self.assertEqual(dc.EXPECTATIONS["F13"], {"target": "redis", "rules": [("R02", "redis"), ("R06", None)],
                                                  "primary": "redis", "limit_s": 50})
        self.assertEqual(dc.EXPECTATIONS["F06"], {"target": "inventory", "rules": [("R01", "inventory")],
                                                  "primary": "inventory", "limit_s": 50})
        self.assertEqual(dc.FAULT_ORDER, ["F10", "F13", "F06"])

    def test_the_expected_rules_exist_and_can_name_that_service(self):
        rules = {r["id"]: r for r in json.loads(RULES_JSON.read_text(encoding="utf-8"))["rules"]}
        for fid, exp in dc.EXPECTATIONS.items():
            for rule, service in exp["rules"]:
                self.assertIn(rule, rules, fid)
                if service is not None:
                    self.assertIn(service, rules[rule]["expected_subjects"], (fid, rule, service))

    @unittest.skipUnless(CATALOGUE_JSON.exists(), "chaos/catalogue.json not found")
    def test_targets_and_root_causes_match_the_m3_catalogue(self):
        catalogue = {f["id"]: f for f in json.loads(CATALOGUE_JSON.read_text(encoding="utf-8"))}
        for fid, exp in dc.EXPECTATIONS.items():
            self.assertEqual(catalogue[fid]["default_target"], exp["target"], fid)
            cause = catalogue[fid]["expected_root_cause"]["service"]
            self.assertEqual(exp["primary"], exp["target"] if cause == "$target" else cause, fid)

    def test_final_table(self):
        rows = [{"fault": "F10", "limit_s": 50, "ok": True, "incident_id": "INC-0002", "delay_s": 12.34, "resolve_s": 70.0,
                 "rules": ["R02", "R05"], "primary": "postgres"},
                {"fault": "F13", "limit_s": 50, "ok": False}]
        lines = dc.final_table(rows)
        self.assertIn("12.3", lines[2])
        self.assertIn("PASS", lines[2])
        self.assertIn("FAIL", lines[3])
        self.assertIn("null", lines[3])


class StreamTests(unittest.TestCase):
    """stream(): lines are stamped when they ARRIVE, while the child still runs; poll() is called meanwhile."""

    def test_arrival_time_of_the_injected_line_and_polling(self):
        code = ("import time\n"
                "print('run F10: warm-up 0 s', flush=True)\n"
                "time.sleep(0.6)\n"
                "print('injected F10 on postgres (run id F10-0123abcd)', flush=True)\n"
                "time.sleep(0.6)\n"
                "print('recovered 7.7 s after the clear', flush=True)\n")
        stamps = []
        polls = []
        started = dc.utcnow()
        code_, lines = dc.stream([sys.executable, "-c", code], lambda stamp, text: stamps.append((stamp, text)),
                                 lambda: polls.append(1), 0.1, 30)
        finished = dc.utcnow()
        self.assertEqual(code_, 0)
        self.assertEqual(len(lines), 3)
        injected = next(stamp for stamp, text in stamps if text.startswith("injected"))
        self.assertGreaterEqual((injected - started).total_seconds(), 0.5)
        self.assertLess((finished - injected).total_seconds(), 1.9)          # arrived while the child was still sleeping
        self.assertGreaterEqual(len(polls), 3)

    def test_a_child_that_runs_too_long_is_killed(self):
        code, lines = dc.stream([sys.executable, "-c", "import time; time.sleep(30)"], lambda s, t: None, lambda: None, 0.1, 0.5)
        self.assertEqual(code, 124)
        self.assertIn("timed out", lines[-1])

    def test_the_exit_code_is_returned(self):
        code, lines = dc.stream([sys.executable, "-c", "import sys; print('x'); sys.exit(3)"], lambda s, t: None, lambda: None, 0.1, 30)
        self.assertEqual((code, lines), (3, ["x"]))

    def test_the_child_is_registered_for_ctrl_c(self):
        started = []
        dc.stream([sys.executable, "-c", "print('x')"], lambda s, t: None, lambda: None, 0.1, 30, started=started)
        self.assertEqual(len(started), 1)


class SummaryTests(unittest.TestCase):
    def tearDown(self):
        dc.results.clear()
        dc.warnings.clear()

    def test_summary_fails_on_one_failed_check(self):
        import contextlib
        import io
        dc.results[:] = [("a", True, ""), ("b", False, "why")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dc.summary()
        self.assertEqual(code, 1)
        self.assertIn("1/2 checks passed", out.getvalue())
        self.assertIn("FAILED: b", out.getvalue())
        self.assertIn("python chaos/chaos.py clear --all", out.getvalue())

    def test_summary_passes_and_an_incomplete_run_never_passes(self):
        import contextlib
        import io
        dc.results[:] = [("a", True, "")]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dc.summary(), 0)
        self.assertIn("DETECTOR CHECK PASSED", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(dc.summary(complete=False), 1)
        self.assertIn("INCOMPLETE", out.getvalue())

    def test_unknown_fault_exits_with_2(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(dc.main(["--only", "F99"]), 2)
        self.assertIn("F99", err.getvalue())
        self.assertIn("F10, F13, F06", err.getvalue())


if __name__ == "__main__":
    unittest.main()
