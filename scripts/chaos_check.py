#!/usr/bin/env python3
"""SentinelOps M3 chaos check. Standard library only, so it runs on the Windows host without installing anything.

    python scripts/chaos_check.py                  all faults of chaos/catalogue.json (18 min 40 s measured on the owner's machine)
    python scripts/chaos_check.py --only F10 F13   only these faults
    python scripts/chaos_check.py --quick          shorter waits (settle 60 s instead of 100 s); --only F10 --quick took about 4 minutes

Needs the full stack: docker compose --profile observability up -d --build --wait

What it does, one [OK] / [FAIL] line per step (a [WARN] line is not a failure):
  preflight   Prometheus and Loki are ready (Loki readiness is retried for up to 90 s: it answered HTTP 503 now and then),
              11 containers run, the 6 M1 containers are healthy, `list` shows every fault, `status` says
              "no active faults", unknown fault / forbidden target / duration over the cap are refused,
              then a light background load starts (M1 load test, 10 users, container sentinelops-check-load,
              NOT the F18 container sentinelops-chaos-load)
  per fault   wait until the stack was quiet for 100 s (the 1-minute windows read the previous minute),
              read the baselines, `inject`, wait for every expected_symptoms entry of the catalogue,
              `status` shows it active, a second `inject` is refused, `clear`, the stack is healthy again,
              the three ground-truth lines are right, no password was printed or written
  finish      stop the background load FIRST (the smoke test's stock check fails while a load test runs),
              `clear --all`, `status`, scripts/smoke_test.py (22/22), every line of ground_truth.jsonl parses,
              `git status` is the same as at the start
--quick is for a fast look: the first fault starts after only 60 s of load (rates read low in the first minute),
so the full run without --quick is the one that counts.
Exit code 0 = everything passed, 1 = at least one check failed, 130 = interrupted (the stack is cleaned up first).

How the time limits are read (also written in docs/CHAOS.md):
  * within_s of a symptom counts from the moment `inject` returned.
  * a Prometheus query is evaluated as it stands in the catalogue. A query result that is empty counts as 0.
    Values above 100 occur in the CPU-of-limit query: nothing here assumes a maximum of 100.
  * a Loki query has the window [2m]. The window is shortened to the time since the inject started, so that log
    lines of the previous fault (which ended about two minutes ago) can never satisfy it.

Only chaos/data/ground_truth.jsonl lines written DURING this run are judged one by one. The file may hold
events of earlier verification runs; it is deleted before real evaluation runs.
"""
import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAOS_PY = ROOT / "chaos" / "chaos.py"
CATALOGUE_PATH = ROOT / "chaos" / "catalogue.json"
GROUND_TRUTH_PATH = ROOT / "chaos" / "data" / "ground_truth.jsonl"
SMOKE_TEST = ROOT / "scripts" / "smoke_test.py"
ENV_PATH = ROOT / ".env"

PROMETHEUS = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
GATEWAY_PROBE_URL = "http://127.0.0.1:8000/api/products/SKU-001"

M1_SERVICES = ["postgres", "redis", "inventory", "payments", "orders", "gateway"]
OBSERVABILITY_SERVICES = ["prometheus", "cadvisor", "loki", "alloy", "grafana"]
BASE_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.override.yml"]
PROJECT_NAME = "sentinelops"                 # `name:` in docker-compose.yml

# The background load. Its name MUST differ from the name of the F18 container (sentinelops-chaos-load):
# `clear --all` removes that one, so the check's own load survives every clear (unit test: BackgroundLoadTests).
LOAD_CONTAINER = "sentinelops-check-load"
LOAD_SERVICE = "loadtest"
LOAD_USERS = 10
LOAD_SPAWN_RATE = 2

SETTLE_S = 100               # quiet time before the baselines are read (1-minute windows read the previous minute)
QUICK_SETTLE_S = 60
DURATION_MARGIN_S = 60       # a self-ending fault lasts: longest within_s + this, so that the clear is never late
QUICK_DURATION_MARGIN_S = 30
POLL_S = 5                   # Prometheus scrapes every 5 s
CHAOS_TIMEOUT_S = 900        # one chaos.py call (clear --all can wait 120 s per step)
DOCKER_TIMEOUT_S = 60
LOAD_START_TIMEOUT_S = 300   # the first start may build the load test image
LOKI_READY_ATTEMPTS = 18    # Loki /ready answered HTTP 503 for more than 12 s on a running stack: wait up to 90 s
LOKI_READY_DELAY_S = 5
PROMETHEUS_READY_ATTEMPTS = 3
PROMETHEUS_READY_DELAY_S = 3
PER_FAULT_OVERHEAD_S = 120   # inject, status, second inject, clear, recovery, checks (only used for the estimate)

EVENT_FIELDS = ["run_id", "event", "fault_id", "category", "target", "params", "ts",
                "expected_root_cause", "expected_action", "acceptable_actions"]
TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
CONDITION_RE = re.compile(r"^(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?)$")
RANGE_RE = re.compile(r"\[(\d+)([smh])\]")
RUN_ID_RE = re.compile(r"run id (F\d\d-[0-9a-f]{8})")
RECOVERED_RE = re.compile(r"recovered ([0-9.]+) s after the clear")
SMOKE_SUMMARY_RE = re.compile(r"(\d+)/(\d+) checks passed")
ALL_HEALTHY_LINE = "no active faults; the stack is healthy"

results = []                 # (name, ok, detail) of every [OK] / [FAIL] step
warnings = []                # names of the [WARN] lines
printed_output = []          # everything chaos.py printed in this run, scanned for passwords at the end


class Abort(Exception):
    """A preflight check failed: there is no point in going on."""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def warn(name, detail=""):
    warnings.append(name)
    print(f"[WARN] {name}" + (f"  ({detail})" if detail else ""), flush=True)


def say(text=""):
    print(text, flush=True)


def section(title):
    print(f"\n== {title} ==", flush=True)


def show_output(text, limit=25):
    """Print the last lines of a failed command, indented."""
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines[-limit:]:
        print("    " + line, flush=True)


def shorten(text, limit=60):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ---------------------------------------------------------------------------
# Pure helpers (unit tested in chaos/tests/test_chaos_check.py)
# ---------------------------------------------------------------------------

def parse_condition(text):
    """'> 85' -> ('>', 85.0). Same grammar as the catalogue validation in chaos.py."""
    match = CONDITION_RE.match(str(text).strip())
    if not match:
        raise ValueError(f"bad condition {text!r}: use '<op> <number>' with op one of > >= < <= ==")
    return match.group(1), float(match.group(2))


def condition_met(value, condition):
    """True when `value <op> number`. A missing value or NaN never meets a condition. There is no upper limit
    for the value: the CPU-of-limit query returns more than 100 when a container is above its limit."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    op, number = parse_condition(condition)
    return {">": value > number, ">=": value >= number, "<": value < number,
            "<=": value <= number, "==": value == number}[op]


def format_value(value):
    if value is None:
        return "no value"
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return f"{value:.4g}"


def instantiate(query, target):
    """$target in a catalogue query is the real target service."""
    return query.replace("$target", target)


def narrow_window(query, elapsed_s):
    """Shorten the (last) range selector of a LogQL query to the time since the inject started.

    {service="x"} |~ "p" [2m]  ->  [37s] when only 37 s have passed. A window is never lengthened and never
    shorter than 5 s. Without this, log lines of the previous fault could make the symptom true at once.
    """
    matches = list(RANGE_RE.finditer(query))
    if not matches:
        return query
    last = matches[-1]
    unit = {"s": 1, "m": 60, "h": 3600}[last.group(2)]
    window_s = int(last.group(1)) * unit
    new_s = min(window_s, max(5, int(math.ceil(elapsed_s))))
    if new_s == window_s:
        return query
    return query[:last.start()] + f"[{new_s}s]" + query[last.end():]


def vector_value(data):
    """The one number of a Prometheus/Loki instant-query answer. An empty result counts as 0 (D46)."""
    payload = data["data"]
    if payload.get("resultType") == "scalar":
        return float(payload["result"][1])
    result = payload["result"]
    if not result:
        return 0.0
    return float(result[0]["value"][1])          # "NaN" and "+Inf" strings convert to float as well


def max_within_s(fault):
    return max(symptom["within_s"] for symptom in fault["expected_symptoms"])


def duration_for(fault, margin_s):
    """--duration for `inject`: None for faults that stay until clear, else longest within_s + margin."""
    if fault["default_duration_s"] is None:
        return None
    return min(max_within_s(fault) + margin_s, fault["max_duration_s"])


def estimate_run_s(faults, settle_s, margin_s):
    """Rough upper estimate of the whole check (used for the background load duration and the banner)."""
    return sum(settle_s + max_within_s(f) + margin_s + PER_FAULT_OVERHEAD_S for f in faults) + 300


def resolve_root_cause(fault, target):
    cause = fault["expected_root_cause"]
    return {"service": target if cause["service"] == "$target" else cause["service"], "type": cause["type"]}


def parse_ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def ground_truth_problems(events, fault, target):
    """Judge the lines of ONE inject/clear pair, in file order. Returns {"order": [...], "recovery": [...],
    "labels": [...]}: problem texts per group, all lists empty when everything is right.

    order    the three events, one run id, right fault and target, all fields, ts is ISO 8601 and non-decreasing
    recovery no "late" flag (a normal check run clears while the effect is still there), recovery_s is a number
             that matches the time from the clear ts to the recovered ts
    labels   expected_root_cause, expected_action and acceptable_actions equal the catalogue
    ts values can be equal, so only the position in the file orders the events and the comparison uses <=.
    """
    order, recovery, labels = [], [], []
    problems = {"order": order, "recovery": recovery, "labels": labels}
    kinds = [event.get("event") for event in events]
    if kinds != ["inject", "clear", "recovered"]:
        order.append(f"the events are {kinds}, expected ['inject', 'clear', 'recovered']")
    for event in events:
        missing = [name for name in EVENT_FIELDS if name not in event]
        if missing:
            order.append(f"{event.get('event')}: missing fields {', '.join(missing)}")
    if len({event.get("run_id") for event in events}) > 1:
        order.append("the events do not share one run_id")
    for event in events:
        if event.get("fault_id") != fault["id"] or event.get("target") != target:
            order.append(f"{event.get('event')}: fault_id/target is {event.get('fault_id')}/{event.get('target')}, "
                         f"expected {fault['id']}/{target}")
    if order:
        return problems

    stamps = []
    for event in events:
        if not isinstance(event["ts"], str) or not TS_RE.match(event["ts"]):
            order.append(f"{event['event']}: ts {event['ts']!r} is not ISO 8601 with milliseconds and Z")
        else:
            stamps.append(parse_ts(event["ts"]))
    if len(stamps) == 3:
        if not stamps[0] <= stamps[1] <= stamps[2]:
            order.append("ts is not inject <= clear <= recovered")
        seconds = events[2].get("recovery_s")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            recovery.append(f"recovered: recovery_s is {seconds!r}, expected a number >= 0")
        elif abs(seconds - (stamps[2] - stamps[1]).total_seconds()) > 0.06:
            recovery.append(f"recovery_s {seconds} does not match the time from the clear ts to the recovered ts "
                            f"({(stamps[2] - stamps[1]).total_seconds():.3f} s)")
    for event in events:
        if "late" in event:
            recovery.append(f"{event['event']} has a late flag: the effect was already gone before the clear")

    cause = resolve_root_cause(fault, target)
    for event in events:
        if event["expected_root_cause"] != cause:
            labels.append(f"{event['event']}: expected_root_cause is {event['expected_root_cause']}, expected {cause}")
        if event["expected_action"] != fault["expected_action"]:
            labels.append(f"{event['event']}: expected_action is {event['expected_action']}")
        if event["acceptable_actions"] != fault["acceptable_actions"]:
            labels.append(f"{event['event']}: acceptable_actions is {event['acceptable_actions']}")
    return problems


def parse_event_lines(raw):
    """bytes of the ground-truth file (or a part of it) -> (events, bad line numbers). Line numbers count from 1."""
    events, bad = [], []
    for number, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            bad.append(number)
    return events, bad


_URL_PASSWORD_RE = re.compile(r"://[^:/@\s]+:([^@\s]+)@")
_KEY_VALUE_RE = re.compile(r"(?i)\b[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN)\s*[=:]\s*(\S+)")
MIN_SECRET_LENGTH = 6        # shorter .env values are plain words that can occur in normal text


def find_password_leaks(text, secrets=()):
    """Reasons why `text` shows a password: a known .env secret value, a password inside a URL, or a
    NAME_PASSWORD=value pair whose value is not masked. The text of a leak is never put in the result."""
    leaks = []
    if any(len(secret) >= MIN_SECRET_LENGTH and secret in text for secret in secrets):
        leaks.append("a secret value from .env")
    if any(match.group(1) != "***" for match in _URL_PASSWORD_RE.finditer(text)):
        leaks.append("a password inside a URL")
    if any(match.group(1).strip("'\"") != "***" for match in _KEY_VALUE_RE.finditer(text)):
        leaks.append("an unmasked NAME_PASSWORD=value pair")
    return leaks


def parse_env_secrets(text):
    """Values of the PASSWORD / SECRET / TOKEN entries of a .env file (only used to look for leaks)."""
    secrets = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if re.search(r"(?i)(PASSWORD|SECRET|TOKEN)", key) and value:
            secrets.append(value)
    return secrets


def stack_problems(rows, services):
    """Names (with the reason) of the services that are not running AND healthy, from `docker compose ps` rows."""
    states = {row.get("Service"): (str(row.get("State", "")).lower(), str(row.get("Health", "")).lower())
              for row in rows}
    problems = []
    for name in services:
        entry = states.get(name)
        if entry is None:
            problems.append(f"{name} (no container)")
        elif entry[0] != "running":
            problems.append(f"{name} ({entry[0]})")
        elif entry[1] != "healthy":
            problems.append(f"{name} (health: {entry[1] or 'none'})")
    return problems


def running_problems(rows, services):
    """Like stack_problems, but only 'running' counts (loki and alloy have no health check)."""
    states = {row.get("Service"): str(row.get("State", "")).lower() for row in rows}
    return [f"{name} ({states.get(name, 'no container')})" for name in services if states.get(name) != "running"]


def parse_ps(text):
    """`docker compose ps --format json`: one JSON array, or one JSON object per line."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def parse_inspect_lines(text):
    """Lines '<name> <restart count> <oom killed> <status>' -> {name: (restarts, oom_killed, status)}."""
    info = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4:
            info[parts[0].lstrip("/")] = (int(parts[1]), parts[2].lower() == "true", parts[3])
    return info


def restart_problems(before, after):
    """No container may have restarted, been OOM-killed or stopped between the two snapshots."""
    problems = []
    for name, (restarts, oom, status) in after.items():
        old = before.get(name)
        if old is not None and restarts != old[0]:
            problems.append(f"{name} restarted ({old[0]} -> {restarts})")
        if oom:
            problems.append(f"{name} was OOM-killed")
        if status != "running":
            problems.append(f"{name} is {status}")
    for name in before:
        if name not in after:
            problems.append(f"{name} disappeared")
    return problems


# ---------------------------------------------------------------------------
# Waiting for symptoms (the clock, the sleep and the query function are parameters, so a test can fake them)
# ---------------------------------------------------------------------------

def wait_symptoms(symptoms, target, t_before, t0, fetch, clock, sleep, report):
    """Poll every symptom until its condition is true or its within_s (counted from t0) is over.

    t_before: clock value just before `inject` started (the Loki window never reaches back before it).
    t0: clock value when `inject` returned. report(index, ok, value, seconds, detail) is called once per symptom,
    in the order in which they are decided. fetch(source, query) -> (value or None, error text).
    """
    pending = list(range(len(symptoms)))
    last = {}
    while pending:
        now = clock()
        for index in list(pending):
            symptom = symptoms[index]
            source = symptom.get("source", "prometheus")
            query = instantiate(symptom["query"], target)
            if source == "loki":
                query = narrow_window(query, now - t_before)
            value, error = fetch(source, query)
            last[index] = (value, error)
            elapsed = now - t0
            if condition_met(value, symptom["condition"]):
                report(index, True, value, elapsed, "")
                pending.remove(index)
            elif elapsed >= symptom["within_s"]:
                report(index, False, value, elapsed, error)
                pending.remove(index)
        if pending:
            sleep(POLL_S)
    return last


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(url, timeout=10):
    """(status, body text); (0, error text) when the host cannot be reached."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc)


def fetch_value(source, query, http=None):
    """Instant query against Prometheus or Loki. Returns (value, error): (None, text) when no number came back."""
    base = f"{PROMETHEUS}/api/v1/query" if source == "prometheus" else f"{LOKI}/loki/api/v1/query"
    status, text = (http or http_get)(base + "?" + urllib.parse.urlencode({"query": query}), 15)
    if status != 200:
        return None, f"{source} answered HTTP {status}"
    try:
        data = json.loads(text)
        if data.get("status") != "success":
            return None, f"{source} query error: {str(data.get('error', ''))[:80]}"
        return vector_value(data), ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return None, f"unexpected {source} answer ({exc})"


def wait_ready(url, attempts, delay_s, http=None, sleep=None):
    """GET url until HTTP 200. Returns (ok, detail). Loki /ready answered 503 now and then (four times seen, once for more than 12 s
    on a running stack), so one failure must not fail the check."""
    http, sleep = http or http_get, sleep or time.sleep
    status = 0
    for attempt in range(1, attempts + 1):
        status, _text = http(url, 5)
        if status == 200:
            return True, "HTTP 200" + (f", attempt {attempt} of {attempts}" if attempt > 1 else "")
        if attempt < attempts:
            sleep(delay_s)
    return False, f"HTTP {status} after {attempts} attempts"


# ---------------------------------------------------------------------------
# docker and chaos.py (every call is an argument list, no shell, with a timeout)
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    @property
    def text(self):
        return self.stdout + self.stderr


def run(args, timeout, env=None):
    """Run one command in the repo root. The child gets its own process group, so that a Ctrl+C typed in the
    console reaches only this script, which then cleans up (stop the load, clear --all)."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
    try:
        done = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, shell=False, env=child_env, **group)
    except FileNotFoundError:
        return Result(127, "", f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout} s: {' '.join(args[:5])}")
    return Result(done.returncode, done.stdout or "", done.stderr or "")


def chaos(*args):
    """python chaos/chaos.py <args>. Everything it prints is kept for the password scan at the end."""
    result = run([sys.executable, str(CHAOS_PY), *args], CHAOS_TIMEOUT_S)
    printed_output.append(result.text)
    return result


def compose_base():
    cmd = ["docker", "compose"]
    for name in BASE_COMPOSE_FILES:
        if (ROOT / name).exists():
            cmd += ["-f", name]
    return cmd


def compose_ps(profile_observability=False):
    cmd = ["docker", "compose"] + (["--profile", "observability"] if profile_observability else [])
    result = run(cmd + ["ps", "--all", "--format", "json"], DOCKER_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(f"docker compose ps failed: {shorten(result.text, 120)}")
    return parse_ps(result.stdout)


def gateway_status():
    return http_get(GATEWAY_PROBE_URL, 5)[0]


def stack_state():
    """(problems, detail) for: 6 M1 containers healthy and the gateway answers the product request with 200."""
    problems = stack_problems(compose_ps(), M1_SERVICES)
    if problems:
        return problems, "not healthy: " + ", ".join(problems)
    code = gateway_status()
    if code != 200:
        return [f"gateway answered {code}"], f"GET /api/products/SKU-001 returned {code}"
    return [], "6 containers healthy, GET /api/products/SKU-001 returned 200"


def container_snapshot():
    """{name: (restart count, OOM killed, status)} of the 6 M1 containers (docker inspect, nothing secret)."""
    ids = run(["docker", "compose", "ps", "-q", "--all"], DOCKER_TIMEOUT_S).stdout.split()
    if not ids:
        return {}
    out = run(["docker", "inspect", "--format", "{{.Name}} {{.RestartCount}} {{.State.OOMKilled}} {{.State.Status}}",
               *ids], DOCKER_TIMEOUT_S).stdout
    return parse_inspect_lines(out)


def target_env_state(service):
    """'ok' | 'wrong password' | 'no DATABASE_URL' | 'no container' for F06/F08 targets. Values are never printed."""
    ids = run(["docker", "compose", "ps", "-q", "--all", service], DOCKER_TIMEOUT_S).stdout.split()
    if not ids:
        return "no container"
    out = run(["docker", "inspect", "--format", "{{json .Config.Env}}", ids[0]], DOCKER_TIMEOUT_S).stdout
    try:
        env = json.loads(out)
    except ValueError:
        return "no container"
    url = next((item for item in env if item.startswith("DATABASE_URL=")), None)
    if url is None:
        return "no DATABASE_URL"
    return "wrong password" if "chaos-wrong-password-F08" in url else "ok"


# -- the background load ------------------------------------------------------------------------

def load_filters():
    """Only a container with this name AND the compose project, service and one-off labels is ever touched."""
    return ["--filter", f"name=^/{LOAD_CONTAINER}$",
            "--filter", f"label=com.docker.compose.project={PROJECT_NAME}",
            "--filter", f"label=com.docker.compose.service={LOAD_SERVICE}",
            "--filter", "label=com.docker.compose.oneoff=True"]


def load_command():
    return compose_base() + ["--profile", "load", "run", "-d", "--no-deps", "--name", LOAD_CONTAINER, LOAD_SERVICE]


def load_environment(duration_s):
    """USERS, SPAWN_RATE, DURATION go into the environment of the docker compose process: compose reads the
    ${...} placeholders of the loadtest service from there (the same way F18 does it)."""
    return {"USERS": str(LOAD_USERS), "SPAWN_RATE": str(LOAD_SPAWN_RATE), "DURATION": f"{int(duration_s)}s"}


def load_ids():
    return run(["docker", "ps", "-a", "-q", *load_filters()], DOCKER_TIMEOUT_S).stdout.split()


def load_status():
    ids = load_ids()
    if not ids:
        return "missing"
    out = run(["docker", "inspect", "--format", "{{.State.Status}}", ids[0]], DOCKER_TIMEOUT_S).stdout.strip()
    return "running" if out in ("running", "restarting") else "exited"


def remove_load():
    """docker rm -f by id. True when a container was removed."""
    ids = load_ids()
    if not ids:
        return False
    result = run(["docker", "rm", "-f", ids[0]], DOCKER_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(f"docker rm -f failed: {shorten(result.text, 120)}")
    return True


def start_load(duration_s):
    result = run(load_command(), LOAD_START_TIMEOUT_S, env=load_environment(duration_s))
    if result.returncode != 0:
        raise RuntimeError(f"the background load did not start: {shorten(result.text, 200)}")


def git_status():
    """Set of `git status --porcelain` lines, or None when git is not available."""
    result = run(["git", "status", "--porcelain"], DOCKER_TIMEOUT_S)
    if result.returncode != 0:
        return None
    return set(result.stdout.splitlines())


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

class Check:
    def __init__(self, catalogue, plan, quick):
        self.catalogue = catalogue
        self.plan = plan                      # list of fault ids
        self.quick = quick
        self.settle_s = QUICK_SETTLE_S if quick else SETTLE_S
        self.margin_s = QUICK_DURATION_MARGIN_S if quick else DURATION_MARGIN_S
        self.secrets = []
        self.quiet_since = None               # monotonic time of the last disturbance (load start, recovery)
        self.load_started = False
        self.git_before = None
        self.load_duration_s = int(1.2 * estimate_run_s([catalogue[f] for f in plan], self.settle_s, self.margin_s))

    # -- preflight ---------------------------------------------------------------------------
    def preflight(self):
        section("Preflight")
        try:
            self.secrets = parse_env_secrets(ENV_PATH.read_text(encoding="utf-8"))
        except OSError:
            self.secrets = []
        self.git_before = git_status()
        if self.git_before is None:
            warn("git is not available: the git status check is skipped")

        ok, detail = wait_ready(f"{PROMETHEUS}/-/ready", PROMETHEUS_READY_ATTEMPTS, PROMETHEUS_READY_DELAY_S)
        if not record("Prometheus is ready", ok, detail):
            say("    Prometheus is not reachable on 127.0.0.1:9090. This check needs the observability profile:")
            say("    docker compose --profile observability up -d --build --wait")
            raise Abort("Prometheus is not reachable")
        ok, detail = wait_ready(f"{LOKI}/ready", LOKI_READY_ATTEMPTS, LOKI_READY_DELAY_S)
        if not record("Loki is ready", ok, detail):
            say(f"    Loki /ready did not answer 200 within about {LOKI_READY_ATTEMPTS * LOKI_READY_DELAY_S} s. Nothing was injected. Look at:")
            say("    docker compose --profile observability logs loki --tail 50")
            raise Abort("Loki is not ready")

        rows = compose_ps(profile_observability=True)
        problems = running_problems(rows, M1_SERVICES + OBSERVABILITY_SERVICES)
        if not record("11 containers are running (6 M1 + 5 observability, M3 adds none)", not problems,
                      ", ".join(problems)):
            raise Abort("the full stack is not running")
        problems = stack_problems(rows, M1_SERVICES)
        if not record("the 6 M1 containers are healthy", not problems, ", ".join(problems)):
            raise Abort("the M1 containers are not healthy (repair with: python chaos/chaos.py clear --all)")
        apps, _ = fetch_value("prometheus", 'count(up{job=~"gateway|orders|payments|inventory"} == 1)')
        seen, _ = fetch_value("prometheus", "count(count by (service) (container_memory_working_set_bytes"
                                            '{service=~"gateway|orders|payments|inventory|postgres|redis"}))')
        if not record("Prometheus sees 4 app targets up and 6 containers in cAdvisor", apps == 4 and seen == 6,
                      f"{format_value(apps)} targets up, {format_value(seen)} containers"):
            raise Abort("Prometheus does not see the whole stack yet")

        result = chaos("list")
        listed = [fid for fid in self.catalogue if re.search(rf"^{fid}\s", result.stdout, re.M)]
        if not record(f"chaos.py list shows the {len(self.catalogue)} faults", result.returncode == 0
                      and len(listed) == len(self.catalogue), f"{len(listed)} found"):
            show_output(result.text)
        result = chaos("status")
        if not record('chaos.py status says "no active faults"', result.returncode == 0
                      and result.stdout.strip() == "no active faults", f"exit code {result.returncode}"):
            show_output(result.text)
            raise Abort("a fault is still recorded as active: run 'python chaos/chaos.py clear --all' first")

        self.refusal("an unknown fault is refused", ["inject", "F99"], "unknown fault")
        self.refusal("a target that is not allowed is refused", ["inject", "F10", "--target", "orders"], "not allowed")
        self.refusal("a duration above the cap is refused", ["inject", "F01", "--duration", "601"], "above the cap")

        left = remove_load()
        record("no leftover background load container", True,
               "removed one from an earlier run" if left else "")
        self.start_background_load()

    def refusal(self, name, args, expected_text):
        result = chaos(*args)
        record(name, result.returncode == 2 and expected_text in result.text,
               f"exit code {result.returncode}")
        if result.returncode != 2:
            show_output(result.text)

    def start_background_load(self):
        start_load(self.load_duration_s)
        self.load_started = True
        env = load_environment(self.load_duration_s)
        record("background load started", True,
               f"USERS={env['USERS']}, SPAWN_RATE={env['SPAWN_RATE']}, DURATION={env['DURATION']}, "
               f"container {LOAD_CONTAINER}")
        time.sleep(5)
        status = load_status()
        if not record("background load is running after 5 s", status == "running", status):
            say(f"    See: docker logs --tail 20 {LOAD_CONTAINER}")
            raise Abort("the background load does not run")
        self.quiet_since = time.monotonic()

    def ensure_background_load(self):
        """The load must run during every fault. If it ended (its DURATION was too short), start it again."""
        if load_status() == "running":
            return
        warn("the background load had stopped: it is started again")
        remove_load()
        self.start_background_load()

    # -- one fault ---------------------------------------------------------------------------
    def settle(self):
        remaining = self.settle_s - (time.monotonic() - self.quiet_since)
        if remaining > 0:
            say(f"settling for {int(math.ceil(remaining))} s: the 1-minute windows of the baselines must be clean ...")
            time.sleep(remaining)

    def baselines(self, fid, fault, target):
        """A Prometheus symptom must not be true before the injection, otherwise it proves nothing. (A Loki symptom
        counts the lines since the inject started, so it has no baseline.)"""
        for number, symptom in enumerate(fault["expected_symptoms"], start=1):
            if symptom.get("source", "prometheus") != "prometheus":
                continue
            query = instantiate(symptom["query"], target)
            value, error = fetch_value("prometheus", query)
            label = f"{fid} baseline of symptom {number}: {shorten(query)}"
            if value is None:
                record(label, False, error)
            elif condition_met(value, symptom["condition"]):
                text = f"already true before the injection: {format_value(value)} (condition {symptom['condition']})"
                if self.quick:
                    warn(label, text + "; --quick settles for less")
                else:
                    record(label, False, text + "; something else is disturbing the stack")
            else:
                record(label, True, f"{format_value(value)}, condition {symptom['condition']} not met")

    def check_fault(self, fid):
        fault = self.catalogue[fid]
        target = fault["default_target"]
        section(f"{fid} {fault['name']} (target {target})")
        self.ensure_background_load()
        self.settle()
        self.baselines(fid, fault, target)

        before_restarts = container_snapshot() if fid == "F18" else None
        offset = GROUND_TRUTH_PATH.stat().st_size if GROUND_TRUTH_PATH.exists() else 0
        duration = duration_for(fault, self.margin_s)
        command = ["inject", fid] + (["--duration", str(duration)] if duration is not None else [])

        t_before = time.monotonic()
        result = chaos(*command)
        t0 = time.monotonic()
        run_id = (RUN_ID_RE.search(result.stdout) or [None, "?"])[1]
        if not record(f"{fid} inject", result.returncode == 0,
                      f"exit code {result.returncode}, took {t0 - t_before:.1f} s, run id {run_id}"):
            show_output(result.text)
            chaos("clear", fid)             # the tool undoes a failed inject itself; this is only for safety
            self.quiet_since = time.monotonic()
            return

        self.symptoms(fid, fault, target, t_before, t0)

        result = chaos("status")
        record(f"{fid} status shows the fault as active",
               result.returncode == 0 and re.search(rf"^{fid}\s.*\bactive\s*$", result.stdout, re.M) is not None,
               f"exit code {result.returncode}")
        result = chaos("inject", fid)
        record(f"{fid} a second inject is refused", result.returncode == 2 and "already active" in result.text,
               f"exit code {result.returncode}")

        result = chaos("clear", fid)
        recovered = RECOVERED_RE.search(result.stdout)
        if not record(f"{fid} clear", result.returncode == 0 and recovered is not None,
                      f"exit code {result.returncode}, recovered " + (f"{recovered.group(1)} s after the clear"
                                                                      if recovered else "line not printed")):
            show_output(result.text)
        self.quiet_since = time.monotonic()      # the disturbance ended with the recovery

        problems, detail = stack_state()
        record(f"{fid} the stack is healthy again", not problems, detail)
        self.after_clear(fid, target, before_restarts)
        result = chaos("status")
        record(f"{fid} status shows no active faults", result.stdout.strip() == "no active faults",
               f"exit code {result.returncode}")
        self.ground_truth(fid, fault, target, offset)

    def symptoms(self, fid, fault, target, t_before, t0):
        symptoms = fault["expected_symptoms"]

        def report(index, ok, value, seconds, error):
            symptom = symptoms[index]
            source = symptom.get("source", "prometheus")
            query = shorten(instantiate(symptom["query"], target))
            text = f"{format_value(value)}, condition {symptom['condition']}"
            if ok:
                record(f"{fid} symptom {index + 1} ({source}) appeared after {seconds:.0f} s, limit "
                       f"{symptom['within_s']} s: {query}", True, text)
            else:
                record(f"{fid} symptom {index + 1} ({source}) did not appear within {symptom['within_s']} s: {query}",
                       False, text + (f"; {error}" if error else ""))

        say(f"waiting for {len(symptoms)} symptom(s) of {fid} (limits counted from the end of the inject) ...")
        wait_symptoms(symptoms, target, t_before, t0, fetch_value, time.monotonic, time.sleep, report)

    def after_clear(self, fid, target, before_restarts):
        if fid in ("F06", "F08"):
            state = target_env_state(target)
            left = (ROOT / "chaos" / "overrides" / f"{fid}-{target}.yml").exists()
            record(f"{fid} clear recreated {target} from the base files (DATABASE_URL back, fault file deleted)",
                   state == "ok" and not left, f"environment: {state}" + ("; fault file still there" if left else ""))
        if fid == "F18":
            record("F18 the load container sentinelops-chaos-load is gone",
                   not [i for i in run(["docker", "ps", "-a", "-q", "--filter", "name=^/sentinelops-chaos-load$"],
                                       DOCKER_TIMEOUT_S).stdout.split()], "")
            problems = restart_problems(before_restarts or {}, container_snapshot())
            record("F18 no M1 container restarted, exited or was OOM-killed during the surge", not problems,
                   ", ".join(problems))

    def ground_truth(self, fid, fault, target, offset):
        raw = GROUND_TRUTH_PATH.read_bytes()[offset:] if GROUND_TRUTH_PATH.exists() else b""
        events, bad = parse_event_lines(raw)
        if bad:
            record(f"{fid} ground truth: every new line parses", False, f"bad line(s): {bad}")
            return
        found = ground_truth_problems(events, fault, target)
        record(f"{fid} ground truth: inject, clear, recovered in file order, one run id, ts non-decreasing",
               not found["order"], "; ".join(found["order"]))
        recovery = events[2].get("recovery_s") if len(events) == 3 else None
        record(f"{fid} ground truth: no late flag, recovery_s matches the timestamps", not found["recovery"],
               "; ".join(found["recovery"]) if found["recovery"] else f"recovery_s {recovery}")
        cause = resolve_root_cause(fault, target)
        record(f"{fid} ground truth: expected root cause {cause['service'] or 'no single service'} / {cause['type']}, "
               f"action {fault['expected_action']}", not found["labels"], "; ".join(found["labels"]))
        leaks = find_password_leaks(raw.decode("utf-8", errors="replace") + "\n".join(printed_output[-8:]),
                                    self.secrets)
        record(f"{fid} no password in the printed output or in the new ground-truth lines", not leaks,
               ", ".join(leaks))

    # -- finish ------------------------------------------------------------------------------
    def finish(self):
        section("Finish")
        removed = remove_load()
        self.load_started = False
        record("background load removed (before the smoke test: its stock check fails during a load test)", True,
               "" if removed else "it had already ended")

        result = chaos("clear", "--all")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not record("clear --all returns the stack to healthy",
                      result.returncode == 0 and bool(lines) and lines[-1] == ALL_HEALTHY_LINE,
                      f"exit code {result.returncode}"):
            show_output(result.text)
        result = chaos("status")
        record('chaos.py status says "no active faults"', result.stdout.strip() == "no active faults",
               f"exit code {result.returncode}")

        say("running scripts/smoke_test.py (about 20 s) ...")
        smoke = run([sys.executable, str(SMOKE_TEST)], 300)
        match = SMOKE_SUMMARY_RE.search(smoke.stdout)
        passed = smoke.returncode == 0 and match is not None and match.group(1) == match.group(2) == "22"
        if not record("scripts/smoke_test.py passes 22/22", passed,
                      f"{match.group(1)}/{match.group(2)} checks passed" if match else f"exit code {smoke.returncode}"):
            for line in smoke.stdout.splitlines():
                if line.startswith("[FAIL]"):
                    say("    " + line)

        self.whole_file()
        if self.git_before is not None:
            after = git_status()
            changed = sorted((after or set()) ^ self.git_before)
            record("git status is the same as at the start (no tracked file was modified)",
                   after is not None and not changed, "; ".join(changed[:3]))

    def whole_file(self):
        if not GROUND_TRUTH_PATH.exists():
            record("ground_truth.jsonl exists", False, str(GROUND_TRUTH_PATH))
            return
        raw = GROUND_TRUTH_PATH.read_bytes()
        events, bad = parse_event_lines(raw)
        crlf = b"\r\n" in raw
        record(f"ground_truth.jsonl: all {len(events)} lines parse with json.loads, \\n line ends",
               not bad and not crlf, (f"bad line(s): {bad[:5]}" if bad else "") + ("; CRLF found" if crlf else ""))
        leaks = find_password_leaks(raw.decode("utf-8", errors="replace"), self.secrets)
        record("ground_truth.jsonl contains no password", not leaks, ", ".join(leaks))
        leaks = find_password_leaks("\n".join(printed_output), self.secrets)
        record("nothing chaos.py printed during this check contains a password", not leaks, ", ".join(leaks))


def emergency_cleanup():
    """After Ctrl+C or a failed preflight: remove the background load and clear every fault. Ctrl+C is ignored
    meanwhile, so that the cleanup cannot be cut short."""
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        say("\ncleaning up: removing the background load and running clear --all ...")
        try:
            remove_load()
        except RuntimeError as exc:
            say(f"  could not remove the background load: {exc}")
        result = chaos("clear", "--all")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        say(f"  clear --all: exit code {result.returncode}" + (f", last line: {lines[-1]}" if lines else ""))
    finally:
        signal.signal(signal.SIGINT, old)


def summary(complete=True):
    failed = [name for name, ok, _ in results if not ok]
    say(f"\n{len(results) - len(failed)}/{len(results)} checks passed" +
        (f"   ({len(warnings)} warning(s))" if warnings else ""))
    if failed:
        say("FAILED: " + "; ".join(failed))
        return 1
    if not complete:
        say("CHAOS CHECK INCOMPLETE: it stopped before the last step")
        return 1
    say("CHAOS CHECK PASSED")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="SentinelOps M3 chaos check")
    parser.add_argument("--only", nargs="+", metavar="ID", help="check only these faults, for example F10 F13")
    parser.add_argument("--quick", action="store_true", help="settle 60 s instead of 100 s, shorter fault durations")
    args = parser.parse_args(argv)

    try:
        catalogue = {fault["id"]: fault for fault in json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot read {CATALOGUE_PATH}: {exc}", file=sys.stderr)
        return 1
    plan = list(catalogue)
    if args.only:
        wanted = [name.upper() for name in args.only]
        unknown = [name for name in wanted if name not in catalogue]
        if unknown:
            print(f"unknown fault(s): {', '.join(unknown)}. Known faults: {', '.join(catalogue)}", file=sys.stderr)
            return 2
        plan = [fid for fid in catalogue if fid in wanted]

    check = Check(catalogue, plan, args.quick)
    minutes = estimate_run_s([catalogue[f] for f in plan], check.settle_s, check.margin_s) // 60
    say(f"SentinelOps M3 chaos check: {', '.join(plan)} | {'quick' if args.quick else 'normal'} mode | "
        f"settle {check.settle_s} s | at most about {minutes} minutes")
    try:
        check.preflight()
        for fid in plan:
            check.check_fault(fid)
        check.finish()
        return summary()
    except Abort as exc:
        say(f"\nstopped: {exc}")
        if check.load_started:
            emergency_cleanup()
        summary(complete=False)
        return 1
    except KeyboardInterrupt:
        say("\ninterrupted")
        emergency_cleanup()
        summary(complete=False)
        return 130
    except Exception as exc:                          # a bug or an unexpected docker error: never leave a fault behind
        say(f"\nunexpected error: {type(exc).__name__}: {exc}")
        emergency_cleanup()
        summary(complete=False)
        return 1


if __name__ == "__main__":
    sys.exit(main())
