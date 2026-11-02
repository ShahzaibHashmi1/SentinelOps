#!/usr/bin/env python3
"""SentinelOps cumulative regression runner (step R1). Standard library only, so it runs on the Windows host.

    python scripts/regression.py --gate A              unit suites of every module, rules validation, allowed-imports scan
    python scripts/regression.py --gate B              gate A, then the live checks against the running stack (about 15 minutes of live suites)
    python scripts/regression.py --gate C              gates A and B, then the two full runs (module completion only)
    python scripts/regression.py --gate B --list       print the suites of the gate and the estimate, run nothing
    python scripts/regression.py --gate B --dry-run    print every command it would run, run nothing
    python scripts/regression.py --gate B --only M1-smoke M4-unit    run selected suites (the verdict then says "partial")
    python scripts/regression.py --gate B --no-build   skip the rebuild (repeated runs; the stack must already be up)
    python scripts/regression.py --gate B --report C:\\temp\\r.md      report path (default: regression_report.md in the repo root)

The rule (rule 10 of PROJECT_CONTEXT.md, full text and the ledger in docs/REGRESSION.md): a checkpoint or module is not done until the
older modules still pass. Gates are cumulative: B runs A and then B, C runs A, B and C. Inside a gate the unit suites come first and
the live checks after them.

Live suites (gates B and C) first need the stack: Docker answers, `docker compose --profile observability --profile detector up -d
--build --wait` (unless --no-build), 12 containers, both detector sources ok, the stock refilled, no chaos fault active, no open
incident. Between two live suites it waits again for "no open incident". A failed prerequisite stops the live suites; the unit suites
and the other independent suites still run and all results are shown.

Result rules (docs/REGRESSION.md): a suite passes only if its exit code is 0 AND its own verdict is found (OK for unittest; N/N checks
passed plus the verdict line for the check scripts). A count that differs from the ledger is a FAIL (older suites cannot be loosened,
the ledger changes only by a decision). An expected count of None prints [NEW] and passes: record the number in docs/REGRESSION.md.

Output: one [OK] / [FAIL] line per prerequisite and suite (a [WARN] or [NEW] line is not a failure), then a table, `N/M checks
passed` and `REGRESSION GATE <X> PASSED` or `FAILED`. The same table goes to the report file (git-ignored) with the last 60 lines of
every failed suite and one ready-to-paste ledger row. The runner changes no other file, and it never starts a fault by itself (faults
are injected only by the suites). In `finally` it runs `python chaos/chaos.py clear --all` and removes leftover load containers
(only if a live suite was started).

Exit code 0 = pass, 1 = at least one check failed, 2 = bad arguments, 130 = interrupted (cleaned up first).
"""
import argparse
import contextlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / "regression_report.md"
LEDGER_DOC = "docs/REGRESSION.md"

DETECTOR_STATUS_URL = "http://127.0.0.1:9100/internal/v1/status"

# The stack of every live check. A new module that adds containers extends this number by a decision (docs/REGRESSION.md, rule 10 d).
EXPECTED_CONTAINERS = 12

GATES = ("A", "B", "C")
KINDS = ("unittest", "checks", "exit", "python")
COUNT_POLICIES = ("strict", "info", "none")   # strict: a difference fails; info: printed only; none: no count is tracked

# Leftover load containers of the check scripts (scripts/chaos_check.py and scripts/detector_check.py); removed in `finally`.
LOAD_CONTAINERS = ("sentinelops-check-load", "sentinelops-detector-check-load")

STOCK_SQL = "UPDATE items SET stock = 1000;\n"
STOCK_REFILL_OUTPUT = "UPDATE 50"
NO_FAULTS_TEXT = "no active faults"   # chaos.py status prints this and exits 0; with active faults it prints a table and ALSO exits 0
STOCK_WAIT_S = 35                  # the inventory service caches stock reads for 30 s
SOURCES_OK_WAIT_S = 120
OPEN_INCIDENT_WAIT_S = 300
OPEN_INCIDENT_POLL_S = 10
SOURCES_POLL_S = 5

DOCKER_TIMEOUT_S = 60
COMPOSE_UP_TIMEOUT_S = 1800       # the first build of all images takes minutes
STOCK_TIMEOUT_S = 60
CHAOS_TIMEOUT_S = 900
GIT_TIMEOUT_S = 30
REPORT_TAIL_LINES = 60
ECHO_PREFIX = "    "

# ---------------------------------------------------------------------------
# The commands of the prerequisites and of the cleanup (module level, so that a static test can check them).
# "python" as the first argument is replaced by the running interpreter when the command is executed.
# ---------------------------------------------------------------------------

PY = "python"
LIVE_PROFILES = ("--profile", "observability", "--profile", "detector")
CMD_DOCKER_INFO = ("docker", "info")
CMD_COMPOSE_UP = ("docker", "compose", *LIVE_PROFILES, "up", "-d", "--build", "--wait")
CMD_COMPOSE_PS = ("docker", "compose", *LIVE_PROFILES, "ps", "-q")
CMD_STOCK_REFILL = ("docker", "compose", "exec", "-T", "postgres", "sh", "-c", "psql -U $POSTGRES_USER -d $POSTGRES_DB")
CMD_CHAOS_STATUS = (PY, "chaos/chaos.py", "status")
CMD_CHAOS_CLEAR = (PY, "chaos/chaos.py", "clear", "--all")
CMD_REMOVE_LOADS = tuple(("docker", "rm", "-f", name) for name in LOAD_CONTAINERS)
CMD_GIT_HEAD = ("git", "rev-parse", "--short", "HEAD")
CMD_GIT_STATUS = ("git", "status", "--short")

# Which compose profile a compose service needs; every compose call that names one of these carries the flag.
SERVICE_PROFILE = {"prometheus": "observability", "loki": "observability", "detector": "detector"}

# The allowed-imports scan of detector/app (the detector reads only Prometheus and Loki over HTTP).
FORBIDDEN_PATTERN = r"psycopg|redis|docker|subprocess|requests"
ALLOWED_PATTERN = r"urllib\.request|http\.server|threading"
IMPORTS_GLOB = "detector/app/*.py"


# ---------------------------------------------------------------------------
# The suites (the one list; docs/REGRESSION.md must show the same table, a test checks it)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Suite:
    id: str
    module: str
    gate: str                      # the gate that introduces it: A, B or C
    command: tuple                 # argument list (shell=False); () for an in-process suite
    kind: str                      # unittest, checks, exit, python
    expected: Optional[int]        # expected count; None = not recorded yet (the first green run records it)
    typical_s: Optional[int]       # typical duration in seconds; None = not measured
    timeout_s: int
    needs_stack: bool
    description: str
    verdict: str = ""              # closing line of a check script
    count_policy: str = "strict"
    env: tuple = ()                # ((NAME, value), ...) added to the environment of the command
    internal: str = ""             # name of an in-process check instead of a command


def _m1_unit(service, expected):
    return Suite(f"M1-unit-{service}", "M1", "A",
                 ("docker", "compose", "--profile", "test", "run", "--rm", "-e", f"SERVICE={service}", "tests"),
                 "exit", expected, None, 1200, False,
                 f"M1 service tests of {service} in a python:3.12 container (pytest); pass by exit code, the count is informational",
                 count_policy="info")


SUITES = (
    # ---- gate A: unit suites, rules validation, allowed imports (Docker is needed only for the M1 service tests)
    _m1_unit("gateway", 11),
    _m1_unit("orders", 10),
    _m1_unit("payments", 10),
    _m1_unit("inventory", 11),
    Suite("M3-unit", "M3", "A", (PY, "-m", "unittest", "discover", "-s", "chaos/tests"), "unittest", 243, None, 900, False,
          "chaos unit tests (test_chaos_unit.py and test_chaos_check.py)"),
    Suite("M4-unit", "M4", "A", (PY, "-m", "unittest", "discover", "-s", "detector/tests"), "unittest", 445, None, 900, False,
          "detector unit tests, including the docs consistency test of docs/DETECTOR.md"),
    Suite("R1-unit", "R1", "A", (PY, "-m", "unittest", "discover", "-s", "regression/tests"), "unittest", 180, None, 600, False,
          "unit tests of this runner and of docs/REGRESSION.md"),
    Suite("M4-rules", "M4", "A", (PY, "-m", "app.rules", "detector/rules/rules.json"), "python", 9, None, 120, False,
          "rules.json validation: `9 rules valid` and `pending provenance: none` (exit code 0 = valid)",
          env=(("PYTHONPATH", "detector"),)),
    Suite("M4-imports", "M4", "A", (), "python", None, None, 60, False,
          f"in-process scan of {IMPORTS_GLOB}: no match for {FORBIDDEN_PATTERN} (case-insensitive), "
          "and the pattern urllib.request|http.server|threading matches at least once (positive control)",
          count_policy="none", internal="imports"),
    # ---- gate B: live checks against the running stack (the new module running)
    Suite("M1-smoke", "M1", "B", (PY, "scripts/smoke_test.py"), "checks", 22, None, 600, True,
          "smoke test of the target application", verdict="SMOKE TEST PASSED"),
    Suite("M2-observability", "M2", "B", (PY, "scripts/observability_check.py"), "checks", 29, None, 1200, True,
          "observability check (it runs the smoke test inside)", verdict="OBSERVABILITY CHECK PASSED"),
    Suite("M3-chaos-quick", "M3", "B", (PY, "scripts/chaos_check.py", "--only", "F10", "--quick"), "checks", 36, 240, 1800, True,
          "chaos check, fault F10 only, quick mode (it runs the smoke test inside)", verdict="CHAOS CHECK PASSED"),
    Suite("M4-detector-quick", "M4", "B",
          (PY, "scripts/detector_check.py", "--quick", "--only", "F10", "--skip-nodata", "--skip-persistence"),
          "checks", 41, 600, 3600, True,
          "detector check, fault F10 only, quick mode, without the no-data and persistence steps (41 checks at the first green run)",
          verdict="DETECTOR CHECK PASSED"),
    # ---- gate C: module completion (the full runs)
    Suite("M3-chaos-full", "M3", "C", (PY, "scripts/chaos_check.py"), "checks", 135, 1120, 5400, True,
          "chaos check, all 8 faults (18 min 40 s measured)", verdict="CHAOS CHECK PASSED"),
    Suite("M4-detector-full", "M4", "C", (PY, "scripts/detector_check.py"), "checks", 164, 4080, 10800, True,
          "detector check, all 8 faults, no-data step and persistence step (68 minutes measured)", verdict="DETECTOR CHECK PASSED"),
)

SUITE_BY_ID = {suite.id: suite for suite in SUITES}


def gate_suites(gate):
    """The suites of a gate, cumulative (C = A + B + C), in the order of SUITES: unit suites first, live checks after them."""
    return [suite for suite in SUITES if GATES.index(suite.gate) <= GATES.index(gate)]


def all_commands():
    """Every command the runner can start, as (label, command tuple); used by the static profile-flag test."""
    rows = [(suite.id, suite.command) for suite in SUITES if suite.command]
    rows += [("docker info", CMD_DOCKER_INFO), ("compose up", CMD_COMPOSE_UP), ("compose ps", CMD_COMPOSE_PS),
             ("stock refill", CMD_STOCK_REFILL), ("chaos status", CMD_CHAOS_STATUS), ("chaos clear", CMD_CHAOS_CLEAR)]
    rows += [(f"remove {cmd[-1]}", cmd) for cmd in CMD_REMOVE_LOADS]
    return rows


# ---------------------------------------------------------------------------
# Pure helpers (unit tested in regression/tests/test_regression.py; no Docker, no network)
# ---------------------------------------------------------------------------

def display_arg(arg):
    return f'"{arg}"' if (not arg or re.search(r'[\s"$]', arg)) else arg


def command_text(suite):
    """The command as written in docs/REGRESSION.md."""
    if suite.internal:
        return f"in process: scan {IMPORTS_GLOB}"
    prefix = "".join(f"{name}={value} " for name, value in suite.env)
    return prefix + " ".join(display_arg(arg) for arg in suite.command)


def format_duration(seconds):
    if seconds is None:
        return "not measured"
    minutes, rest = divmod(int(seconds), 60)
    if minutes == 0:
        return f"{rest} s"
    return f"{minutes} min" + (f" {rest} s" if rest else "")


def expected_text(suite):
    if suite.count_policy == "none":
        return "-"
    return "None" if suite.expected is None else str(suite.expected)


def suite_row(suite):
    """The cells of the suite table of docs/REGRESSION.md."""
    return [f"`{suite.id}`", suite.module, suite.gate, f"`{command_text(suite)}`", expected_text(suite),
            format_duration(suite.typical_s)]


def suite_table_markdown():
    lines = ["| Id | Module | Gate | Command | Expected count | Typical duration |", "|---|---|---|---|---|---|"]
    lines += ["| " + " | ".join(suite_row(suite)) + " |" for suite in SUITES]
    return lines


def estimate(suites):
    """(seconds, ids without a measured duration): the sum of the measured typical durations; waits and prerequisites are not in it."""
    known = sum(s.typical_s for s in suites if s.typical_s is not None)
    unknown = [s.id for s in suites if s.typical_s is None]
    return known, unknown


def estimate_text(suites):
    seconds, unknown = estimate(suites)
    if seconds == 0:
        text = "no measured duration in the ledger for these suites"
    else:
        minutes = int(math.ceil(seconds / 60.0))
        text = f"at least about {minutes} minutes (the sum of the measured typical durations of the ledger)"
    if unknown:
        text += f"; not measured, so not included: {', '.join(unknown)}; prerequisites and waits are not included either"
    return text


def select_suites(gate, only=None):
    """(suites in gate order, unknown ids). `only` names suite ids (case-insensitive) of the cumulative gate."""
    available = gate_suites(gate)
    if not only:
        return available, []
    by_lower = {suite.id.lower(): suite for suite in available}
    wanted, unknown = set(), []
    for name in only:
        suite = by_lower.get(name.lower())
        if suite is None:
            unknown.append(name)
        else:
            wanted.add(suite.id)
    return [suite for suite in available if suite.id in wanted], unknown


@dataclass
class UnittestResult:
    ran: Optional[int] = None
    verdict: Optional[str] = None     # "OK", "OK (skipped=1)" or "FAILED (failures=1)"
    ok: bool = False
    skipped: int = 0


def parse_unittest(lines):
    """`Ran N tests in X s`, then `OK`, `OK (skipped=K)` or `FAILED (failures=a, errors=b)`; the last of each wins."""
    result = UnittestResult()
    for line in lines:
        text = line.strip()
        ran = re.match(r"^Ran (\d+) tests? in ", text)
        if ran:
            result.ran = int(ran.group(1))
        verdict = re.match(r"^(OK|FAILED)\b(.*)$", text)
        if verdict and (verdict.group(1) == "FAILED" or verdict.group(2).strip() == "" or verdict.group(2).strip().startswith("(")):
            result.verdict = text
    if result.verdict:
        result.ok = result.verdict.startswith("OK")
        skipped = re.search(r"skipped=(\d+)", result.verdict)
        result.skipped = int(skipped.group(1)) if skipped else 0
    return result


@dataclass
class ChecksResult:
    passed: Optional[int] = None
    total: Optional[int] = None
    verdict_found: bool = False
    fail_lines: tuple = ()
    failed_names: Optional[str] = None


def parse_checks(lines, verdict):
    """The check scripts print `[OK] ...` / `[FAIL] ...`, `N/M checks passed`, then the verdict line (`FAILED: <names>` on failure).
    observability_check.py and chaos_check.py contain the smoke test, so the LAST `N/M checks passed` line is the one that counts."""
    result = ChecksResult()
    fails = []
    for line in lines:
        text = line.strip()
        counted = re.match(r"^(\d+)/(\d+) checks passed\b", text)
        if counted:
            result.passed, result.total = int(counted.group(1)), int(counted.group(2))
        if text == verdict:
            result.verdict_found = True
        if text.startswith("[FAIL]"):
            fails.append(text)
        if text.startswith("FAILED:"):
            result.failed_names = text[len("FAILED:"):].strip()
    result.fail_lines = tuple(fails)
    return result


def parse_pytest_count(lines):
    """`10 passed in 0.67s` (also `1 failed, 9 passed`): the passed count, or None if there is no such line."""
    found = None
    for line in lines:
        match = re.search(r"\b(\d+) passed\b", line)
        if match:
            found = int(match.group(1))
    return found


def parse_rules_valid(lines):
    """(number of valid rules or None, `pending provenance: none` present)."""
    count, pending_none = None, False
    for line in lines:
        match = re.search(r"\b(\d+) rules valid\b", line)
        if match:
            count = int(match.group(1))
        if line.strip() == "pending provenance: none":
            pending_none = True
    return count, pending_none


def scan_imports(sources):
    """sources: [(file name, text)]. Returns (forbidden hits, positive-control hit count, files scanned). Case-insensitive, like Select-String."""
    forbidden = re.compile(FORBIDDEN_PATTERN, re.IGNORECASE)
    allowed = re.compile(ALLOWED_PATTERN, re.IGNORECASE)
    hits, positive = [], 0
    for name, text in sources:
        for number, line in enumerate(text.splitlines(), 1):
            if forbidden.search(line):
                hits.append(f"{name}:{number}: {line.strip()}")
            if allowed.search(line):
                positive += 1
    return hits, positive, len(sources)


@dataclass
class Evaluation:
    ok: bool
    found: Optional[int]
    detail: str
    notes: tuple = ()          # extra lines: "[NEW] ...", "[WARN] ..."


def judge_count(suite, found):
    """Apply the count rules. Returns (ok, detail, notes)."""
    policy = suite.count_policy
    if policy == "none":
        return True, "", ()
    if found is None:
        if policy == "strict":
            return False, "the count was not found in the output" + ("" if suite.expected is None else f"; expected {suite.expected}"), ()
        return True, "count unknown", ()
    if suite.expected is None:
        return True, f"count {found}", (f"[NEW] record this count in {LEDGER_DOC}: {found}  ({suite.id})",)
    if found == suite.expected:
        return True, "", ()
    message = f"count differs from the ledger: expected {suite.expected}, found {found}; change the ledger by a decision"
    if policy == "strict":
        return False, message, ()
    return True, message, (f"[WARN] {suite.id}: {message}",)


def _failure_reason(code, lines):
    last = next((line.strip() for line in reversed(lines) if line.strip()), "")
    if code == 124:
        return last or "timed out"
    if code == 127:
        return last or "command not found"
    return f"exit code {code}"


def evaluate(suite, code, lines, sources=None):
    """Judge one finished suite from its exit code and its output lines (pure function)."""
    if suite.internal == "imports":
        hits, positive, scanned = scan_imports(sources or [])
        if scanned == 0:
            return Evaluation(False, None, f"no file found by {IMPORTS_GLOB}")
        if hits:
            return Evaluation(False, None, f"{len(hits)} forbidden match(es): " + " | ".join(hits[:3]))
        if positive == 0:
            return Evaluation(False, None, "positive control failed: urllib.request|http.server|threading matched nothing")
        return Evaluation(True, None, f"{scanned} files, no forbidden match, positive control {positive} line(s)")

    if code in (124, 127):
        return Evaluation(False, None, _failure_reason(code, lines))

    if suite.kind == "unittest":
        parsed = parse_unittest(lines)
        if code != 0:
            return Evaluation(False, parsed.ran, f"exit code {code}; " + (parsed.verdict or "no unittest verdict found"))
        if not parsed.ok:
            return Evaluation(False, parsed.ran, "exit code 0 but no OK verdict found" + (f" ({parsed.verdict})" if parsed.verdict else ""))
        ok, detail, notes = judge_count(suite, parsed.ran)
        notes = list(notes)
        if parsed.skipped:
            notes.append(f"[WARN] {suite.id}: {parsed.skipped} test(s) skipped ({parsed.verdict})")
        shown = f"{parsed.ran} tests" + (f", {detail}" if detail else "")
        return Evaluation(ok, parsed.ran, shown, tuple(notes))

    if suite.kind == "checks":
        parsed = parse_checks(lines, suite.verdict)
        if code != 0:
            names = f"; FAILED: {parsed.failed_names}" if parsed.failed_names else ""
            return Evaluation(False, parsed.total, f"exit code {code}{names}")
        problems = []
        if parsed.total is None:
            problems.append("no `N/M checks passed` line")
        elif parsed.passed != parsed.total:
            problems.append(f"{parsed.passed}/{parsed.total} checks passed")
        elif parsed.total == 0:
            problems.append("0 checks ran")
        if not parsed.verdict_found:
            problems.append(f"verdict line `{suite.verdict}` not found")
        if parsed.fail_lines:
            problems.append(f"{len(parsed.fail_lines)} [FAIL] line(s)")
        if problems:
            return Evaluation(False, parsed.total, "exit code 0 but " + "; ".join(problems))
        ok, detail, notes = judge_count(suite, parsed.total)
        shown = f"{parsed.passed}/{parsed.total} checks" + (f", {detail}" if detail else "")
        return Evaluation(ok, parsed.total, shown, notes)

    if suite.kind == "exit":
        if code != 0:
            return Evaluation(False, parse_pytest_count(lines), f"exit code {code}")
        found = parse_pytest_count(lines)
        ok, detail, notes = judge_count(suite, found)
        shown = (f"{found} passed" if found is not None else "count unknown") + (f", {detail}" if detail and found is not None else "")
        return Evaluation(ok, found, shown, notes)

    if suite.kind == "python":
        count, pending_none = parse_rules_valid(lines)
        if code != 0:
            return Evaluation(False, count, f"exit code {code}")
        if count is None:
            return Evaluation(False, None, "`N rules valid` not found")
        if not pending_none:
            return Evaluation(False, count, "`pending provenance: none` not found")
        ok, detail, notes = judge_count(suite, count)
        return Evaluation(ok, count, f"{count} rules valid" + (f", {detail}" if detail else ""), notes)

    raise ValueError(f"unknown suite kind {suite.kind!r}")


def format_table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip(),
             "  ".join("-" * w for w in widths)]
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return lines


def shorten(text, limit=100):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def tail_lines(lines, limit=REPORT_TAIL_LINES):
    kept = [line for line in lines if line.strip()]
    return kept[-limit:]


def ledger_row(date, commit, gate, passed, counts):
    return f"| {date} | {commit} | {gate} | {'PASS' if passed else 'FAIL'} | {counts} |"


def verdict_line(gate, passed, partial_of=None):
    word = "PASSED" if passed else "FAILED"
    suffix = f" (partial: {partial_of[0]} of {partial_of[1]} suites)" if partial_of else ""
    return f"REGRESSION GATE {gate} {word}{suffix}"


# ---------------------------------------------------------------------------
# The host: everything that touches the machine (processes, HTTP, files, clock). Tests replace it.
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    @property
    def text(self):
        return self.stdout + self.stderr


def _group_flags():
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def _resolve(args):
    return [sys.executable] + list(args[1:]) if args and args[0] == PY else list(args)


class Host:
    """The real machine. Children get their own process group, so that a Ctrl+C typed in the console reaches only this
    script, which then cleans up (same as scripts/detector_check.py)."""

    def say(self, text=""):
        try:
            print(text, flush=True)
        except UnicodeEncodeError:      # a console or a redirect that cannot show a character of a child's output
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            print(str(text).encode(encoding, "replace").decode(encoding), flush=True)

    def run(self, args, timeout, env=None, input_text=None):
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
        try:
            done = subprocess.run(_resolve(args), cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, shell=False, env=child_env, input=input_text,
                                  stdin=None if input_text is not None else subprocess.DEVNULL, **_group_flags())
        except FileNotFoundError:
            return Result(127, "", f"command not found: {args[0]}")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout} s: {' '.join(args[:5])}")
        return Result(done.returncode, done.stdout or "", done.stderr or "")

    def stream(self, args, timeout, on_line, env=None):
        """Run a command, hand every output line to on_line(text) AS IT ARRIVES. Returns (returncode, lines).
        After `timeout` seconds the process is killed (returncode 124); a missing program gives 127."""
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", **(env or {})}
        try:
            child = subprocess.Popen(_resolve(args), cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     shell=False, env=child_env, **_group_flags())
        except FileNotFoundError:
            return 127, [f"command not found: {args[0]}"]
        lines = []

        def reader():
            for raw in child.stdout:
                text = raw.rstrip("\r\n")
                lines.append(text)
                on_line(text)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            code = 124
            lines.append(f"timed out after {timeout} s")
        finally:
            if child.poll() is None:
                child.kill()
            thread.join(timeout=5)
            child.stdout.close()
        return code, lines

    def detector_status(self):
        """The JSON of GET /internal/v1/status, or None if the detector does not answer."""
        try:
            with urllib.request.urlopen(DETECTOR_STATUS_URL, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            exc.close()
            return None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def utcnow(self):
        return datetime.now(timezone.utc)

    def detector_sources(self):
        paths = sorted((ROOT / "detector" / "app").glob("*.py"))
        return [(f"detector/app/{path.name}", path.read_text(encoding="utf-8", errors="replace")) for path in paths]

    def write_report(self, path, text):
        path = Path(path)
        if not path.is_absolute():
            path = ROOT / path
        path.write_text(text, encoding="utf-8", newline="\n")
        return str(path)

    @contextlib.contextmanager
    def protect(self):
        """Ctrl+C is ignored while the cleanup runs."""
        try:
            old = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except ValueError:          # not the main thread
            yield
            return
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, old)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@dataclass
class SuiteResult:
    suite: Suite
    ok: bool
    found: Optional[int]
    seconds: float
    detail: str
    tail: tuple = ()


class Regression:
    def __init__(self, host, gate, only=None, no_build=False, report_path=None):
        self.host = host
        self.gate = gate
        self.only = list(only) if only else None
        self.no_build = no_build
        self.report_path = report_path if report_path is not None else str(DEFAULT_REPORT)
        self.selected, _ = select_suites(gate, self.only)
        self.available = gate_suites(gate)
        self.checks = []              # (name, ok, detail) of every [OK] / [FAIL] line
        self.warnings = []
        self.notes = []               # [NEW] lines
        self.rows = []                # SuiteResult in run order
        self.live_started = False
        self.prerequisites_done = False
        self.prerequisites_ok = False
        self.interrupted = False
        self.started_at = None
        self.started_mono = None
        self.total_s = 0.0

    # ------------------------------------------------------------------ output
    def say(self, text=""):
        self.host.say(text)

    def record(self, name, ok, detail=""):
        self.checks.append((name, ok, detail))
        self.say(f"[{'OK' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
        return ok

    def warn(self, text):
        self.warnings.append(text)
        self.say(text if text.startswith("[WARN]") else f"[WARN] {text}")

    def echo(self, text):
        self.say(ECHO_PREFIX + text)

    # ------------------------------------------------------------------ banner
    def banner(self):
        partial = "" if len(self.selected) == len(self.available) else f" | partial: {len(self.selected)} of {len(self.available)} suites"
        cumulative = ", ".join(g for g in GATES if GATES.index(g) <= GATES.index(self.gate))
        self.say(f"SentinelOps regression, gate {self.gate} (cumulative: {cumulative}): {len(self.selected)} suites{partial}")
        self.say("suites: " + ", ".join(s.id for s in self.selected))
        self.say("estimate: " + estimate_text(self.selected))
        self.say("rule 10: an older test that fails is fixed in the NEW code; the expected counts are in " + LEDGER_DOC)

    # ------------------------------------------------------------------ prerequisites
    def wait_until(self, predicate, timeout_s, interval_s):
        """(value, seconds waited); predicate runs at least once and once more at the deadline. Uses the host clock."""
        start = self.host.monotonic()
        while True:
            value = predicate()
            elapsed = self.host.monotonic() - start
            if value or elapsed >= timeout_s:
                return value, elapsed
            self.host.sleep(min(interval_s, max(0.0, timeout_s - elapsed)))

    def sources_state(self):
        status = self.host.detector_status()
        if not isinstance(status, dict):
            return None
        sources = status.get("sources") or {}
        return {name: (sources.get(name) or {}).get("state") for name in ("prometheus", "loki")}

    def open_incident(self):
        """(known, incident id or None)."""
        status = self.host.detector_status()
        if not isinstance(status, dict):
            return False, None
        return True, status.get("open_incident_id")

    def wait_no_open_incident(self, label):
        def quiet():
            known, incident = self.open_incident()
            return known and incident is None

        ok, waited = self.wait_until(quiet, OPEN_INCIDENT_WAIT_S, OPEN_INCIDENT_POLL_S)
        if ok:
            return self.record(f"no open incident ({label})", True, f"after {waited:.0f} s")
        known, incident = self.open_incident()
        why = f"incident {incident} is still open" if known else "the detector API does not answer"
        return self.record(f"no open incident ({label})", False, f"{why} after {OPEN_INCIDENT_WAIT_S} s")

    @staticmethod
    def no_fault_active(result):
        """chaos.py status exits 0 in every case (also with active or stale faults), so the exit code proves nothing: the text decides."""
        return result.returncode == 0 and NO_FAULTS_TEXT in result.stdout.lower()

    def prerequisites(self):
        """The conditions of every live suite, each an [OK] / [FAIL] line. Returns True if all hold."""
        self.say("\n== prerequisites of the live suites ==")
        result = self.host.run(CMD_DOCKER_INFO, DOCKER_TIMEOUT_S)
        if not self.record("Docker answers (docker info)", result.returncode == 0, "" if result.returncode == 0 else shorten(result.text)):
            return False

        if self.no_build:
            self.say("rebuild skipped (--no-build): the stack must already be up")
        else:
            self.say("starting the stack (the first build takes minutes): " + " ".join(CMD_COMPOSE_UP))
            code, lines = self.host.stream(CMD_COMPOSE_UP, COMPOSE_UP_TIMEOUT_S, self.echo)
            if not self.record("stack rebuilt and started (docker compose up --build --wait)", code == 0,
                               "" if code == 0 else _failure_reason(code, lines)):
                return False

        result = self.host.run(CMD_COMPOSE_PS, DOCKER_TIMEOUT_S)
        count = len([line for line in result.stdout.splitlines() if line.strip()])
        count_ok = result.returncode == 0 and count == EXPECTED_CONTAINERS
        detail = f"{count} listed" if result.returncode == 0 else shorten(result.text)
        if count > EXPECTED_CONTAINERS:
            detail += ("; leftover load containers? docker rm -f " + " ".join(LOAD_CONTAINERS))
        if not self.record(f"{EXPECTED_CONTAINERS} containers (docker compose ps -q)", count_ok, detail):
            return False

        def both_ok():
            states = self.sources_state()
            return states is not None and all(state == "ok" for state in states.values()) and states

        states, waited = self.wait_until(both_ok, SOURCES_OK_WAIT_S, SOURCES_POLL_S)
        if not self.record("detector sources prometheus and loki are ok", bool(states),
                           f"after {waited:.0f} s" if states else f"states after {SOURCES_OK_WAIT_S} s: {self.sources_state()}"):
            return False

        result = self.host.run(CMD_STOCK_REFILL, STOCK_TIMEOUT_S, input_text=STOCK_SQL)
        refilled = result.returncode == 0 and STOCK_REFILL_OUTPUT in result.stdout
        if not self.record("stock refilled (UPDATE items SET stock = 1000)", refilled,
                           f"printed {STOCK_REFILL_OUTPUT}" if refilled else shorten(result.text)):
            return False
        self.say(f"waiting {STOCK_WAIT_S} s (the inventory service caches stock reads for 30 s) ...")
        self.host.sleep(STOCK_WAIT_S)

        result = self.host.run(CMD_CHAOS_STATUS, CHAOS_TIMEOUT_S)
        if not self.no_fault_active(result):
            self.warn("a chaos fault is active: running `python chaos/chaos.py clear --all` once")
            self.live_started = True        # something may have been cleared; the cleanup runs in any case
            cleared = self.host.run(CMD_CHAOS_CLEAR, CHAOS_TIMEOUT_S)
            result = self.host.run(CMD_CHAOS_STATUS, CHAOS_TIMEOUT_S)
            detail = f"clear --all exit code {cleared.returncode}; status exit code {result.returncode}; " + shorten(result.stdout.strip() or result.text)
        else:
            detail = ""
        if not self.record(f"no chaos fault is active (chaos.py status prints `{NO_FAULTS_TEXT}`)", self.no_fault_active(result), detail):
            return False

        return self.wait_no_open_incident("start")

    # ------------------------------------------------------------------ suites
    def run_suite(self, suite):
        self.say(f"\n== {suite.id}: {suite.description} ==")
        self.say("$ " + command_text(suite))
        started = self.host.monotonic()
        sources = None
        if suite.internal:
            code, lines = 0, []
            sources = self.host.detector_sources()
        else:
            code, lines = self.host.stream(suite.command, suite.timeout_s, self.echo, env=dict(suite.env) or None)
        seconds = self.host.monotonic() - started
        outcome = evaluate(suite, code, lines, sources)
        for note in outcome.notes:
            if note.startswith("[WARN]"):
                self.warn(note)
            else:
                self.notes.append(note)
                self.say(note)
        detail = (outcome.detail + ", " if outcome.detail else "") + f"{seconds:.1f} s"
        self.record(suite.id, outcome.ok, detail)
        if not outcome.ok and any("insufficient_stock" in line for line in lines):
            self.say("hint: the stock is exhausted; refill it (docs/REGRESSION.md, \"Stock refill\") and run this suite again with "
                     f"--gate {self.gate} --only {suite.id} --no-build")
        tail = tuple(tail_lines(lines)) if not outcome.ok else ()
        row = SuiteResult(suite, outcome.ok, outcome.found, seconds, outcome.detail, tail)
        self.rows.append(row)
        return row

    def not_run(self, suite, reason):
        self.record(suite.id, False, f"not run: {reason}")
        self.rows.append(SuiteResult(suite, False, None, 0.0, f"not run: {reason}"))

    def execute(self):
        live_blocked = None
        first_live = True
        for suite in self.selected:
            if suite.needs_stack:
                if live_blocked:
                    self.not_run(suite, live_blocked)
                    continue
                if first_live:
                    first_live = False
                    self.prerequisites_done = True
                    self.prerequisites_ok = self.prerequisites()
                    if not self.prerequisites_ok:
                        live_blocked = "a prerequisite failed"
                        self.not_run(suite, live_blocked)
                        continue
                else:
                    if not self.wait_no_open_incident(f"before {suite.id}"):
                        live_blocked = "an incident stayed open"
                        self.not_run(suite, live_blocked)
                        continue
                self.live_started = True
            self.run_suite(suite)

    # ------------------------------------------------------------------ cleanup
    def cleanup(self):
        """In `finally`: clear every fault and remove leftover load containers, but only if a live suite was started."""
        if not self.live_started:
            return
        with self.host.protect():
            self.say("\ncleaning up: python chaos/chaos.py clear --all, removing leftover load containers ...")
            try:
                result = self.host.run(CMD_CHAOS_CLEAR, CHAOS_TIMEOUT_S)
                lines = [line for line in result.stdout.splitlines() if line.strip()]
                self.say(f"  clear --all: exit code {result.returncode}" + (f", last line: {lines[-1]}" if lines else ""))
                if result.returncode != 0:
                    self.warn("chaos.py clear --all did not exit with 0; check: python chaos/chaos.py status")
                for command in CMD_REMOVE_LOADS:
                    result = self.host.run(command, DOCKER_TIMEOUT_S)
                    if result.returncode == 0:
                        self.say(f"  removed {command[-1]}")
                    elif "No such container" in result.text:
                        self.say(f"  {command[-1]}: not present")
                    else:
                        self.warn(f"could not remove {command[-1]}: {shorten(result.text)}")
            except Exception as exc:   # the cleanup must never hide the result
                self.say(f"  cleanup error: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ summary and report
    @property
    def failed_names(self):
        return [name for name, ok, _ in self.checks if not ok]

    @property
    def passed(self):
        return (not self.interrupted) and bool(self.checks) and not self.failed_names and len(self.rows) == len(self.selected)

    def partial_of(self):
        return None if len(self.selected) == len(self.available) else (len(self.selected), len(self.available))

    def counts_text(self):
        parts = []
        for row in self.rows:
            if row.ok and row.found is not None:
                parts.append(f"{row.suite.id} {row.found}")
        return ", ".join(parts) if parts else "-"

    def table_rows(self):
        rows = []
        for row in self.rows:
            rows.append([row.suite.id, expected_text(row.suite), "-" if row.found is None else row.found,
                         f"{row.seconds:.1f}", "PASS" if row.ok else "FAIL"])
        return rows

    def summary(self):
        self.say("\nResults:")
        for line in format_table(["Suite", "Expected", "Found", "Seconds", "Result"], self.table_rows()):
            self.say("  " + line)
        failed = self.failed_names
        total = len(self.checks)
        self.say(f"\n{total - len(failed)}/{total} checks passed" + (f"   ({len(self.warnings)} warning(s))" if self.warnings else ""))
        if self.notes:
            self.say(f"{len(self.notes)} count(s) to record in {LEDGER_DOC} (see the [NEW] lines above)")
        if self.interrupted:
            self.say("REGRESSION GATE " + self.gate + " INTERRUPTED")
            return 130
        if failed:
            self.say("FAILED: " + "; ".join(failed))
        passed = self.passed
        self.say(verdict_line(self.gate, passed, self.partial_of()))
        return 0 if passed else 1

    def git_info(self):
        head = self.host.run(CMD_GIT_HEAD, GIT_TIMEOUT_S)
        status = self.host.run(CMD_GIT_STATUS, GIT_TIMEOUT_S)
        commit = head.stdout.strip() if head.returncode == 0 and head.stdout.strip() else "unknown"
        if status.returncode != 0:
            return commit, "unknown (git status failed)", []
        lines = [line for line in status.stdout.splitlines() if line.strip()]
        tracked = [line for line in lines if not line.startswith("??")]
        return commit, (f"yes ({len(tracked)})" if tracked else "no"), tracked

    def report_text(self):
        stamp = self.started_at or self.host.utcnow()
        commit, modified, tracked = self.git_info()
        partial = self.partial_of()
        if self.interrupted:
            verdict = "INTERRUPTED"
        else:
            verdict = verdict_line(self.gate, self.passed, partial)
        lines = ["# SentinelOps regression report", "",
                 f"- Timestamp (UTC): {stamp.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"- Commit: {commit}",
                 f"- Tracked files modified: {modified}"]
        lines += [f"  - `{line}`" for line in tracked[:20]]
        lines += [f"- Gate: {self.gate}" + (f" (partial: {partial[0]} of {partial[1]} suites)" if partial else ""),
                  f"- Verdict: {verdict}",
                  f"- Total time: {self.total_s:.0f} s ({format_duration(self.total_s)})", "",
                  "| Suite | Command | Expected | Found | Seconds | Result |", "|---|---|---|---|---|---|"]
        for row in self.rows:
            lines.append(f"| {row.suite.id} | `{command_text(row.suite)}` | {expected_text(row.suite)} | "
                         f"{'-' if row.found is None else row.found} | {row.seconds:.1f} | {'PASS' if row.ok else 'FAIL'} |")
        failed_checks = [(name, detail) for name, ok, detail in self.checks if not ok]
        if failed_checks:
            lines += ["", "## Failed checks", ""] + [f"- {name}: {detail}" for name, detail in failed_checks]
        for row in self.rows:
            if not row.ok and row.tail:
                lines += ["", f"## Last {REPORT_TAIL_LINES} lines of {row.suite.id}", "", "```text"] + list(row.tail) + ["```"]
        if self.notes or self.warnings:
            lines += ["", "## Notes", ""] + [f"- {text}" for text in self.notes + self.warnings]
        counts = self.counts_text()
        lines += ["", "## Ledger row", "",
                  "| Date | Commit | Gate | Result | Counts |", "|---|---|---|---|---|",
                  ledger_row(stamp.strftime("%Y-%m-%d"), commit, self.gate + (" (partial)" if partial else ""),
                             self.passed, counts), ""]
        return "\n".join(lines)

    def write_report(self):
        try:
            where = self.host.write_report(self.report_path, self.report_text())
            self.say(f"report written: {where}")
        except OSError as exc:
            self.say(f"could not write the report: {exc}")

    # ------------------------------------------------------------------ run
    def run(self):
        self.started_at = self.host.utcnow()
        self.started_mono = self.host.monotonic()
        code = 1
        try:
            self.banner()
            self.execute()
        except KeyboardInterrupt:
            self.interrupted = True
            self.say("\ninterrupted")
        except Exception as exc:        # a bug or an unexpected error: still clean up and report
            self.record("runner error", False, f"{type(exc).__name__}: {exc}")
        finally:
            self.cleanup()
            self.total_s = self.host.monotonic() - self.started_mono
            code = self.summary()
            self.write_report()
        return code


# ---------------------------------------------------------------------------
# --list and --dry-run
# ---------------------------------------------------------------------------

def print_list(host, gate, selected):
    host.say(f"Gate {gate} (cumulative): {len(selected)} suites")
    rows = [[s.id, s.module, s.gate, "yes" if s.needs_stack else "no", expected_text(s), format_duration(s.typical_s)] for s in selected]
    for line in format_table(["Suite", "Module", "Gate", "Needs stack", "Expected", "Typical"], rows):
        host.say("  " + line)
    host.say("estimate: " + estimate_text(selected))


def print_dry_run(host, selected, no_build):
    host.say("dry run: nothing is executed")
    live = [s for s in selected if s.needs_stack]
    for suite in selected:
        if suite.needs_stack and suite is live[0]:
            host.say("-- prerequisites (before the first live suite):")
            host.say("prerequisite: " + " ".join(display_arg(a) for a in CMD_DOCKER_INFO))
            if no_build:
                host.say("prerequisite: (docker compose up skipped: --no-build)")
            else:
                host.say("prerequisite: " + " ".join(display_arg(a) for a in CMD_COMPOSE_UP))
            host.say("prerequisite: " + " ".join(display_arg(a) for a in CMD_COMPOSE_PS) + f"   (expect {EXPECTED_CONTAINERS} lines)")
            host.say(f"prerequisite: wait up to {SOURCES_OK_WAIT_S} s until {DETECTOR_STATUS_URL} shows prometheus and loki ok")
            host.say("prerequisite: " + " ".join(display_arg(a) for a in CMD_STOCK_REFILL) + f"   (stdin: {STOCK_SQL.strip()}  expect: {STOCK_REFILL_OUTPUT})")
            host.say(f"prerequisite: wait {STOCK_WAIT_S} s")
            host.say("prerequisite: " + " ".join(display_arg(a) for a in CMD_CHAOS_STATUS)
                     + "   (if a fault is active: " + " ".join(display_arg(a) for a in CMD_CHAOS_CLEAR) + " once, then [WARN])")
            host.say(f"prerequisite: wait up to {OPEN_INCIDENT_WAIT_S} s (every {OPEN_INCIDENT_POLL_S} s) until open_incident_id is null")
        elif suite.needs_stack:
            host.say(f"-- before {suite.id}: wait up to {OPEN_INCIDENT_WAIT_S} s until open_incident_id is null")
        host.say(f"suite {suite.id}: " + command_text(suite))
    if live:
        host.say("cleanup (finally, because live suites run): " + " ".join(CMD_CHAOS_CLEAR))
        for command in CMD_REMOVE_LOADS:
            host.say("cleanup: " + " ".join(command))
    host.say("report: " + str(DEFAULT_REPORT) + " (or --report PATH)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="SentinelOps cumulative regression runner (rule 10)")
    parser.add_argument("--gate", required=True, type=str.upper, choices=GATES,
                        help="A: unit suites; B: A + live checks; C: A + B + the full runs (cumulative)")
    parser.add_argument("--list", action="store_true", help="print the suites of the gate and the estimate, run nothing")
    parser.add_argument("--dry-run", action="store_true", help="print every command it would run, run nothing")
    parser.add_argument("--only", nargs="+", metavar="SUITE", help="run only these suite ids (the verdict then says partial)")
    parser.add_argument("--no-build", action="store_true", help="skip the rebuild of the stack (repeated runs)")
    parser.add_argument("--report", metavar="PATH", default=str(DEFAULT_REPORT), help="report file (default: regression_report.md in the repo root)")
    return parser


def main(argv=None, host=None):
    host = host or Host()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    selected, unknown = select_suites(args.gate, args.only)
    if unknown:
        print(f"unknown or not in gate {args.gate}: {', '.join(unknown)}. Suites of gate {args.gate}: "
              f"{', '.join(s.id for s in gate_suites(args.gate))}", file=sys.stderr)
        return 2
    if args.list:
        print_list(host, args.gate, selected)
        return 0
    if args.dry_run:
        print_dry_run(host, selected, args.no_build)
        return 0
    run = Regression(host, args.gate, args.only, args.no_build, args.report)
    return run.run()


if __name__ == "__main__":
    sys.exit(main())
