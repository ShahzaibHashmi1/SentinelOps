"""Unit tests for the pure logic of scripts/chaos_check.py (module M3, CP5). No docker, no network.

Run from the repo root (host Python, standard library only):
    python -m unittest discover -s chaos/tests
"""

import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

CHAOS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHAOS_DIR))
sys.path.insert(0, str(CHAOS_DIR.parent / "scripts"))

import chaos_check as cc   # noqa: E402
import faults              # noqa: E402

CATALOGUE = {f["id"]: f for f in json.loads((CHAOS_DIR / "catalogue.json").read_text(encoding="utf-8"))}


def good_events(fid="F10", target="postgres", recovery=7.4, late=False):
    fault = CATALOGUE[fid]
    cause = cc.resolve_root_cause(fault, target)
    stamps = {"inject": "2026-10-16T10:00:00.000Z", "clear": "2026-10-16T10:02:00.000Z",
              "recovered": "2026-10-16T10:02:07.400Z"}
    events = []
    for name in ("inject", "clear", "recovered"):
        event = {"run_id": f"{fid}-0a1b2c3d", "event": name, "fault_id": fid, "category": fault["category"],
                 "target": target, "params": {}, "ts": stamps[name], "expected_root_cause": cause,
                 "expected_action": fault["expected_action"], "acceptable_actions": fault["acceptable_actions"]}
        if name == "recovered":
            event["recovery_s"] = recovery
        if late and name != "inject":
            event["late"] = True
        events.append(event)
    return events


class ConditionTests(unittest.TestCase):
    def test_every_catalogue_condition_parses(self):
        for fault in CATALOGUE.values():
            for symptom in fault["expected_symptoms"]:
                cc.parse_condition(symptom["condition"])

    def test_operators(self):
        self.assertTrue(cc.condition_met(86.0, "> 85"))
        self.assertFalse(cc.condition_met(85.0, "> 85"))
        self.assertTrue(cc.condition_met(85.0, ">= 85"))
        self.assertTrue(cc.condition_met(0.0, "== 0"))
        self.assertFalse(cc.condition_met(1.0, "== 0"))
        self.assertTrue(cc.condition_met(0.3, "< 0.5"))
        self.assertTrue(cc.condition_met(0.5, "<= 0.5"))

    def test_values_above_100_are_fine(self):
        self.assertTrue(cc.condition_met(111.0, "> 85"))          # the CPU-of-limit query reached 111 in CP4
        self.assertFalse(cc.condition_met(111.0, "< 100"))

    def test_no_value_and_nan_never_meet_a_condition(self):
        for condition in ("> 0", "== 0", "< 5", ">= 0"):
            self.assertFalse(cc.condition_met(None, condition))
            self.assertFalse(cc.condition_met(float("nan"), condition))

    def test_bad_condition(self):
        with self.assertRaises(ValueError):
            cc.parse_condition("about 5")


class QueryHelperTests(unittest.TestCase):
    def test_target_is_filled_in(self):
        self.assertEqual(cc.instantiate('min(up{job="$target"})', "inventory"), 'min(up{job="inventory"})')

    def test_narrow_window_shortens_the_last_range_only(self):
        query = 'sum(count_over_time({service="orders"} |~ "(?i)required" [2m]))'
        self.assertEqual(cc.narrow_window(query, 37.2), 'sum(count_over_time({service="orders"} |~ "(?i)required" [38s]))')
        self.assertEqual(cc.narrow_window(query, 1), query.replace("[2m]", "[5s]"))        # never below 5 s
        self.assertEqual(cc.narrow_window(query, 120), query)                               # never lengthened
        self.assertEqual(cc.narrow_window(query, 900), query)

    def test_narrow_window_without_a_range(self):
        self.assertEqual(cc.narrow_window("min(up)", 10), "min(up)")

    def test_narrow_window_ignores_brackets_that_are_not_ranges(self):
        query = '{service="x"} |~ "[0-9]+" [2m]'
        self.assertIn("[30s]", cc.narrow_window(query, 30))
        self.assertIn("[0-9]+", cc.narrow_window(query, 30))

    def test_vector_value(self):
        vector = {"data": {"resultType": "vector", "result": [{"metric": {}, "value": [1.7e9, "56.7"]}]}}
        self.assertEqual(cc.vector_value(vector), 56.7)
        self.assertEqual(cc.vector_value({"data": {"resultType": "vector", "result": []}}), 0.0)    # empty counts as 0
        self.assertEqual(cc.vector_value({"data": {"resultType": "scalar", "result": [1.7e9, "3"]}}), 3.0)
        nan = cc.vector_value({"data": {"resultType": "vector", "result": [{"value": [1, "NaN"]}]}})
        self.assertTrue(nan != nan)

    def test_fetch_value_with_a_fake_http(self):
        seen = []

        def http(url, timeout):
            seen.append(url)
            return 200, json.dumps({"status": "success", "data": {"resultType": "vector",
                                                                   "result": [{"value": [1, "2"]}]}})
        self.assertEqual(cc.fetch_value("prometheus", "up", http), (2.0, ""))
        self.assertEqual(cc.fetch_value("loki", "up", http), (2.0, ""))
        self.assertIn("127.0.0.1:9090/api/v1/query?query=up", seen[0])
        self.assertIn("127.0.0.1:3100/loki/api/v1/query?query=up", seen[1])
        self.assertEqual(cc.fetch_value("loki", "up", lambda u, t: (503, "")), (None, "loki answered HTTP 503"))
        value, error = cc.fetch_value("prometheus", "up", lambda u, t: (200, "not json"))
        self.assertIsNone(value)
        self.assertIn("unexpected", error)
        value, error = cc.fetch_value("prometheus", "bad(", lambda u, t: (200, '{"status":"error","error":"parse"}'))
        self.assertIsNone(value)
        self.assertIn("query error", error)


class ReadyRetryTests(unittest.TestCase):
    def test_one_transient_503_does_not_fail(self):
        answers = iter([503, 200])
        sleeps = []
        ok, detail = cc.wait_ready("http://x/ready", 5, 3, http=lambda u, t: (next(answers), ""), sleep=sleeps.append)
        self.assertTrue(ok)
        self.assertEqual(detail, "HTTP 200, attempt 2 of 5")
        self.assertEqual(sleeps, [3])

    def test_first_try_has_no_attempt_note(self):
        self.assertEqual(cc.wait_ready("http://x", 5, 3, http=lambda u, t: (200, ""), sleep=lambda s: None),
                         (True, "HTTP 200"))

    def test_gives_up_after_the_attempts(self):
        calls = []
        ok, detail = cc.wait_ready("http://x", 4, 1, http=lambda u, t: (calls.append(1) or 503, ""), sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(detail, "HTTP 503 after 4 attempts")
        self.assertEqual(len(calls), 4)


class LokiReadyWaitTests(unittest.TestCase):
    """Preflight: Loki /ready answered 503 for more than 12 s on a running stack (CP5), so it gets up to 90 s."""

    def test_the_constants(self):
        self.assertEqual((cc.LOKI_READY_ATTEMPTS, cc.LOKI_READY_DELAY_S), (18, 5))
        self.assertEqual((cc.PROMETHEUS_READY_ATTEMPTS, cc.PROMETHEUS_READY_DELAY_S), (3, 3))   # Prometheus is unchanged

    def test_loki_that_is_ready_at_the_last_attempt_passes(self):
        answers, sleeps = [503] * 17 + [200], []
        ok, detail = cc.wait_ready("http://x/ready", cc.LOKI_READY_ATTEMPTS, cc.LOKI_READY_DELAY_S,
                                   http=lambda u, t: (answers.pop(0), ""), sleep=sleeps.append)
        self.assertTrue(ok)
        self.assertEqual(detail, "HTTP 200, attempt 18 of 18")
        self.assertEqual(sleeps, [5] * 17)                              # 85 s of waiting between the 18 attempts

    def test_loki_that_never_gets_ready_fails_after_18_attempts(self):
        calls, sleeps = [], []
        ok, detail = cc.wait_ready("http://x/ready", cc.LOKI_READY_ATTEMPTS, cc.LOKI_READY_DELAY_S,
                                   http=lambda u, t: (calls.append(1) or 503, ""), sleep=sleeps.append)
        self.assertFalse(ok)
        self.assertEqual(detail, "HTTP 503 after 18 attempts")
        self.assertEqual((len(calls), len(sleeps)), (18, 17))


class LokiPreflightTests(unittest.TestCase):
    """A Loki that is not ready after the wait stops the check cleanly: nothing is injected, no load is started."""

    def setUp(self):
        cc.results.clear()
        cc.warnings.clear()
        cc.printed_output.clear()
        self.waits = []

    def fake_wait_ready(self, url, attempts, delay_s, http=None, sleep=None):
        self.waits.append((url, attempts, delay_s))
        return (True, "HTTP 200") if ":9090/" in url else (False, f"HTTP 503 after {attempts} attempts")

    def patches(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cc, "wait_ready", self.fake_wait_ready))
        for name in ("chaos", "compose_ps", "start_load", "remove_load", "run", "emergency_cleanup"):
            stack.enter_context(mock.patch.object(cc, name, side_effect=AssertionError(f"{name} must not be called")))
        stack.enter_context(mock.patch.object(cc, "git_status", return_value=set()))
        return stack

    def test_preflight_aborts_at_loki(self):
        check = cc.Check(CATALOGUE, ["F10"], quick=True)
        out = io.StringIO()
        with self.patches(), contextlib.redirect_stdout(out):
            with self.assertRaises(cc.Abort):
                check.preflight()
        self.assertEqual(self.waits, [(f"{cc.PROMETHEUS}/-/ready", 3, 3), (f"{cc.LOKI}/ready", 18, 5)])
        self.assertEqual(cc.results[-1], ("Loki is ready", False, "HTTP 503 after 18 attempts"))
        self.assertFalse(check.load_started)
        self.assertIn("Loki /ready did not answer 200 within about 90 s. Nothing was injected.", out.getvalue())

    def test_main_returns_1_without_cleanup_or_chaos_calls(self):
        out = io.StringIO()
        with self.patches(), contextlib.redirect_stdout(out):
            code = cc.main(["--only", "F10", "--quick"])
        self.assertEqual(code, 1)
        self.assertEqual(cc.printed_output, [])                         # chaos.py never ran
        text = out.getvalue()
        self.assertIn("stopped: Loki is not ready", text)
        self.assertIn("FAILED: Loki is ready", text)
        self.assertNotIn("CHAOS CHECK PASSED", text)


class DurationTests(unittest.TestCase):
    def test_faults_that_stay_until_clear_get_no_duration(self):
        for fid in ("F06", "F08", "F10", "F13"):
            self.assertIsNone(cc.duration_for(CATALOGUE[fid], 60))

    def test_self_ending_faults_outlast_their_longest_symptom(self):
        for fid in ("F01", "F02", "F11", "F18"):
            fault = CATALOGUE[fid]
            for margin in (cc.DURATION_MARGIN_S, cc.QUICK_DURATION_MARGIN_S):
                duration = cc.duration_for(fault, margin)
                self.assertEqual(duration, cc.max_within_s(fault) + margin)
                self.assertLessEqual(duration, fault["max_duration_s"])
        self.assertEqual([cc.duration_for(CATALOGUE[f], 60) for f in ("F01", "F02", "F11", "F18")], [150, 180, 150, 150])

    def test_f18_requests_per_second_has_90_seconds_and_nothing_else_moves(self):
        first = CATALOGUE["F18"]["expected_symptoms"][0]
        self.assertEqual((first["condition"], first["within_s"]), ("> 30", 90))
        self.assertIn("http_requests_total", first["query"])
        self.assertEqual(cc.max_within_s(CATALOGUE["F18"]), 90)
        self.assertEqual(cc.duration_for(CATALOGUE["F18"], 60), 150)
        self.assertEqual(cc.duration_for(CATALOGUE["F18"], 30), 120)
        old = copy.deepcopy(list(CATALOGUE.values()))
        for fault in old:
            if fault["id"] == "F18":
                fault["expected_symptoms"][0]["within_s"] = 60          # the value before CP5 follow-up 2
        for settle, margin in ((100, 60), (60, 30)):
            self.assertEqual(cc.estimate_run_s(old, settle, margin),
                             cc.estimate_run_s(list(CATALOGUE.values()), settle, margin))
        self.assertEqual(cc.Check(CATALOGUE, list(CATALOGUE), False).load_duration_s,
                         int(1.2 * cc.estimate_run_s(old, 100, 60)))

    def test_duration_is_capped(self):
        fault = copy.deepcopy(CATALOGUE["F01"])
        fault["max_duration_s"] = 100
        self.assertEqual(cc.duration_for(fault, 60), 100)

    def test_estimate_grows_with_the_number_of_faults(self):
        one = cc.estimate_run_s([CATALOGUE["F10"]], 100, 60)
        two = cc.estimate_run_s([CATALOGUE["F10"], CATALOGUE["F13"]], 100, 60)
        self.assertGreater(two, one)


class WaitSymptomsTests(unittest.TestCase):
    """The polling loop with a fake clock: sleeping advances the clock."""

    def setUp(self):
        self.now = 1000.0
        self.reports = []
        self.queries = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def report(self, index, ok, value, seconds, detail):
        self.reports.append((index, ok, value, seconds))

    def run_with(self, symptoms, values, t_before=900.0, t0=1000.0, target="orders"):
        def fetch(source, query):
            self.queries.append((source, query, self.now))
            return values(source, query, self.now - t0)
        return cc.wait_symptoms(symptoms, target, t_before, t0, fetch, self.clock, self.sleep, self.report)

    def test_symptom_that_appears_in_time_is_ok_and_values_above_100_count(self):
        symptoms = CATALOGUE["F01"]["expected_symptoms"]
        self.run_with(symptoms, lambda s, q, elapsed: (110.1 if elapsed >= 20 else 30.0, ""))
        self.assertEqual([(i, ok, v, sec) for i, ok, v, sec in self.reports], [(0, True, 110.1, 20.0)])

    def test_symptom_that_never_appears_fails_at_its_limit(self):
        symptoms = CATALOGUE["F01"]["expected_symptoms"]
        self.run_with(symptoms, lambda s, q, elapsed: (30.0, ""))
        (index, ok, value, seconds), = self.reports
        self.assertFalse(ok)
        self.assertEqual((value, seconds), (30.0, 90.0))               # one last reading at the limit

    def test_every_symptom_is_decided_and_independent(self):
        symptoms = CATALOGUE["F08"]["expected_symptoms"]               # prometheus, prometheus, loki
        def values(source, query, elapsed):
            if source == "loki":
                return (None, "loki answered HTTP 503")
            return (50.0, "") if "dependency_requests_total" in query else (0.0, "")
        self.run_with(symptoms, values)
        decided = {index: ok for index, ok, _v, _s in self.reports}
        self.assertEqual(decided, {0: True, 1: False, 2: False})

    def test_loki_window_is_narrowed_to_the_time_since_the_inject_started(self):
        symptoms = CATALOGUE["F06"]["expected_symptoms"]               # prometheus, loki ([2m])
        def values(source, query, elapsed):
            return (0.0, "") if source == "prometheus" else (5.0 if elapsed >= 10 else 0.0, "")
        self.run_with(symptoms, values, t_before=980.0, t0=1000.0, target="inventory")
        loki = [(q, at) for s, q, at in self.queries if s == "loki"]
        self.assertIn("[20s]", loki[0][0])                             # 1000 - 980: the window starts at the inject
        self.assertIn("[25s]", loki[1][0])                             # five seconds later
        self.assertIn('{service="inventory"}', loki[0][0])             # $target is filled in

    def test_a_decided_symptom_is_not_queried_again(self):
        symptoms = CATALOGUE["F10"]["expected_symptoms"]
        self.run_with(symptoms, lambda s, q, e: (0.0, "") if "memory_working_set" in q else (0.0, ""))
        first = [q for _s, q, _t in self.queries if "memory_working_set" in q]
        self.assertEqual(len(first), 1)                                # `== 0` was true at the first reading


class GroundTruthTests(unittest.TestCase):
    def check(self, events, fid="F10", target="postgres"):
        return cc.ground_truth_problems(events, CATALOGUE[fid], target)

    def test_a_normal_run_has_no_problem(self):
        for fid, target in (("F10", "postgres"), ("F01", "orders"), ("F06", "inventory"), ("F18", "gateway")):
            with self.subTest(fault=fid):
                self.assertEqual(self.check(good_events(fid, target), fid, target), {"order": [], "recovery": [], "labels": []})

    def test_equal_timestamps_are_fine(self):
        events = good_events(recovery=0.0)
        for event in events:
            event["ts"] = "2026-10-16T10:00:00.000Z"
        self.assertEqual(self.check(events)["order"], [])
        self.assertEqual(self.check(events)["recovery"], [])

    def test_order_is_the_position_in_the_file(self):
        events = good_events()
        events[0], events[1] = events[1], events[0]
        self.assertTrue(any("expected ['inject', 'clear', 'recovered']" in p for p in self.check(events)["order"]))

    def test_timestamps_going_backwards_are_found(self):
        events = good_events()
        events[1]["ts"] = "2026-10-15T10:00:00.000Z"
        self.assertIn("ts is not inject <= clear <= recovered", self.check(events)["order"])

    def test_late_flag_is_a_problem_in_a_normal_run(self):
        problems = self.check(good_events(late=True))["recovery"]
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("late flag" in p for p in problems))

    def test_recovery_s_must_match_the_timestamps(self):
        self.assertTrue(any("does not match" in p for p in self.check(good_events(recovery=9.0))["recovery"]))
        self.assertEqual(self.check(good_events(recovery=7.4))["recovery"], [])
        events = good_events()
        events[2]["recovery_s"] = "fast"
        self.assertTrue(any("expected a number" in p for p in self.check(events)["recovery"]))
        events[2]["recovery_s"] = -1
        self.assertTrue(any("expected a number" in p for p in self.check(events)["recovery"]))

    def test_missing_events_fields_and_mixed_run_ids(self):
        self.assertTrue(self.check(good_events()[:2])["order"])
        events = good_events()
        del events[1]["category"]
        self.assertTrue(any("missing fields category" in p for p in self.check(events)["order"]))
        events = good_events()
        events[2]["run_id"] = "F10-ffffffff"
        self.assertIn("the events do not share one run_id", self.check(events)["order"])

    def test_wrong_fault_target_or_time_format(self):
        self.assertTrue(self.check(good_events("F13", "redis"))["order"])                   # events of another fault
        events = good_events()
        events[0]["ts"] = "2026-10-16 10:00:00"
        self.assertTrue(any("not ISO 8601" in p for p in self.check(events)["order"]))

    def test_labels_must_equal_the_catalogue(self):
        events = good_events()
        events[0]["expected_action"] = "scale_service"
        events[1]["expected_root_cause"] = {"service": "orders", "type": "database_unavailable"}
        events[2]["acceptable_actions"] = ["rotate_logs"]
        self.assertEqual(len(self.check(events)["labels"]), 3)

    def test_f18_has_no_service_in_its_root_cause(self):
        events = good_events("F18", "gateway")
        self.assertIsNone(events[0]["expected_root_cause"]["service"])
        self.assertEqual(self.check(events, "F18", "gateway")["labels"], [])

    def test_event_lines_parse(self):
        raw = b'{"a":1}\n\nnot json\n{"b":2}\n'
        events, bad = cc.parse_event_lines(raw)
        self.assertEqual(events, [{"a": 1}, {"b": 2}])
        self.assertEqual(bad, [3])
        self.assertEqual(cc.parse_event_lines(b""), ([], []))
        self.assertEqual(cc.parse_event_lines(b"\xff\xfe\n")[1], [1])


class PasswordLeakTests(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(cc.find_password_leaks("injected F08 on orders (run id F08-0a1b2c3d)", ["s3cret-value"]), [])

    def test_known_secret(self):
        self.assertEqual(cc.find_password_leaks("... s3cret-value ...", ["s3cret-value"]), ["a secret value from .env"])

    def test_short_secrets_are_plain_words_and_ignored(self):
        self.assertEqual(cc.find_password_leaks("user shop on database shop", ["shop"]), [])

    def test_url_password_and_key_value_pairs(self):
        self.assertEqual(cc.find_password_leaks("postgresql://shop:hunter22@postgres:5432/shop"),
                         ["a password inside a URL"])
        self.assertEqual(cc.find_password_leaks("postgresql://shop:***@postgres:5432/shop"), [])
        self.assertEqual(cc.find_password_leaks("POSTGRES_PASSWORD=hunter22"), ["an unmasked NAME_PASSWORD=value pair"])
        self.assertEqual(cc.find_password_leaks("POSTGRES_PASSWORD=***"), [])
        self.assertEqual(cc.find_password_leaks("differs from the base one only in the password"), [])

    def test_a_leak_is_never_repeated_in_the_result(self):
        self.assertNotIn("hunter22", repr(cc.find_password_leaks("POSTGRES_PASSWORD=hunter22")))

    def test_env_parsing(self):
        text = "# c\nPOSTGRES_USER=shop\nPOSTGRES_PASSWORD='abc123xyz'\nGRAFANA_ADMIN_PASSWORD=x\nUSERS=20\nAPI_TOKEN=\n"
        self.assertEqual(cc.parse_env_secrets(text), ["abc123xyz", "x"])


class StackTests(unittest.TestCase):
    ROWS = [{"Service": name, "State": "running", "Health": "healthy"} for name in cc.M1_SERVICES]

    def test_all_healthy(self):
        self.assertEqual(cc.stack_problems(self.ROWS, cc.M1_SERVICES), [])

    def test_problems_are_named(self):
        rows = [dict(r) for r in self.ROWS]
        rows[0]["State"] = "exited"
        rows[2]["Health"] = "starting"
        del rows[5]
        self.assertEqual(cc.stack_problems(rows, cc.M1_SERVICES),
                         ["postgres (exited)", "inventory (health: starting)", "gateway (no container)"])

    def test_running_problems_ignore_health(self):
        rows = [{"Service": n, "State": "running", "Health": ""} for n in cc.M1_SERVICES + cc.OBSERVABILITY_SERVICES]
        self.assertEqual(cc.running_problems(rows, cc.M1_SERVICES + cc.OBSERVABILITY_SERVICES), [])
        self.assertEqual(cc.running_problems(rows[:-1], cc.M1_SERVICES + cc.OBSERVABILITY_SERVICES), ["grafana (no container)"])

    def test_parse_ps_array_and_lines(self):
        self.assertEqual(cc.parse_ps(json.dumps(self.ROWS)), self.ROWS)
        self.assertEqual(cc.parse_ps("\n".join(json.dumps(r) for r in self.ROWS)), self.ROWS)
        self.assertEqual(cc.parse_ps(""), [])

    def test_restart_snapshot_comparison(self):
        before = cc.parse_inspect_lines("/sentinelops-gateway-1 0 false running\n/sentinelops-orders-1 0 false running\n")
        self.assertEqual(before, {"sentinelops-gateway-1": (0, False, "running"), "sentinelops-orders-1": (0, False, "running")})
        self.assertEqual(cc.restart_problems(before, dict(before)), [])
        after = {"sentinelops-gateway-1": (1, False, "running"), "sentinelops-orders-1": (0, True, "exited")}
        self.assertEqual(cc.restart_problems(before, after), ["sentinelops-gateway-1 restarted (0 -> 1)",
                                                              "sentinelops-orders-1 was OOM-killed",
                                                              "sentinelops-orders-1 is exited"])


class BackgroundLoadTests(unittest.TestCase):
    """Change 3 of CP5: the background load has its own container name."""

    def test_the_name_differs_from_the_f18_container(self):
        self.assertNotEqual(cc.LOAD_CONTAINER, faults.LOAD_CONTAINER)
        self.assertEqual(cc.LOAD_CONTAINER, "sentinelops-check-load")
        self.assertNotIn(faults.LOAD_CONTAINER, " ".join(cc.load_command() + cc.load_filters()))

    def test_clear_all_cannot_touch_the_background_load(self):
        # faults.sweep_load_container removes what faults.load_filters() finds: the exact name ^/sentinelops-chaos-load$
        self.assertIn(f"name=^/{faults.LOAD_CONTAINER}$", faults.load_filters())
        self.assertNotIn(f"name=^/{cc.LOAD_CONTAINER}$", faults.load_filters())

    def test_the_command_runs_the_load_service_detached_with_the_values_in_the_environment(self):
        command = cc.load_command()
        self.assertEqual(command[:2], ["docker", "compose"])
        self.assertEqual(command[command.index("--name") + 1], "sentinelops-check-load")
        for part in ("--profile", "load", "run", "-d", "--no-deps", "loadtest"):
            self.assertIn(part, command)
        self.assertNotIn("-e", command)                                # compose reads ${USERS} from its own environment
        self.assertEqual(cc.load_environment(5400), {"USERS": "10", "SPAWN_RATE": "2", "DURATION": "5400s"})

    def test_filters_need_name_project_service_and_oneoff(self):
        joined = " ".join(cc.load_filters())
        for part in ("name=^/sentinelops-check-load$", "com.docker.compose.project=sentinelops",
                     "com.docker.compose.service=loadtest", "com.docker.compose.oneoff=True"):
            self.assertIn(part, joined)

    def test_the_load_outlasts_the_whole_check(self):
        check = cc.Check(CATALOGUE, list(CATALOGUE), quick=False)
        self.assertGreater(check.load_duration_s, cc.estimate_run_s(list(CATALOGUE.values()), 100, 60))


class PlanTests(unittest.TestCase):
    def test_quick_mode_shortens_the_waits(self):
        normal = cc.Check(CATALOGUE, list(CATALOGUE), quick=False)
        quick = cc.Check(CATALOGUE, list(CATALOGUE), quick=True)
        self.assertEqual((normal.settle_s, quick.settle_s), (100, 60))
        self.assertLess(quick.margin_s, normal.margin_s)

    def test_a_loki_symptom_has_no_baseline_and_a_prometheus_symptom_has(self):
        sources = {f["id"]: [s.get("source", "prometheus") for s in f["expected_symptoms"]] for f in CATALOGUE.values()}
        self.assertEqual(sources["F10"], ["prometheus", "prometheus", "loki"])
        self.assertEqual(sources["F06"], ["prometheus", "loki"])


if __name__ == "__main__":
    unittest.main()
