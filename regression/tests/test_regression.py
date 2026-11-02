"""Unit tests of scripts/regression.py and of docs/REGRESSION.md (step R1).

Standard library only. No Docker, no PowerShell, no sleeping (a fake clock), no network except a local HTTP server on 127.0.0.1
(and it is closed again before the test ends). Temporary folders are registered with addCleanup FIRST, so that they are removed LAST,
after every file, process and socket of the test is closed (Windows refuses to delete an open file).

Groups:
  parsing and judging     output of unittest, of the check scripts, of pytest, of the rules module, the imports scan, the count rules
  the suite table         static checks of SUITES (order, gates, commands, the compose profile rule, no fault is ever started)
  prerequisites           one fake host (FakeHost) answers the commands; a fake clock; every prerequisite and its failure
  runs                    cleanup in `finally`, exit codes, --list, --dry-run, --only, --no-build, the report file
  simulated world         the whole runner against fake commands for gates A, B and C
  real host               the real Host class with python itself as the child process
  docs                    docs/REGRESSION.md equals SUITES
"""
import contextlib
import dataclasses
import http.server
import json
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (puts the runner on the import path)
from _bootstrap import LEDGER_PATH, runner

R = runner
PYTEST_SAMPLE = [".........." + " " * 58 + "[100%]", "10 passed in 0.67s"]   # the printed output of one M1 service test run (orders)


# ---------------------------------------------------------------------------
# Canned outputs and the fake host
# ---------------------------------------------------------------------------

def unittest_output(count, tail="OK"):
    return ["." * 20, "-" * 70, f"Ran {count} tests in 1.234s", "", tail]


def checks_output(suite, passed=None, total=None, verdict=True, extra=()):
    count = suite.expected if suite.expected is not None else 7
    passed = count if passed is None else passed
    total = count if total is None else total
    lines = [f"[OK] step {i}" for i in range(3)] + list(extra) + [f"{passed}/{total} checks passed"]
    if verdict:
        lines.append(suite.verdict)
    return lines


def rules_output(valid=9, pending="pending provenance: none"):
    return [f"rules file detector/rules/rules.json: {valid} rules valid", "", "Id   Name", pending]


def suite_output(suite):
    """What the suite prints on a green run."""
    count = suite.expected if suite.expected is not None else 7
    if suite.kind == "unittest":
        return unittest_output(count)
    if suite.kind == "checks":
        return checks_output(suite)
    if suite.kind == "exit":
        return [".", f"{count if suite.expected is not None else 3} passed in 0.5s"]
    if suite.kind == "python":
        return rules_output()
    raise AssertionError(suite.kind)


def ok_status(incident=None):
    return {"sources": {"prometheus": {"state": "ok"}, "loki": {"state": "ok"}}, "open_incident_id": incident}


class FakeHost:
    """Answers every command of the runner without Docker. The clock is fake: sleep() only moves it."""

    def __init__(self, suites=None):
        self.said = []
        self.calls = []                      # (kind, args tuple) in order; kind is run or stream
        self.inputs = []
        self.envs = []
        self.overrides = []                  # (matcher, code, lines, exception)
        self.clock = 5000.0
        self.slept = []
        self.status_script = []              # consumed first: dicts, None (no answer) or callables
        self.default_status = ok_status()
        self.writes = {}
        self.protected = 0
        self.container_lines = R.EXPECTED_CONTAINERS
        self.commit = "abc1234"
        self.git_status = ""
        self.sources = [("detector/app/api.py", "import urllib.request\nimport threading\n")]
        self.now = datetime(2026, 11, 1, 12, 30, 5, tzinfo=timezone.utc)
        self.by_command = {suite.command: suite for suite in (suites or R.SUITES) if suite.command}

    # -- configuration
    def override(self, matcher, code=0, lines=(), raises=None):
        if isinstance(matcher, str):
            wanted = matcher
            matcher = lambda args, wanted=wanted: wanted in " ".join(args)  # noqa: E731
        self.overrides.insert(0, (matcher, code, list(lines), raises))

    def override_suite(self, suite_id, code=0, lines=(), raises=None):
        command = R.SUITE_BY_ID[suite_id].command
        self.override(lambda args, command=command: tuple(args) == tuple(command), code, lines, raises)

    # -- the interface of the runner
    def say(self, text=""):
        self.said.append(str(text))

    def respond(self, args, input_text=None):
        args = tuple(args)
        for matcher, code, lines, raises in self.overrides:
            if matcher(args):
                if raises is not None:
                    raise raises
                return code, list(lines)
        if args in self.by_command:
            return 0, suite_output(self.by_command[args])
        if args == R.CMD_DOCKER_INFO:
            return 0, ["Server Version: 29.8.1"]
        if args == R.CMD_COMPOSE_UP:
            return 0, ["Container sentinelops-gateway-1  Healthy"]
        if args == R.CMD_COMPOSE_PS:
            return 0, [f"id{i:02d}" for i in range(self.container_lines)]
        if args == R.CMD_STOCK_REFILL:
            return 0, ["UPDATE 50"]
        if args == R.CMD_CHAOS_STATUS:
            return 0, ["no active faults"]
        if args == R.CMD_CHAOS_CLEAR:
            return 0, ["cleared", "no active faults; the stack is healthy"]
        if args in R.CMD_REMOVE_LOADS:
            return 1, [f"Error response from daemon: No such container: {args[-1]}"]
        if args == R.CMD_GIT_HEAD:
            return 0, [self.commit]
        if args == R.CMD_GIT_STATUS:
            return 0, self.git_status.splitlines()
        raise AssertionError(f"unexpected command: {args}")

    def run(self, args, timeout, env=None, input_text=None):
        self.calls.append(("run", tuple(args)))
        self.inputs.append(input_text)
        code, lines = self.respond(args, input_text)
        text = "\n".join(lines) + ("\n" if lines else "")
        return R.Result(code, text if code == 0 else "", "" if code == 0 else text)

    def stream(self, args, timeout, on_line, env=None):
        self.calls.append(("stream", tuple(args)))
        self.inputs.append(None)
        self.envs.append(env)
        code, lines = self.respond(args)
        for line in lines:
            on_line(line)
        return code, list(lines)

    def detector_status(self):
        if self.status_script:
            item = self.status_script.pop(0)
            return item() if callable(item) else item
        return self.default_status

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.clock += seconds

    def utcnow(self):
        return self.now

    def detector_sources(self):
        return list(self.sources)

    def write_report(self, path, text):
        self.writes[str(path)] = text
        return str(path)

    @contextlib.contextmanager
    def protect(self):
        self.protected += 1
        yield

    # -- helpers for the tests
    @property
    def commands(self):
        return [args for _, args in self.calls]

    def index(self, args):
        return self.commands.index(tuple(args))

    def count(self, args):
        return self.commands.count(tuple(args))

    def suite_ids_run(self):
        return [self.by_command[args].id for args in self.commands if args in self.by_command]

    def output(self):
        return "\n".join(self.said)

    def report(self):
        assert len(self.writes) == 1, self.writes.keys()
        return next(iter(self.writes.values()))


def run_gate(gate, host=None, only=None, no_build=False, report="report.md"):
    host = host or FakeHost()
    code = R.Regression(host, gate, only, no_build, report).run()
    return code, host


def make_suite(**changes):
    base = dict(id="X-test", module="X", gate="A", command=("python", "-m", "unittest"), kind="unittest", expected=10, typical_s=None,
                timeout_s=60, needs_stack=False, description="test suite")
    base.update(changes)
    return R.Suite(**base)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class ParseUnittestTests(unittest.TestCase):
    def test_ok(self):
        parsed = R.parse_unittest(unittest_output(445))
        self.assertTrue(parsed.ok)
        self.assertEqual((parsed.ran, parsed.verdict, parsed.skipped), (445, "OK", 0))

    def test_ok_with_skips(self):
        parsed = R.parse_unittest(unittest_output(445, "OK (skipped=2)"))
        self.assertTrue(parsed.ok)
        self.assertEqual((parsed.ran, parsed.skipped), (445, 2))

    def test_failed(self):
        parsed = R.parse_unittest(unittest_output(445, "FAILED (failures=1, errors=2)"))
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.verdict, "FAILED (failures=1, errors=2)")
        self.assertEqual(parsed.ran, 445)

    def test_no_output(self):
        parsed = R.parse_unittest([])
        self.assertFalse(parsed.ok)
        self.assertIsNone(parsed.ran)
        self.assertIsNone(parsed.verdict)

    def test_one_test_uses_the_singular(self):
        self.assertEqual(R.parse_unittest(["Ran 1 test in 0.001s", "", "OK"]).ran, 1)

    def test_text_that_only_starts_with_ok_is_not_a_verdict(self):
        parsed = R.parse_unittest(["Ran 3 tests in 0.1s", "OKAY then", "Okay"])
        self.assertIsNone(parsed.verdict)
        self.assertFalse(parsed.ok)

    def test_traceback_noise_before_the_summary_is_ignored(self):
        lines = ["Traceback (most recent call last):", "  File x", "AssertionError", "Ran 5 tests in 0.2s", "", "FAILED (failures=1)"]
        self.assertFalse(R.parse_unittest(lines).ok)


class ParseChecksTests(unittest.TestCase):
    VERDICT = "DETECTOR CHECK PASSED"

    def test_pass(self):
        parsed = R.parse_checks(["[OK] a", "[OK] b", "2/2 checks passed", self.VERDICT], self.VERDICT)
        self.assertEqual((parsed.passed, parsed.total, parsed.verdict_found), (2, 2, True))
        self.assertEqual(parsed.fail_lines, ())

    def test_count_with_warnings_suffix(self):
        parsed = R.parse_checks(["164/164 checks passed   (3 warning(s))", self.VERDICT], self.VERDICT)
        self.assertEqual((parsed.passed, parsed.total), (164, 164))

    def test_mismatch(self):
        parsed = R.parse_checks(["[FAIL] x", "35/36 checks passed", "FAILED: x"], self.VERDICT)
        self.assertEqual((parsed.passed, parsed.total), (35, 36))
        self.assertFalse(parsed.verdict_found)
        self.assertEqual(parsed.failed_names, "x")
        self.assertEqual(parsed.fail_lines, ("[FAIL] x",))

    def test_missing_verdict(self):
        parsed = R.parse_checks(["2/2 checks passed"], self.VERDICT)
        self.assertFalse(parsed.verdict_found)

    def test_the_last_count_line_wins(self):
        lines = ["22/22 checks passed", "SMOKE TEST PASSED", "29/29 checks passed", "OBSERVABILITY CHECK PASSED"]
        parsed = R.parse_checks(lines, "OBSERVABILITY CHECK PASSED")
        self.assertEqual((parsed.passed, parsed.total), (29, 29))

    def test_no_count_line(self):
        parsed = R.parse_checks(["[OK] a", self.VERDICT], self.VERDICT)
        self.assertIsNone(parsed.total)

    def test_verdict_must_be_a_whole_line(self):
        parsed = R.parse_checks(["2/2 checks passed", "DETECTOR CHECK PASSED?"], self.VERDICT)
        self.assertFalse(parsed.verdict_found)


class ParseOtherTests(unittest.TestCase):
    def test_pytest_sample(self):
        self.assertEqual(R.parse_pytest_count(PYTEST_SAMPLE), 10)

    def test_pytest_failed_and_passed(self):
        self.assertEqual(R.parse_pytest_count(["1 failed, 9 passed in 1.0s"]), 9)

    def test_pytest_without_summary(self):
        self.assertIsNone(R.parse_pytest_count(["collected 0 items", "no tests ran in 0.01s"]))

    def test_rules_output(self):
        self.assertEqual(R.parse_rules_valid(rules_output()), (9, True))
        self.assertEqual(R.parse_rules_valid(rules_output(pending="pending provenance: R03")), (9, False))
        self.assertEqual(R.parse_rules_valid(["rules file x: invalid"]), (None, False))

    def test_scan_imports_clean(self):
        hits, positive, files = R.scan_imports([("a.py", "import urllib.request\n"), ("b.py", "import threading\nx = 1\n")])
        self.assertEqual((hits, positive, files), ([], 2, 2))

    def test_scan_imports_is_case_insensitive_and_names_the_line(self):
        hits, _, _ = R.scan_imports([("a.py", "x = 1\nimport Docker\n"), ("b.py", "REDIS = 1\n")])
        self.assertEqual(hits, ["a.py:2: import Docker", "b.py:1: REDIS = 1"])

    def test_every_forbidden_word_is_found(self):
        for word in ("psycopg", "redis", "docker", "subprocess", "requests"):
            hits, _, _ = R.scan_imports([("a.py", f"import {word}\n")])
            self.assertEqual(len(hits), 1, word)


class CountRuleTests(unittest.TestCase):
    def test_equal(self):
        self.assertEqual(R.judge_count(make_suite(expected=445), 445), (True, "", ()))

    def test_different_fails_with_the_documented_text(self):
        ok, detail, notes = R.judge_count(make_suite(expected=445), 446)
        self.assertFalse(ok)
        self.assertEqual(detail, "count differs from the ledger: expected 445, found 446; change the ledger by a decision")
        self.assertEqual(notes, ())

    def test_fewer_tests_also_fail(self):
        self.assertFalse(R.judge_count(make_suite(expected=445), 444)[0])

    def test_none_prints_new_and_passes(self):
        ok, detail, notes = R.judge_count(make_suite(expected=None), 37)
        self.assertTrue(ok)
        self.assertEqual(notes, ("[NEW] record this count in docs/REGRESSION.md: 37  (X-test)",))
        self.assertTrue(notes[0].startswith("[NEW] record this count in docs/REGRESSION.md: 37"))

    def test_strict_without_a_found_count_fails(self):
        ok, detail, _ = R.judge_count(make_suite(expected=445), None)
        self.assertFalse(ok)
        self.assertIn("expected 445", detail)
        self.assertFalse(R.judge_count(make_suite(expected=None), None)[0])

    def test_info_policy_never_fails(self):
        suite = make_suite(expected=10, count_policy="info")
        ok, detail, notes = R.judge_count(suite, 12)
        self.assertTrue(ok)
        self.assertTrue(notes[0].startswith("[WARN]"))
        self.assertTrue(R.judge_count(suite, None)[0])
        self.assertEqual(R.judge_count(suite, 10), (True, "", ()))

    def test_none_policy_tracks_nothing(self):
        suite = make_suite(expected=None, count_policy="none")
        self.assertEqual(R.judge_count(suite, 5), (True, "", ()))
        self.assertEqual(R.judge_count(suite, None), (True, "", ()))


class EvaluateTests(unittest.TestCase):
    def test_unittest_pass(self):
        outcome = R.evaluate(make_suite(expected=445), 0, unittest_output(445))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.found, 445)

    def test_unittest_exit_code_decides_even_if_ok_is_printed(self):
        self.assertFalse(R.evaluate(make_suite(expected=445), 1, unittest_output(445)).ok)

    def test_unittest_needs_the_verdict_even_with_exit_code_zero(self):
        outcome = R.evaluate(make_suite(expected=445), 0, ["Ran 445 tests in 1s"])
        self.assertFalse(outcome.ok)
        self.assertIn("no OK verdict", outcome.detail)

    def test_unittest_failed_text(self):
        outcome = R.evaluate(make_suite(expected=445), 1, unittest_output(445, "FAILED (failures=3)"))
        self.assertFalse(outcome.ok)
        self.assertIn("FAILED (failures=3)", outcome.detail)

    def test_unittest_count_difference_is_a_fail(self):
        outcome = R.evaluate(make_suite(expected=445), 0, unittest_output(446))
        self.assertFalse(outcome.ok)
        self.assertIn("expected 445, found 446", outcome.detail)

    def test_unittest_skips_pass_with_a_warning(self):
        outcome = R.evaluate(make_suite(expected=445), 0, unittest_output(445, "OK (skipped=1)"))
        self.assertTrue(outcome.ok)
        self.assertTrue(any(note.startswith("[WARN]") and "skipped" in note for note in outcome.notes))

    def test_unittest_new_count(self):
        outcome = R.evaluate(make_suite(expected=None), 0, unittest_output(12))
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.notes[0].startswith("[NEW] record this count in docs/REGRESSION.md: 12"))

    def test_timeout_and_missing_program(self):
        self.assertIn("timed out after 60 s", R.evaluate(make_suite(), 124, ["timed out after 60 s"]).detail)
        self.assertFalse(R.evaluate(make_suite(), 124, []).ok)
        self.assertIn("command not found: docker", R.evaluate(make_suite(), 127, ["command not found: docker"]).detail)

    def checks_suite(self, **changes):
        return make_suite(kind="checks", expected=36, verdict="CHAOS CHECK PASSED", **changes)

    def test_checks_pass(self):
        suite = self.checks_suite()
        outcome = R.evaluate(suite, 0, checks_output(suite))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.found, 36)

    def test_checks_mismatch_fails_even_with_exit_code_zero(self):
        suite = self.checks_suite()
        self.assertFalse(R.evaluate(suite, 0, checks_output(suite, passed=35)).ok)

    def test_checks_missing_verdict_fails(self):
        suite = self.checks_suite()
        outcome = R.evaluate(suite, 0, checks_output(suite, verdict=False))
        self.assertFalse(outcome.ok)
        self.assertIn("CHAOS CHECK PASSED", outcome.detail)

    def test_checks_fail_line_fails_even_if_the_rest_looks_green(self):
        suite = self.checks_suite()
        outcome = R.evaluate(suite, 0, checks_output(suite, extra=["[FAIL] something"]))
        self.assertFalse(outcome.ok)
        self.assertIn("[FAIL] line", outcome.detail)

    def test_checks_nonzero_exit_names_the_failed_checks(self):
        suite = self.checks_suite()
        outcome = R.evaluate(suite, 1, ["[FAIL] a", "35/36 checks passed", "FAILED: a"])
        self.assertFalse(outcome.ok)
        self.assertIn("FAILED: a", outcome.detail)

    def test_checks_without_a_count_line_fails(self):
        suite = self.checks_suite()
        self.assertFalse(R.evaluate(suite, 0, [suite.verdict]).ok)

    def test_checks_zero_of_zero_fails(self):
        suite = make_suite(kind="checks", expected=None, verdict="CHAOS CHECK PASSED")
        self.assertFalse(R.evaluate(suite, 0, ["0/0 checks passed", suite.verdict]).ok)

    def test_checks_count_difference_is_a_fail(self):
        suite = self.checks_suite()
        outcome = R.evaluate(suite, 0, checks_output(suite, passed=37, total=37))
        self.assertFalse(outcome.ok)
        self.assertIn("expected 36, found 37", outcome.detail)

    def test_exit_kind_uses_the_exit_code_and_the_count_is_informational(self):
        suite = make_suite(kind="exit", expected=10, count_policy="info")
        self.assertTrue(R.evaluate(suite, 0, PYTEST_SAMPLE).ok)
        self.assertEqual(R.evaluate(suite, 0, PYTEST_SAMPLE).found, 10)
        self.assertFalse(R.evaluate(suite, 1, PYTEST_SAMPLE).ok)
        different = R.evaluate(suite, 0, ["11 passed in 1s"])
        self.assertTrue(different.ok)
        self.assertTrue(any(n.startswith("[WARN]") for n in different.notes))
        unknown = R.evaluate(suite, 0, ["something else"])
        self.assertTrue(unknown.ok)
        self.assertIsNone(unknown.found)

    def test_rules_kind(self):
        suite = R.SUITE_BY_ID["M4-rules"]
        self.assertTrue(R.evaluate(suite, 0, rules_output()).ok)
        self.assertFalse(R.evaluate(suite, 1, rules_output()).ok)
        self.assertFalse(R.evaluate(suite, 0, rules_output(pending="pending provenance: R03")).ok)
        self.assertFalse(R.evaluate(suite, 0, ["nothing"]).ok)
        self.assertFalse(R.evaluate(suite, 0, rules_output(valid=8)).ok)

    def test_imports_kind(self):
        suite = R.SUITE_BY_ID["M4-imports"]
        clean = [("a.py", "import urllib.request\n")]
        self.assertTrue(R.evaluate(suite, 0, [], clean).ok)
        self.assertFalse(R.evaluate(suite, 0, [], clean + [("b.py", "import psycopg\n")]).ok)
        no_control = R.evaluate(suite, 0, [], [("a.py", "x = 1\n")])
        self.assertFalse(no_control.ok)
        self.assertIn("positive control", no_control.detail)
        self.assertFalse(R.evaluate(suite, 0, [], []).ok)

    def test_unknown_kind_is_an_error(self):
        with self.assertRaises(ValueError):
            R.evaluate(make_suite(kind="bogus"), 0, [])


class FormatTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual([R.format_duration(s) for s in (None, 45, 60, 240, 600, 1120, 4080)],
                         ["not measured", "45 s", "1 min", "4 min", "10 min", "18 min 40 s", "68 min"])

    def test_estimate(self):
        suites = [make_suite(id="a", typical_s=100), make_suite(id="b", typical_s=None), make_suite(id="c", typical_s=50)]
        self.assertEqual(R.estimate(suites), (150, ["b"]))
        text = R.estimate_text(suites)
        self.assertIn("at least about 3 minutes", text)
        self.assertIn("not measured, so not included: b", text)

    def test_estimate_with_no_measured_duration_says_so(self):
        text = R.estimate_text([make_suite(id="a", typical_s=None)])
        self.assertTrue(text.startswith("no measured duration in the ledger"))
        self.assertNotIn("0 minutes", text)

    def test_estimate_without_unknown(self):
        self.assertNotIn("not measured", R.estimate_text([make_suite(typical_s=60)]))

    def test_verdict_lines(self):
        self.assertEqual(R.verdict_line("B", True), "REGRESSION GATE B PASSED")
        self.assertEqual(R.verdict_line("B", False), "REGRESSION GATE B FAILED")
        self.assertEqual(R.verdict_line("C", True, (2, 15)), "REGRESSION GATE C PASSED (partial: 2 of 15 suites)")

    def test_ledger_row(self):
        self.assertEqual(R.ledger_row("2026-11-01", "abc1234", "B", True, "M4-unit 445"), "| 2026-11-01 | abc1234 | B | PASS | M4-unit 445 |")
        self.assertIn("| FAIL |", R.ledger_row("2026-11-01", "abc1234", "A", False, "-"))

    def test_table(self):
        lines = R.format_table(["A", "Bee"], [["x", "yy"], ["longer", "z"]])
        self.assertEqual(lines[0], "A       Bee")
        self.assertEqual(lines[1], "------  ---")

    def test_tail_keeps_the_last_lines_and_drops_blanks(self):
        lines = [f"line {i}" for i in range(100)] + ["", "  "]
        tail = R.tail_lines(lines)
        self.assertEqual(len(tail), 60)
        self.assertEqual(tail[-1], "line 99")
        self.assertEqual(tail[0], "line 40")


# ---------------------------------------------------------------------------
# The suite table (static)
# ---------------------------------------------------------------------------

class SuiteTableTests(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = [s.id for s in R.SUITES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 15)

    def test_fields_are_valid(self):
        for suite in R.SUITES:
            self.assertIn(suite.kind, R.KINDS, suite.id)
            self.assertIn(suite.gate, R.GATES, suite.id)
            self.assertIn(suite.count_policy, R.COUNT_POLICIES, suite.id)
            self.assertGreater(suite.timeout_s, 0, suite.id)
            self.assertTrue(suite.description, suite.id)
            self.assertIsInstance(suite.command, tuple, suite.id)
            if suite.expected is not None:
                self.assertGreater(suite.expected, 0, suite.id)
            if suite.typical_s is not None:
                self.assertGreater(suite.timeout_s, suite.typical_s, f"{suite.id}: the timeout must exceed the typical duration")

    def test_check_scripts_have_a_verdict_and_a_count_policy_that_tracks_the_count(self):
        for suite in R.SUITES:
            if suite.kind == "checks":
                self.assertTrue(suite.verdict.endswith("PASSED"), suite.id)
                self.assertEqual(suite.count_policy, "strict", suite.id)
                self.assertTrue(suite.needs_stack, suite.id)

    def test_in_process_suites_have_no_command_and_the_others_have_one(self):
        for suite in R.SUITES:
            self.assertEqual(bool(suite.internal), not suite.command, suite.id)
        self.assertEqual([s.id for s in R.SUITES if s.internal], ["M4-imports"])

    def test_expected_counts_of_the_ledger(self):
        expected = {s.id: s.expected for s in R.SUITES}
        self.assertEqual(expected["M3-unit"], 243)
        self.assertEqual(expected["M4-unit"], 445)
        self.assertEqual(expected["M1-smoke"], 22)
        self.assertEqual(expected["M2-observability"], 29)
        self.assertEqual(expected["M3-chaos-quick"], 36)
        self.assertEqual(expected["M3-chaos-full"], 135)
        self.assertEqual(expected["M4-detector-full"], 164)
        self.assertEqual(expected["M4-detector-quick"], 41)
        self.assertEqual([expected[f"M1-unit-{name}"] for name in ("gateway", "orders", "payments", "inventory")], [11, 10, 10, 11])
        self.assertEqual(expected["M4-rules"], 9)
        self.assertEqual(expected["M1-unit-orders"], 10)

    def test_the_commands_of_the_handoff(self):
        commands = {s.id: s.command for s in R.SUITES}
        self.assertEqual(commands["M1-unit-gateway"], ("docker", "compose", "--profile", "test", "run", "--rm", "-e", "SERVICE=gateway", "tests"))
        self.assertEqual(commands["M3-chaos-quick"], ("python", "scripts/chaos_check.py", "--only", "F10", "--quick"))
        self.assertEqual(commands["M4-detector-quick"],
                         ("python", "scripts/detector_check.py", "--quick", "--only", "F10", "--skip-nodata", "--skip-persistence"))
        self.assertEqual(commands["M4-unit"], ("python", "-m", "unittest", "discover", "-s", "detector/tests"))
        self.assertEqual(R.SUITE_BY_ID["M4-rules"].env, (("PYTHONPATH", "detector"),))

    def test_gates_are_cumulative(self):
        a, b, c = ([s.id for s in R.gate_suites(g)] for g in "ABC")
        self.assertEqual(b[:len(a)], a)
        self.assertEqual(c[:len(b)], b)
        self.assertLess(len(a), len(b))
        self.assertLess(len(b), len(c))
        self.assertEqual([s.id for s in R.SUITES if s.gate == "A"], a)
        self.assertEqual(c[len(b):], ["M3-chaos-full", "M4-detector-full"])
        self.assertEqual(b[len(a):], ["M1-smoke", "M2-observability", "M3-chaos-quick", "M4-detector-quick"])

    def test_unit_suites_come_before_live_checks_in_every_gate(self):
        for gate in R.GATES:
            flags = [s.needs_stack for s in R.gate_suites(gate)]
            self.assertEqual(flags, sorted(flags), gate)          # all False first, then all True
        self.assertEqual([s.id for s in R.gate_suites("A") if s.needs_stack], [])

    def test_gate_a_needs_no_stack(self):
        self.assertFalse(any(s.needs_stack for s in R.gate_suites("A")))

    def test_every_compose_command_names_its_profile(self):
        gated = R.SERVICE_PROFILE
        for label, command in R.all_commands():
            if command[:2] != ("docker", "compose"):
                continue
            profiles = {command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "--profile"}
            skip_next, named = False, set()
            for arg in command[2:]:
                if skip_next:
                    skip_next = False
                    continue
                if arg == "--profile":
                    skip_next = True
                    continue
                named.add(arg)
            for service in named & set(gated):
                self.assertIn(gated[service], profiles, f"{label}: names {service} without --profile {gated[service]}")

    def test_the_stack_commands_carry_both_profiles(self):
        for command in (R.CMD_COMPOSE_UP, R.CMD_COMPOSE_PS):
            self.assertIn("observability", command)
            self.assertIn("detector", command)
            self.assertEqual(command.count("--profile"), 2)
        self.assertEqual(R.CMD_COMPOSE_UP[-4:], ("up", "-d", "--build", "--wait")[-4:])

    def test_the_m1_unit_tests_use_the_test_profile(self):
        for suite in R.SUITES:
            if suite.id.startswith("M1-unit-"):
                self.assertEqual(suite.command[:4], ("docker", "compose", "--profile", "test"))
                self.assertEqual(suite.command[-1], "tests")
                self.assertEqual(suite.kind, "exit", suite.id)
                self.assertEqual(suite.count_policy, "info", suite.id)        # pass by exit code, the count is informational

    def test_the_runner_never_starts_a_fault(self):
        for label, command in R.all_commands():
            if any("chaos/chaos.py" in arg for arg in command):
                self.assertIn(command[2], ("status", "clear"), label)
            self.assertNotIn("inject", command, label)
            self.assertFalse(any("chaos/chaos.py" in arg for arg in command) and "run" in command[2:3], label)
        for suite in R.SUITES:
            self.assertFalse("chaos/chaos.py" in " ".join(suite.command), suite.id)   # faults only through the check scripts

    def test_no_command_uses_a_shell_string(self):
        for label, command in R.all_commands():
            self.assertIsInstance(command, tuple, label)
            self.assertTrue(all(isinstance(arg, str) for arg in command), label)

    def test_the_stock_refill_command(self):
        self.assertEqual(R.CMD_STOCK_REFILL[-1], "psql -U $POSTGRES_USER -d $POSTGRES_DB")
        self.assertEqual(R.STOCK_SQL, "UPDATE items SET stock = 1000;\n")
        self.assertEqual(R.STOCK_WAIT_S, 35)

    def test_constants(self):
        self.assertEqual(R.EXPECTED_CONTAINERS, 12)
        self.assertEqual(R.LOAD_CONTAINERS, ("sentinelops-check-load", "sentinelops-detector-check-load"))
        self.assertEqual((R.SOURCES_OK_WAIT_S, R.OPEN_INCIDENT_WAIT_S, R.OPEN_INCIDENT_POLL_S), (120, 300, 10))
        self.assertEqual(R.REPORT_TAIL_LINES, 60)


class SelectTests(unittest.TestCase):
    def test_no_only_gives_the_whole_gate(self):
        selected, unknown = R.select_suites("B")
        self.assertEqual([s.id for s in selected], [s.id for s in R.gate_suites("B")])
        self.assertEqual(unknown, [])

    def test_only_keeps_the_gate_order_and_ignores_case(self):
        selected, unknown = R.select_suites("B", ["m4-unit", "M1-smoke", "M3-unit"])
        self.assertEqual([s.id for s in selected], ["M3-unit", "M4-unit", "M1-smoke"])
        self.assertEqual(unknown, [])

    def test_only_unknown_or_outside_the_gate(self):
        selected, unknown = R.select_suites("A", ["M4-unit", "M1-smoke", "nope"])
        self.assertEqual([s.id for s in selected], ["M4-unit"])
        self.assertEqual(unknown, ["M1-smoke", "nope"])


# ---------------------------------------------------------------------------
# Prerequisites, fake host, fake clock
# ---------------------------------------------------------------------------

class PrerequisiteTests(unittest.TestCase):
    def prepare(self, no_build=False):
        host = FakeHost()
        return host, R.Regression(host, "B", ["M1-smoke"], no_build, "r.md")

    def names(self, regression):
        return [name for name, _, _ in regression.checks]

    def test_all_prerequisites_pass_in_order(self):
        host, regression = self.prepare()
        self.assertTrue(regression.prerequisites())
        self.assertTrue(all(ok for _, ok, _ in regression.checks))
        self.assertEqual(len(regression.checks), 7)
        order = [host.index(c) for c in (R.CMD_DOCKER_INFO, R.CMD_COMPOSE_UP, R.CMD_COMPOSE_PS, R.CMD_STOCK_REFILL, R.CMD_CHAOS_STATUS)]
        self.assertEqual(order, sorted(order))
        self.assertEqual(host.inputs[host.index(R.CMD_STOCK_REFILL)], R.STOCK_SQL)
        self.assertIn(35, host.slept)

    def test_no_build_skips_compose_up(self):
        host, regression = self.prepare(no_build=True)
        self.assertTrue(regression.prerequisites())
        self.assertEqual(host.count(R.CMD_COMPOSE_UP), 0)
        self.assertEqual(len(regression.checks), 6)
        self.assertIn("rebuild skipped", host.output())

    def test_docker_does_not_answer(self):
        host, regression = self.prepare()
        host.override(lambda args: args == R.CMD_DOCKER_INFO, 1, ["Cannot connect to the Docker daemon"])
        self.assertFalse(regression.prerequisites())
        self.assertEqual(host.count(R.CMD_COMPOSE_UP), 0)
        self.assertEqual(regression.checks[-1][:2], ("Docker answers (docker info)", False))

    def test_compose_up_fails(self):
        host, regression = self.prepare()
        host.override(lambda args: args == R.CMD_COMPOSE_UP, 1, ["build failed"])
        self.assertFalse(regression.prerequisites())
        self.assertEqual(host.count(R.CMD_COMPOSE_PS), 0)
        self.assertIn("exit code 1", regression.checks[-1][2])

    def test_wrong_container_count(self):
        for lines in (11, 13):
            host, regression = self.prepare()
            host.container_lines = lines
            self.assertFalse(regression.prerequisites(), lines)
            self.assertIn(f"{lines} listed", regression.checks[-1][2])
            self.assertEqual(host.count(R.CMD_STOCK_REFILL), 0)

    def test_too_many_containers_hint_names_the_load_containers(self):
        host, regression = self.prepare()
        host.container_lines = 13
        regression.prerequisites()
        self.assertIn("docker rm -f sentinelops-check-load sentinelops-detector-check-load", regression.checks[-1][2])

    def test_sources_become_ok_after_a_wait(self):
        host, regression = self.prepare()
        warming = {"sources": {"prometheus": {"state": "ok"}, "loki": {"state": "warming_up"}}, "open_incident_id": None}
        host.status_script = [warming, warming, warming]
        self.assertTrue(regression.prerequisites())
        self.assertGreaterEqual(host.slept.count(R.SOURCES_POLL_S), 2)

    def test_sources_never_ok_fails_after_120_seconds_of_fake_time(self):
        host, regression = self.prepare()
        host.default_status = {"sources": {"prometheus": {"state": "ok"}, "loki": {"state": "no_data"}}, "open_incident_id": None}
        start = host.clock
        self.assertFalse(regression.prerequisites())
        self.assertGreaterEqual(host.clock - start, R.SOURCES_OK_WAIT_S)
        self.assertLess(host.clock - start, R.SOURCES_OK_WAIT_S + 10)
        self.assertEqual(host.count(R.CMD_STOCK_REFILL), 0)

    def test_detector_api_does_not_answer(self):
        host, regression = self.prepare()
        host.default_status = None
        self.assertFalse(regression.prerequisites())
        self.assertIn("sources prometheus and loki", regression.checks[-1][0])

    def test_stock_refill_must_print_update_50(self):
        host, regression = self.prepare()
        host.override(lambda args: args == R.CMD_STOCK_REFILL, 0, ["UPDATE 0"])
        self.assertFalse(regression.prerequisites())
        host2, regression2 = self.prepare()
        host2.override(lambda args: args == R.CMD_STOCK_REFILL, 1, ["psql: error"])
        self.assertFalse(regression2.prerequisites())
        self.assertEqual(host2.count(R.CMD_CHAOS_STATUS), 0)

    def test_an_active_fault_is_cleared_once_with_a_warning(self):
        host, regression = self.prepare()
        state = {"status_calls": 0}
        original = host.respond

        def respond(args, input_text=None):
            if tuple(args) == R.CMD_CHAOS_STATUS:
                state["status_calls"] += 1
                return (0, ["ID   Target    Run id  Age   State", "F10  postgres  r-1     12 s  active"]) if state["status_calls"] == 1 else (0, ["no active faults"])
            return original(args, input_text)

        host.respond = respond
        self.assertTrue(regression.prerequisites())
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)
        self.assertEqual(host.count(R.CMD_CHAOS_STATUS), 2)
        self.assertTrue(any(w.startswith("a chaos fault is active") for w in regression.warnings))
        self.assertIn("[WARN] a chaos fault is active", host.output())
        self.assertTrue(regression.live_started)

    def test_a_fault_that_cannot_be_cleared_fails(self):
        host, regression = self.prepare()
        host.override(lambda args: args == R.CMD_CHAOS_STATUS, 0, ["ID   Target    Run id  Age   State", "F10  postgres  r-1     12 s  active"])
        self.assertFalse(regression.prerequisites())
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)

    def test_a_stale_fault_table_with_exit_code_0_is_not_a_pass(self):
        # real chaos.py status exits 0 even when it lists active or stale faults: only the text `no active faults` counts
        host, regression = self.prepare()
        host.override(lambda args: args == R.CMD_CHAOS_STATUS, 0, ["F13  redis  r-2  3 min  stale", "F13 is stale: the container is running again"])
        self.assertFalse(regression.prerequisites())
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)
        self.assertIn("[FAIL] no chaos fault is active", host.output())

    def test_no_fault_active_needs_exit_code_0_and_the_text(self):
        # the fake host moves the output of a failed command to stderr, so the helper is tested with Result objects
        self.assertTrue(R.Regression.no_fault_active(R.Result(0, "no active faults\n", "")))
        self.assertFalse(R.Regression.no_fault_active(R.Result(1, "no active faults\n", "")))
        self.assertFalse(R.Regression.no_fault_active(R.Result(0, "F10  postgres  r-1  12 s  active\n", "")))
        self.assertFalse(R.Regression.no_fault_active(R.Result(0, "", "")))

    def test_an_open_incident_that_resolves(self):
        host, regression = self.prepare()
        # calls: sources wait (1), then the open-incident polls
        host.status_script = [ok_status(), ok_status("INC-0001"), ok_status("INC-0001"), ok_status(None)]
        self.assertTrue(regression.prerequisites())
        self.assertEqual(host.slept.count(R.OPEN_INCIDENT_POLL_S), 2)

    def test_an_incident_that_stays_open_fails_after_300_seconds_of_fake_time(self):
        host, regression = self.prepare()
        host.default_status = ok_status("INC-0007")
        start = host.clock
        self.assertFalse(regression.prerequisites())
        self.assertGreaterEqual(host.clock - start, R.OPEN_INCIDENT_WAIT_S)
        self.assertIn("INC-0007 is still open", regression.checks[-1][2])


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

class GateRunTests(unittest.TestCase):
    def test_gate_a_runs_only_the_unit_suites_in_order(self):
        code, host = run_gate("A")
        self.assertEqual(code, 0)
        self.assertEqual(host.suite_ids_run(), [s.id for s in R.gate_suites("A") if s.command])
        self.assertEqual(host.count(R.CMD_DOCKER_INFO), 0)
        self.assertEqual(host.count(R.CMD_COMPOSE_UP), 0)

    def test_gate_a_has_no_cleanup(self):
        _, host = run_gate("A")
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 0)
        self.assertEqual(host.protected, 0)
        for command in R.CMD_REMOVE_LOADS:
            self.assertEqual(host.count(command), 0)

    def test_gate_b_order_units_then_prerequisites_then_live(self):
        code, host = run_gate("B")
        self.assertEqual(code, 0, host.output())
        ids = host.suite_ids_run()
        self.assertEqual(ids, [s.id for s in R.gate_suites("B") if s.command])
        first_live = host.index(R.SUITE_BY_ID["M1-smoke"].command)
        self.assertLess(host.index(R.SUITE_BY_ID["M4-unit"].command), host.index(R.CMD_DOCKER_INFO))
        self.assertLess(host.index(R.CMD_STOCK_REFILL), first_live)
        self.assertLess(host.index(R.CMD_CHAOS_STATUS), first_live)

    def test_gate_c_includes_a_and_b_and_ends_with_the_full_runs(self):
        code, host = run_gate("C")
        self.assertEqual(code, 0, host.output())
        ids = host.suite_ids_run()
        self.assertEqual(ids[-2:], ["M3-chaos-full", "M4-detector-full"])
        self.assertEqual(ids, [s.id for s in R.gate_suites("C") if s.command])
        self.assertIn("REGRESSION GATE C PASSED", host.output())

    def test_the_prerequisites_run_once_and_the_quiet_wait_between_live_suites(self):
        _, host = run_gate("C")
        self.assertEqual(host.count(R.CMD_COMPOSE_UP), 1)
        self.assertEqual(host.count(R.CMD_STOCK_REFILL), 1)
        text = host.output()
        for before in ("M2-observability", "M3-chaos-quick", "M4-detector-quick", "M3-chaos-full", "M4-detector-full"):
            self.assertIn(f"no open incident (before {before})", text)

    def test_no_build_skips_the_rebuild_in_a_run(self):
        _, host = run_gate("B", no_build=True)
        self.assertEqual(host.count(R.CMD_COMPOSE_UP), 0)

    def test_the_stack_check_is_not_done_for_unit_only_selections(self):
        code, host = run_gate("B", only=["M4-unit", "R1-unit"])
        self.assertEqual(code, 0)
        self.assertEqual(host.count(R.CMD_DOCKER_INFO), 0)
        self.assertEqual(host.suite_ids_run(), ["M4-unit", "R1-unit"])
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 0)

    def test_a_suite_does_not_stop_the_others(self):
        host = FakeHost()
        host.override_suite("M3-unit", 1, unittest_output(243, "FAILED (failures=1)"))
        code, host = run_gate("A", host)
        self.assertEqual(code, 1)
        self.assertEqual(host.suite_ids_run(), [s.id for s in R.gate_suites("A") if s.command])
        self.assertIn("FAILED: M3-unit", host.output())
        self.assertIn("REGRESSION GATE A FAILED", host.output())

    def test_a_count_difference_fails_the_gate(self):
        host = FakeHost()
        host.override_suite("M4-unit", 0, unittest_output(446))
        code, host = run_gate("A", host)
        self.assertEqual(code, 1)
        self.assertIn("count differs from the ledger: expected 445, found 446; change the ledger by a decision", host.output())

    def test_new_count_is_printed_and_passes(self):
        # every count of the ledger is recorded, so the first-run case uses a table in which one count is None again
        changed = tuple(s if s.id != "M4-detector-quick" else dataclasses.replace(s, expected=None) for s in R.SUITES)
        with mock.patch.object(R, "SUITES", changed):
            code, host = run_gate("B")
        self.assertEqual(code, 0)
        self.assertIn("[NEW] record this count in docs/REGRESSION.md: 7  (M4-detector-quick)", host.output())
        self.assertIn("count(s) to record", host.output())

    def test_a_failed_prerequisite_blocks_every_live_suite_but_not_the_unit_suites(self):
        host = FakeHost()
        host.override(lambda args: args == R.CMD_DOCKER_INFO, 1, ["no daemon"])
        code, host = run_gate("B", host)
        self.assertEqual(code, 1)
        self.assertEqual(host.suite_ids_run(), [s.id for s in R.gate_suites("A") if s.command])
        for suite_id in ("M1-smoke", "M2-observability", "M3-chaos-quick", "M4-detector-quick"):
            self.assertIn(f"[FAIL] {suite_id}  (not run: a prerequisite failed)", host.output())
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 0)         # no live suite was started: no cleanup

    def test_an_incident_that_stays_open_between_live_suites_blocks_the_rest(self):
        host = FakeHost()
        calls = {"n": 0}

        def status():
            calls["n"] += 1
            return ok_status("INC-0042") if host.suite_ids_run().count("M1-smoke") and "M2-observability" not in host.suite_ids_run() else ok_status()

        host.status_script = []
        host.detector_status = status
        code, host = run_gate("B", host)
        self.assertEqual(code, 1)
        self.assertEqual(host.suite_ids_run()[-1], "M1-smoke")
        self.assertIn("[FAIL] M2-observability  (not run: an incident stayed open)", host.output())
        self.assertIn("[FAIL] M4-detector-quick  (not run: an incident stayed open)", host.output())
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)         # the cleanup runs: M1-smoke was a live suite

    def test_a_suite_that_times_out_is_a_fail_with_the_reason(self):
        host = FakeHost()
        host.override_suite("M1-smoke", 124, ["timed out after 600 s"])
        code, host = run_gate("B", host)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] M1-smoke  (timed out after 600 s", host.output())
        self.assertEqual(host.suite_ids_run()[-1], "M4-detector-quick")      # the later suites still ran

    def test_an_m1_service_count_that_changes_is_only_a_warning(self):
        host = FakeHost()
        host.override_suite("M1-unit-orders", 0, [".", "11 passed in 0.9s"])
        code, host = run_gate("A", host)
        self.assertEqual(code, 0)
        self.assertIn("[WARN] M1-unit-orders: count differs from the ledger: expected 10, found 11", host.output())

    def test_an_m1_service_test_that_exits_nonzero_fails(self):
        host = FakeHost()
        host.override_suite("M1-unit-payments", 1, ["1 failed, 9 passed in 0.9s"])
        code, host = run_gate("A", host)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] M1-unit-payments  (exit code 1", host.output())

    def test_a_suite_that_cannot_start_is_a_fail(self):
        host = FakeHost()
        host.override_suite("M1-unit-orders", 127, ["command not found: docker"])
        code, host = run_gate("A", host)
        self.assertEqual(code, 1)
        self.assertIn("command not found: docker", host.output())

    def test_insufficient_stock_gets_a_hint(self):
        host = FakeHost()
        host.override_suite("M2-observability", 1, ["[FAIL] order: HTTP 409 insufficient_stock", "28/29 checks passed", "FAILED: order"])
        _, host = run_gate("B", host)
        self.assertIn("the stock is exhausted", host.output())
        self.assertIn("--only M2-observability --no-build", host.output())

    def test_verdict_says_partial_for_only(self):
        code, host = run_gate("B", only=["M4-unit", "M3-unit"])
        self.assertEqual(code, 0)
        self.assertIn("REGRESSION GATE B PASSED (partial: 2 of 13 suites)", host.output())
        self.assertIn("partial: 2 of 13 suites", host.report())

    def test_full_gate_verdict_is_not_partial(self):
        _, host = run_gate("A")
        self.assertIn("REGRESSION GATE A PASSED", host.output())
        self.assertNotIn("partial", host.output().split("REGRESSION GATE A PASSED")[1].splitlines()[0])

    def test_checks_line_counts_prerequisites_and_suites(self):
        _, host = run_gate("B")
        summary = [line.strip() for line in host.said if re.fullmatch(r"\n\d+/\d+ checks passed", line)]       # not the echoed suite output (indented)
        self.assertEqual(len(summary), 1)
        passed, total = summary[0].split()[0].split("/")
        self.assertEqual(passed, total)
        self.assertEqual(int(total), 13 + 7 + 3)           # 13 suites, 7 prerequisites, 3 waits between live suites

    def test_a_bug_in_the_runner_is_reported_and_cleaned_up(self):
        host = FakeHost()
        host.override_suite("M1-smoke", raises=RuntimeError("boom"))
        code, host = run_gate("B", host)
        self.assertEqual(code, 1)
        self.assertIn("runner error", host.output())
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)

    def test_the_banner_names_the_gate_the_suites_and_the_estimate(self):
        _, host = run_gate("C")
        text = host.output()
        self.assertIn("gate C (cumulative: A, B, C): 15 suites", text)
        self.assertIn("estimate: at least about 101 minutes", text)
        self.assertIn("M4-detector-full", text.splitlines()[1])


class CleanupTests(unittest.TestCase):
    def test_cleanup_after_a_live_run(self):
        _, host = run_gate("B")
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)
        for command in R.CMD_REMOVE_LOADS:
            self.assertEqual(host.count(command), 1)
        self.assertEqual(host.protected, 1)
        last_suite = max(host.index(s.command) for s in R.gate_suites("B") if s.command)
        self.assertGreater(host.index(R.CMD_CHAOS_CLEAR), last_suite)
        self.assertIn("sentinelops-check-load: not present", host.output())

    def test_cleanup_after_a_failed_live_suite(self):
        host = FakeHost()
        host.override_suite("M3-chaos-quick", 1, ["[FAIL] x", "35/36 checks passed", "FAILED: x"])
        _, host = run_gate("B", host)
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)

    def test_cleanup_reports_a_removed_container_and_a_failed_clear(self):
        host = FakeHost()
        host.override(lambda args: args == R.CMD_REMOVE_LOADS[0], 0, ["sentinelops-check-load"])
        host.override(lambda args: args == R.CMD_CHAOS_CLEAR, 1, ["could not clear"])
        _, host = run_gate("B", host)
        text = host.output()
        self.assertIn("removed sentinelops-check-load", text)
        self.assertIn("[WARN] chaos.py clear --all did not exit with 0", text)

    def test_cleanup_warns_when_a_container_cannot_be_removed(self):
        host = FakeHost()
        host.override(lambda args: args == R.CMD_REMOVE_LOADS[1], 1, ["permission denied"])
        _, host = run_gate("B", host)
        self.assertIn("[WARN] could not remove sentinelops-detector-check-load", host.output())

    def test_cleanup_errors_do_not_hide_the_result(self):
        host = FakeHost()
        host.override(lambda args: args == R.CMD_CHAOS_CLEAR, raises=OSError("disk"))
        code, host = run_gate("B", host)
        self.assertEqual(code, 0)
        self.assertIn("cleanup error: OSError", host.output())
        self.assertEqual(len(host.writes), 1)

    def test_interrupt_in_a_live_suite_cleans_up_and_exits_130(self):
        host = FakeHost()
        host.override_suite("M3-chaos-quick", raises=KeyboardInterrupt())
        code, host = run_gate("B", host)
        self.assertEqual(code, 130)
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 1)
        for command in R.CMD_REMOVE_LOADS:
            self.assertEqual(host.count(command), 1)
        self.assertEqual(host.suite_ids_run()[-1], "M3-chaos-quick")
        self.assertIn("INTERRUPTED", host.output())
        self.assertNotIn("PASSED", host.output().split("INTERRUPTED")[1])
        self.assertIn("Verdict: INTERRUPTED", host.report())

    def test_an_interrupted_run_is_not_a_pass_and_its_ledger_row_says_fail(self):
        host = FakeHost()
        host.override_suite("M4-unit", raises=KeyboardInterrupt())
        regression = R.Regression(host, "A", None, False, "r.md")
        self.assertEqual(regression.run(), 130)
        self.assertFalse(regression.passed)
        self.assertRegex(host.report(), r"\| 2026-11-01 \| abc1234 \| A \| FAIL \|")
        self.assertNotIn("| PASS |", host.report().split("## Ledger row")[1])

    def test_interrupt_in_a_unit_suite_needs_no_cleanup(self):
        host = FakeHost()
        host.override_suite("M3-unit", raises=KeyboardInterrupt())
        code, host = run_gate("B", host)
        self.assertEqual(code, 130)
        self.assertEqual(host.count(R.CMD_CHAOS_CLEAR), 0)
        self.assertEqual(len(host.writes), 1)

    def test_interrupt_in_the_prerequisites_after_a_fault_was_cleared_still_cleans_up(self):
        host = FakeHost()
        host.override(lambda args: args == R.CMD_STOCK_REFILL, raises=KeyboardInterrupt())
        code, host = run_gate("B", host)
        self.assertEqual(code, 130)


class ExitCodeAndOptionTests(unittest.TestCase):
    def main(self, argv, host=None):
        host = host or FakeHost()
        return R.main(argv, host), host

    def test_usage_errors_return_2(self):
        with contextlib.redirect_stderr(_Sink()):
            self.assertEqual(self.main([])[0], 2)
            self.assertEqual(self.main(["--gate", "D"])[0], 2)
            self.assertEqual(self.main(["--gate", "B", "--bogus"])[0], 2)
            self.assertEqual(self.main(["--gate", "B", "--only", "nope"])[0], 2)
            self.assertEqual(self.main(["--gate", "A", "--only", "M1-smoke"])[0], 2)

    def test_usage_errors_run_nothing(self):
        with contextlib.redirect_stderr(_Sink()):
            _, host = self.main(["--gate", "B", "--only", "nope"])
        self.assertEqual(host.calls, [])

    def test_gate_letter_is_case_insensitive(self):
        code, host = self.main(["--gate", "a", "--only", "R1-unit", "--report", "x.md"])
        self.assertEqual(code, 0)

    def test_pass_returns_0_and_fail_returns_1(self):
        self.assertEqual(self.main(["--gate", "A", "--report", "x.md"])[0], 0)
        host = FakeHost()
        host.override_suite("M4-unit", 1, unittest_output(445, "FAILED (failures=1)"))
        self.assertEqual(self.main(["--gate", "A", "--report", "x.md"], host)[0], 1)

    def test_list_prints_the_suites_and_runs_nothing(self):
        code, host = self.main(["--gate", "B", "--list"])
        self.assertEqual(code, 0)
        self.assertEqual(host.calls, [])
        self.assertEqual(host.writes, {})
        text = host.output()
        for suite in R.gate_suites("B"):
            self.assertIn(suite.id, text)
        self.assertNotIn("M3-chaos-full", text)
        self.assertIn("Gate B (cumulative): 13 suites", text)
        self.assertIn("estimate: at least about 14 minutes", text)

    def test_list_with_only(self):
        code, host = self.main(["--gate", "C", "--list", "--only", "M4-detector-full"])
        self.assertEqual(code, 0)
        self.assertIn("1 suites", host.output())
        self.assertIn("at least about 68 minutes", host.output())

    def test_dry_run_prints_every_command_and_runs_nothing(self):
        code, host = self.main(["--gate", "C", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(host.calls, [])
        self.assertEqual(host.writes, {})
        text = host.output()
        for suite in R.gate_suites("C"):
            self.assertIn(f"suite {suite.id}: {R.command_text(suite)}", text)
        self.assertIn("docker compose --profile observability --profile detector up -d --build --wait", text)
        self.assertIn("docker compose --profile observability --profile detector ps -q", text)
        self.assertIn('sh -c "psql -U $POSTGRES_USER -d $POSTGRES_DB"', text)
        self.assertIn("UPDATE items SET stock = 1000;", text)
        self.assertIn("python chaos/chaos.py clear --all", text)
        self.assertIn("docker rm -f sentinelops-check-load", text)
        self.assertIn("docker rm -f sentinelops-detector-check-load", text)
        self.assertEqual(text.count("prerequisites (before the first live suite)"), 1)
        self.assertEqual(text.count("wait up to 300 s until open_incident_id is null"), 5)

    def test_dry_run_gate_a_has_no_live_parts(self):
        _, host = self.main(["--gate", "A", "--dry-run"])
        self.assertNotIn("prerequisite", host.output())
        self.assertNotIn("cleanup", host.output())

    def test_dry_run_no_build(self):
        _, host = self.main(["--gate", "B", "--dry-run", "--no-build"])
        self.assertIn("docker compose up skipped: --no-build", host.output())
        self.assertNotIn("up -d --build --wait", host.output())

    def test_the_report_option(self):
        _, host = self.main(["--gate", "A", "--only", "R1-unit", "--report", "custom_report.md"])
        self.assertEqual(list(host.writes), ["custom_report.md"])

    def test_default_report_is_in_the_repo_root(self):
        host = FakeHost()
        R.main(["--gate", "A", "--only", "R1-unit"], host)
        self.assertEqual(list(host.writes), [str(R.ROOT / "regression_report.md")])


class _Sink:
    def write(self, text):
        return len(text)

    def flush(self):
        pass


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

class ReportTests(unittest.TestCase):
    def test_report_contents_of_a_green_run(self):
        host = FakeHost()
        host.git_status = "?? regression_report.md\n"
        _, host = run_gate("B", host)
        text = host.report()
        self.assertIn("# SentinelOps regression report", text)
        self.assertIn("- Timestamp (UTC): 2026-11-01 12:30:05", text)
        self.assertIn("- Commit: abc1234", text)
        self.assertIn("- Tracked files modified: no", text)
        self.assertIn("- Gate: B", text)
        self.assertIn("- Verdict: REGRESSION GATE B PASSED", text)
        self.assertIn("| Suite | Command | Expected | Found | Seconds | Result |", text)
        self.assertIn("| M4-unit | `python -m unittest discover -s detector/tests` | 445 | 445 | 0.0 | PASS |", text)
        self.assertIn("| M1-unit-gateway |", text)
        self.assertNotIn("Last 60 lines", text)

    def test_ledger_row_is_ready_to_paste(self):
        _, host = run_gate("B")
        rows = [line for line in host.report().splitlines() if line.startswith("| 2026-11-01 |")]
        self.assertEqual(len(rows), 1)
        self.assertRegex(rows[0], r"^\| 2026-11-01 \| abc1234 \| B \| PASS \| .+ \|$")
        self.assertIn("M4-unit 445", rows[0])
        self.assertIn("M2-observability 29", rows[0])

    def test_failed_suite_gets_the_last_60_lines(self):
        host = FakeHost()
        noise = [f"output line {i}" for i in range(100)]
        host.override_suite("M3-unit", 1, noise + ["Ran 243 tests in 1s", "", "FAILED (failures=1)"])
        _, host = run_gate("A", host)
        text = host.report()
        self.assertIn("## Last 60 lines of M3-unit", text)
        self.assertIn("output line 99", text)
        self.assertIn("FAILED (failures=1)", text)
        self.assertNotIn("output line 40\n", text)          # older than the last 60 non-blank lines
        self.assertEqual(text.count("output line "), 58)
        self.assertIn("| FAIL |", text)
        self.assertIn("- Verdict: REGRESSION GATE A FAILED", text)
        self.assertRegex(text, r"\| 2026-11-01 \| abc1234 \| A \| FAIL \|")

    def test_report_shows_modified_tracked_files(self):
        host = FakeHost()
        host.git_status = " M README.md\n M docs/PROPOSAL.md\n?? new.txt\n"
        _, host = run_gate("A", host)
        text = host.report()
        self.assertIn("- Tracked files modified: yes (2)", text)
        self.assertIn("` M README.md`", text)
        self.assertNotIn("new.txt", text)

    def test_report_without_git(self):
        host = FakeHost()
        host.override(lambda args: args in (R.CMD_GIT_HEAD, R.CMD_GIT_STATUS), 128, ["not a git repository"])
        _, host = run_gate("A", host)
        text = host.report()
        self.assertIn("- Commit: unknown", text)
        self.assertIn("unknown (git status failed)", text)

    def test_report_for_a_partial_run(self):
        _, host = run_gate("C", only=["M4-unit"])
        self.assertIn("- Gate: C (partial: 1 of 15 suites)", host.report())
        self.assertRegex(host.report(), r"\| C \(partial\) \| PASS \|")

    def test_not_run_suites_are_in_the_report_as_fail(self):
        host = FakeHost()
        host.override(lambda args: args == R.CMD_DOCKER_INFO, 1, ["no daemon"])
        _, host = run_gate("B", host)
        self.assertIn("not run: a prerequisite failed", host.report())
        self.assertIn("## Failed checks", host.report())

    def test_the_report_is_the_only_file_written(self):
        _, host = run_gate("C", report="only_this.md")
        self.assertEqual(list(host.writes), ["only_this.md"])

    def test_the_report_is_written_even_after_an_interrupt(self):
        host = FakeHost()
        host.override_suite("M4-unit", raises=KeyboardInterrupt())
        _, host = run_gate("A", host)
        self.assertEqual(len(host.writes), 1)

    def test_a_report_that_cannot_be_written_does_not_change_the_exit_code(self):
        host = FakeHost()

        def broken(path, text):
            raise OSError("read-only")

        host.write_report = broken
        code, host = run_gate("A", host)
        self.assertEqual(code, 0)
        self.assertIn("could not write the report", host.output())


# ---------------------------------------------------------------------------
# The whole runner against fake commands
# ---------------------------------------------------------------------------

class SimulatedWorldTests(unittest.TestCase):
    def test_gate_a_world(self):
        code, host = run_gate("A")
        self.assertEqual(code, 0)
        text = host.output()
        self.assertIn("\n9/9 checks passed", "\n".join(host.said))
        self.assertIn("REGRESSION GATE A PASSED", text)
        self.assertNotIn("[FAIL]", text)

    def test_gate_b_world_command_sequence(self):
        code, host = run_gate("B")
        self.assertEqual(code, 0, host.output())
        sequence = []
        for args in host.commands:
            if args in host.by_command:
                sequence.append(host.by_command[args].id)
            elif args in (R.CMD_DOCKER_INFO, R.CMD_COMPOSE_UP, R.CMD_COMPOSE_PS, R.CMD_STOCK_REFILL, R.CMD_CHAOS_STATUS,
                          R.CMD_CHAOS_CLEAR):
                sequence.append(" ".join(args[:3]))
        self.assertEqual(sequence[:9], ["M1-unit-gateway", "M1-unit-orders", "M1-unit-payments", "M1-unit-inventory", "M3-unit",
                                        "M4-unit", "R1-unit", "M4-rules", "docker info"])
        live = [s for s in sequence if s in ("M1-smoke", "M2-observability", "M3-chaos-quick", "M4-detector-quick")]
        self.assertEqual(live, ["M1-smoke", "M2-observability", "M3-chaos-quick", "M4-detector-quick"])
        self.assertEqual(sequence[-1], "python chaos/chaos.py clear")      # clear --all

    def test_gate_c_world_passes_with_the_ledger_counts(self):
        code, host = run_gate("C")
        self.assertEqual(code, 0, host.output())
        text = host.report()
        for suite_id, count in (("M3-chaos-full", 135), ("M4-detector-full", 164), ("M3-unit", 243), ("M4-unit", 445)):
            self.assertIn(f"{suite_id} {count}", text)
        self.assertEqual(sum(1 for line in host.output().splitlines() if line.startswith("[OK]")), 15 + 7 + 5)

    def test_the_output_of_a_suite_is_echoed_with_a_prefix(self):
        _, host = run_gate("A", only=["M4-unit"])
        self.assertIn("    Ran 445 tests in 1.234s", host.said)

    def test_the_environment_of_the_rules_suite_is_passed(self):
        _, host = run_gate("A", only=["M4-rules"])
        self.assertEqual(host.envs, [{"PYTHONPATH": "detector"}])

    def test_a_world_in_which_one_live_suite_fails_still_runs_the_rest(self):
        host = FakeHost()
        suite = R.SUITE_BY_ID["M3-chaos-quick"]
        host.override_suite("M3-chaos-quick", 1, checks_output(suite, passed=35, extra=["[FAIL] F10: opened too late"]) + ["FAILED: F10"])
        code, host = run_gate("B", host)
        self.assertEqual(code, 1)
        self.assertEqual(host.suite_ids_run()[-1], "M4-detector-quick")
        self.assertIn("[FAIL] M3-chaos-quick", host.output())
        self.assertIn("F10: opened too late", host.report())

    def test_a_world_with_a_wrong_container_count_fails_before_any_live_suite(self):
        host = FakeHost()
        host.container_lines = 11
        code, host = run_gate("C", host)
        self.assertEqual(code, 1)
        ids = host.suite_ids_run()
        self.assertEqual(ids, [s.id for s in R.gate_suites("A") if s.command])


# ---------------------------------------------------------------------------
# The real host (python itself is the child process; no Docker)
# ---------------------------------------------------------------------------

class RealHostTests(unittest.TestCase):
    def setUp(self):
        self.host = R.Host()

    def test_run_captures_output_and_the_exit_code(self):
        result = self.host.run(["python", "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], 30)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout.strip(), "out")
        self.assertEqual(result.stderr.strip(), "err")

    def test_run_passes_input_and_environment(self):
        code = "import os, sys; print(sys.stdin.read().strip() + '|' + os.environ['R1_TEST'])"
        result = self.host.run(["python", "-c", code], 30, env={"R1_TEST": "yes"}, input_text="hello\n")
        self.assertEqual(result.stdout.strip(), "hello|yes")

    def test_run_gives_no_stdin_to_children_that_do_not_get_input(self):
        result = self.host.run(["python", "-c", "import sys; print(repr(sys.stdin.read()))"], 30)
        self.assertEqual(result.stdout.strip(), "''")

    def test_run_missing_program_and_timeout(self):
        self.assertEqual(self.host.run(["definitely-not-a-program-r1"], 10).returncode, 127)
        slow = self.host.run(["python", "-c", "import time; time.sleep(30)"], 1)
        self.assertEqual(slow.returncode, 124)
        self.assertIn("timed out after 1 s", slow.stderr)

    def test_stream_delivers_lines_as_they_arrive(self):
        seen = []
        code, lines = self.host.stream(["python", "-c", "print('a'); print('b')"], 30, seen.append)
        self.assertEqual((code, lines, seen), (0, ["a", "b"], ["a", "b"]))

    def test_stream_exit_code_and_stderr_are_merged(self):
        seen = []
        code, lines = self.host.stream(["python", "-c", "import sys; print('x', file=sys.stderr); sys.exit(2)"], 30, seen.append)
        self.assertEqual((code, lines), (2, ["x"]))

    def test_stream_timeout_kills_the_child(self):
        code, lines = self.host.stream(["python", "-c", "import time; time.sleep(60)"], 1, lambda text: None)
        self.assertEqual(code, 124)
        self.assertEqual(lines[-1], "timed out after 1 s")

    def test_stream_missing_program(self):
        code, lines = self.host.stream(["definitely-not-a-program-r1"], 5, lambda text: None)
        self.assertEqual(code, 127)
        self.assertIn("command not found", lines[0])

    def test_stream_passes_the_environment(self):
        code, lines = self.host.stream(["python", "-c", "import os; print(os.environ['R1_TEST'])"], 30, lambda text: None,
                                       env={"R1_TEST": "42"})
        self.assertEqual(lines, ["42"])

    def test_children_start_without_a_shell_with_a_timeout_and_their_own_process_group(self):
        group = R._group_flags()
        self.assertEqual(len(group), 1)
        with mock.patch.object(R.subprocess, "run") as fake_run:
            fake_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.host.run(["docker", "info"], 17)
        kwargs = fake_run.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], 17)
        self.assertEqual(kwargs["cwd"], str(R.ROOT))
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertTrue(set(group) <= set(kwargs))
        with mock.patch.object(R.subprocess, "Popen") as fake_popen:
            child = fake_popen.return_value
            child.stdout = mock.MagicMock()
            child.stdout.__iter__.return_value = iter([])
            child.wait.return_value = 0
            child.poll.return_value = 0
            self.host.stream(["docker", "info"], 17, lambda text: None)
        kwargs = fake_popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], R.subprocess.DEVNULL)
        self.assertTrue(set(group) <= set(kwargs))
        child.wait.assert_called_with(timeout=17)

    def test_python_is_replaced_by_the_running_interpreter(self):
        self.assertEqual(R._resolve(["python", "-m", "x"]), [sys.executable, "-m", "x"])
        self.assertEqual(R._resolve(["docker", "info"]), ["docker", "info"])

    def test_write_report_writes_utf8_with_lf_into_the_given_folder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)            # registered first: it runs last
        target = Path(tmp.name) / "report.md"
        where = self.host.write_report(str(target), "line one\nline two\n")
        self.assertEqual(where, str(target))
        self.assertEqual(target.read_bytes(), b"line one\nline two\n")
        self.assertEqual(sorted(p.name for p in Path(tmp.name).iterdir()), ["report.md"])

    def test_a_relative_report_path_is_taken_from_the_repo_root(self):
        with mock.patch.object(Path, "write_text") as writer:
            where = self.host.write_report("relative_report.md", "x")
        self.assertEqual(where, str(R.ROOT / "relative_report.md"))
        self.assertEqual(writer.call_count, 1)

    def test_detector_sources_are_read_from_the_repo(self):
        sources = self.host.detector_sources()
        self.assertTrue(all(name.startswith("detector/app/") and name.endswith(".py") for name, _ in sources))

    def test_protect_ignores_ctrl_c_and_restores_the_handler(self):
        import signal
        before = signal.getsignal(signal.SIGINT)
        with self.host.protect():
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
        self.assertEqual(signal.getsignal(signal.SIGINT), before)

    def test_say_survives_a_character_the_console_cannot_show(self):
        class Narrow:
            encoding = "ascii"

            def __init__(self):
                self.parts = []

            def write(self, text):
                text.encode("ascii")            # raises UnicodeEncodeError like a narrow console
                self.parts.append(text)

            def flush(self):
                pass

        narrow = Narrow()
        with mock.patch.object(sys, "stdout", narrow):
            self.host.say("arrow \u2192 done")
        self.assertIn("arrow ? done", "".join(narrow.parts))


class DetectorStatusTests(unittest.TestCase):
    """The real detector_status() against a local HTTP server on 127.0.0.1 (read the request, answer, close)."""

    def serve(self, status, body):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_address[1]}/internal/v1/status"

    def test_answer_is_parsed(self):
        url = self.serve(200, json.dumps(ok_status("INC-0003")))
        with mock.patch.object(R, "DETECTOR_STATUS_URL", url):
            data = R.Host().detector_status()
        self.assertEqual(data["open_incident_id"], "INC-0003")
        self.assertEqual(data["sources"]["loki"]["state"], "ok")

    def test_an_error_status_gives_none(self):
        url = self.serve(503, "{}")
        with mock.patch.object(R, "DETECTOR_STATUS_URL", url):
            self.assertIsNone(R.Host().detector_status())

    def test_bad_json_gives_none(self):
        url = self.serve(200, "not json")
        with mock.patch.object(R, "DETECTOR_STATUS_URL", url):
            self.assertIsNone(R.Host().detector_status())

    def test_nothing_listening_gives_none(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        port = server.server_address[1]
        server.server_close()                       # the port is closed again
        with mock.patch.object(R, "DETECTOR_STATUS_URL", f"http://127.0.0.1:{port}/internal/v1/status"):
            self.assertIsNone(R.Host().detector_status())


# ---------------------------------------------------------------------------
# docs/REGRESSION.md equals SUITES
# ---------------------------------------------------------------------------

def table_rows(text, first_cell_pattern=r".+"):
    rows = []
    for line in text.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if re.fullmatch(first_cell_pattern, cells[0]):
                rows.append(cells)
    return rows


class LedgerDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = LEDGER_PATH.read_text(encoding="utf-8-sig")
        cls.rows = {row[0].strip("`"): row for row in table_rows(cls.doc, r"`[A-Za-z0-9-]+`")
                    if len(row) == 6 and row[2] in R.GATES}

    def test_the_ledger_exists(self):
        self.assertTrue(LEDGER_PATH.is_file())
        self.assertGreater(len(self.doc), 4000)

    def test_every_suite_is_in_the_table_once_and_nothing_else(self):
        self.assertEqual(sorted(self.rows), sorted(s.id for s in R.SUITES))
        ids_in_rows = [row[0].strip("`") for row in table_rows(self.doc, r"`[A-Za-z0-9-]+`") if len(row) == 6 and row[2] in R.GATES]
        self.assertEqual(len(ids_in_rows), len(set(ids_in_rows)))

    def test_every_expected_count_equals_suites(self):
        for suite in R.SUITES:
            self.assertEqual(self.rows[suite.id][4], R.expected_text(suite), suite.id)

    def test_module_gate_command_and_duration_equal_suites(self):
        for suite in R.SUITES:
            expected = R.suite_row(suite)
            self.assertEqual(self.rows[suite.id], expected, suite.id)

    def test_the_generated_table_is_in_the_doc_verbatim(self):
        for line in R.suite_table_markdown():
            self.assertIn(line, self.doc)

    def test_the_rule_text_is_complete(self):
        for phrase in ("10. **Cumulative regression rule (M5 and every later module).**",
                       "A checkpoint or module is not done until the older modules still pass.",
                       "a. Gate A (every checkpoint)", "b. Gate B (every checkpoint that changes a file outside the new module's own folder",
                       "c. Gate C (module completion only, on the final commit)",
                       "d. An older test that fails is fixed in the NEW code.",
                       "it may be extended, never loosened.",
                       "A new container gets its own compose profile, and the container-count expectations of older scripts are extended in the same decision.",
                       "e. The reviewer does not mark a checkpoint verified without the gate output pasted raw"):
            self.assertIn(phrase, self.doc)

    def test_every_gate_command_is_documented(self):
        for gate in R.GATES:
            self.assertIn(f"python scripts/regression.py --gate {gate}", self.doc)
        for option in ("--list", "--dry-run", "--only", "--no-build", "--report"):
            self.assertIn(option, self.doc)

    def test_the_refill_the_count_of_containers_and_the_timing_facts_are_documented(self):
        self.assertIn("UPDATE items SET stock = 1000;", self.doc)
        self.assertIn("UPDATE 50", self.doc)
        self.assertIn("35 s", self.doc)
        self.assertIn(f"`EXPECTED_CONTAINERS` ({R.EXPECTED_CONTAINERS})", self.doc)
        self.assertIn("18 min 40 s", self.doc)
        self.assertIn("68 minutes", self.doc)

    def test_the_last_green_runs_table_has_the_manual_row(self):
        self.assertIn("| 2026-10-31 | 449f66c | manual: M3 chaos full 135/135, M4 detector full 164/164, M4 unit 445 | PASS |", self.doc)

    def test_a_changed_count_in_the_doc_is_detected(self):
        changed = self.doc.replace("| `M4-unit` | M4 | A |", "| `M4-unit` | M4 | B |")
        self.assertNotEqual(changed, self.doc)
        rows = {row[0].strip("`"): row for row in table_rows(changed, r"`[A-Za-z0-9-]+`") if len(row) == 6}
        self.assertNotEqual(rows["M4-unit"], R.suite_row(R.SUITE_BY_ID["M4-unit"]))


if __name__ == "__main__":
    unittest.main()
