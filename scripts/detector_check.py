#!/usr/bin/env python3
"""SentinelOps M4 detector check. Standard library only, so it runs on the Windows host without installing anything.

    python scripts/detector_check.py                  preflight, baseline, F10, F13, F06, no-data step
    python scripts/detector_check.py --only F10 F13   only these faults (the baseline and the no-data step still run)
    python scripts/detector_check.py --quick          shorter waits (see the banner); the full run without --quick is the one that counts
    python scripts/detector_check.py --skip-nodata    leave out the Prometheus / Loki no-data step

Needs the full stack with the detector:
    docker compose --profile observability --profile detector up -d --build --wait

What it does, one [OK] / [FAIL] line per step (a [WARN] line is not a failure):
  preflight   Prometheus and Loki are ready (Loki readiness is retried: /ready answers HTTP 503 now and then), the detector
              answers /health and /ready, 11 + 1 containers run (6 M1 healthy, detector healthy), Prometheus scrapes the
              detector, both data sources of the detector are "ok", an incident left by an earlier run is given up to 200 s
              to resolve, no chaos fault is recorded as active
  baseline    the stack is healthy for 90 s, then N minutes idle and N minutes with 10 load-test users (3 and 5 by default, the
              real durations are printed). It FAILS on any rule that fires, any rule that reports no_data, any new incident.
              The highest value per rule and subject is read from Prometheus (max_over_time of detector_rule_value)
  faults      F10, F13, F06 through `python chaos/chaos.py run <ID>` (never through docker). Per fault: exactly one new incident,
              it opened within the limit, the expected rules are among its symptoms, primary_service is as expected, it
              resolved within 180 s after the `recovered` event, it was not reopened, the detector /health stayed 200 and its
              RestartCount did not change. The measured delay is printed
  no data     Prometheus (then Loki) is stopped for 60 s and started again: the detector reports no_data within 30 s, no
              incident is created or resolved, the status goes through warming_up to ok, 90 s later there is still no new incident
  finish      the background load is removed, `chaos.py status` says no active faults, the detector did not restart, no incident is open

How the times are read (also written in docs/DETECTOR.md):
  * A delay counts from the END of the inject: the moment `chaos.py run` printed "injected <ID> on <target> (run id ...)".
    The `inject` event `ts` of chaos/data/ground_truth.jsonl is taken before the first docker action, about 2 s earlier;
    that delay is printed as well. The limits are the ones of docs/CHAOS.md (counted from the end of the inject).
  * opened_at, resolved_at and the event ts are UTC. The clock of the detector container is compared with this host through
    the HTTP Date header of the detector (resolution 1 s); a difference above 3 s is a [WARN].
  * Only incidents that did not exist before a run are judged for that run (older ones are ignored); events are ordered by
    their position in the file, never by ts.
  * Between two faults the script waits for the previous incident to resolve, at least 100 s after its `recovered` event
    AND at least reopen_within_s (read from the detector, 120 s by default) + 10 s after it resolved: the next opening
    symptom would otherwise REOPEN the old incident instead of creating a new one.

Exit code 0 = everything passed, 1 = at least one check failed, 2 = bad arguments, 130 = interrupted (cleaned up first).
"""
import argparse
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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAOS_PY = ROOT / "chaos" / "chaos.py"
CATALOGUE_PATH = ROOT / "chaos" / "catalogue.json"
GROUND_TRUTH_PATH = ROOT / "chaos" / "data" / "ground_truth.jsonl"

PROMETHEUS = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
DETECTOR = "http://127.0.0.1:9100"

M1_SERVICES = ["postgres", "redis", "inventory", "payments", "orders", "gateway"]
OBSERVABILITY_SERVICES = ["prometheus", "cadvisor", "loki", "alloy", "grafana"]
DETECTOR_SERVICE = "detector"
BASE_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.override.yml"]
PROJECT_NAME = "sentinelops"
# Which compose profile a service needs. Every compose call for these services carries it (without it Compose does not know them).
SERVICE_PROFILE = {"prometheus": "observability", "loki": "observability", DETECTOR_SERVICE: "detector"}

# The background load. Its name differs from sentinelops-chaos-load (F18, removed by `clear --all`) and from the load of
# scripts/chaos_check.py (sentinelops-check-load), so that no other tool ever touches it.
LOAD_CONTAINER = "sentinelops-detector-check-load"
LOAD_SERVICE = "loadtest"
LOAD_USERS = 10
LOAD_SPAWN_RATE = 2

# Expectations per fault (docs/CHAOS.md limits, counted from the END of the inject).
# rules: (rule id, service or None = any service). primary: expected primary_service (None = null).
EXPECTATIONS = {
    "F10": {"target": "postgres", "rules": [("R02", "postgres"), ("R05", None)], "primary": "postgres", "limit_s": 50},
    "F13": {"target": "redis", "rules": [("R02", "redis"), ("R06", None)], "primary": "redis", "limit_s": 50},
    "F06": {"target": "inventory", "rules": [("R01", "inventory")], "primary": "inventory", "limit_s": 50},
}
FAULT_ORDER = ["F10", "F13", "F06"]

HEALTHY_S, QUICK_HEALTHY_S = 90, 30
IDLE_MIN, LOAD_MIN = 3, 5
QUICK_IDLE_MIN, QUICK_LOAD_MIN = 1, 2
SETTLE_S, QUICK_SETTLE_S = 100, 60          # quiet time after a `recovered` event before the next fault
REOPEN_MARGIN_S = 10                        # on top of reopen_within_s
HOLD_MARGIN_S, QUICK_HOLD_MARGIN_S = 40, 25  # the fault is held for limit + this
RESOLVE_LIMIT_S = 180                       # the incident must be resolved this long after the recovered event
OPEN_INCIDENT_WAIT_S = 200                  # preflight: an incident of an earlier run may still resolve
SOURCES_OK_WAIT_S = 120                     # preflight: warm-up of the detector (30 s) plus margin
NODATA_DETECT_S = 30
OUTAGE_S = 60
AFTER_OK_S, QUICK_AFTER_OK_S = 90, 45
POLL_S = 5
HEALTH_POLL_S = 2
LOKI_READY_ATTEMPTS, LOKI_READY_DELAY_S = 18, 5                # preflight, as in chaos_check.py
LOKI_RESTART_READY_ATTEMPTS = 36                               # after a restart: 503 can last for minutes
PROMETHEUS_READY_ATTEMPTS, PROMETHEUS_READY_DELAY_S = 3, 3
DETECTOR_READY_ATTEMPTS, DETECTOR_READY_DELAY_S = 6, 5
CHAOS_TIMEOUT_S = 900
DOCKER_TIMEOUT_S = 60
COMPOSE_UP_TIMEOUT_S = 240
LOAD_START_TIMEOUT_S = 300
CLOCK_WARN_S = 3.0

TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
INJECTED_RE = re.compile(r"injected (F\d\d) on (\S+) \(run id (F\d\d-[0-9a-f]{8})\)")
RECOVERED_RE = re.compile(r"recovered ([0-9.]+) s after the clear")

results = []          # (name, ok, detail) of every [OK] / [FAIL] step
warnings = []
summary_rows = []     # one row per fault for the final table
baseline_rows = []    # (phase, seconds)


class Abort(Exception):
    """A preflight check failed: there is no point in going on."""


class ApiError(Exception):
    """The detector API did not answer as expected."""


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
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines[-limit:]:
        print("    " + line, flush=True)


def shorten(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


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


# ---------------------------------------------------------------------------
# Pure helpers (unit tested in detector/tests/test_detector_check.py; no Docker, no network)
# ---------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc)


def parse_ts(text):
    """ISO 8601 with a Z (or an offset) -> aware UTC datetime."""
    parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_between(later, earlier):
    return (later - earlier).total_seconds()


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


def run_events(events, run_id):
    """The events of one experiment, in file order."""
    return [event for event in events if event.get("run_id") == run_id]


def run_problems(events, fault_id, target):
    """Judge the events of ONE run, in file order: inject, clear, recovered; one run id; right fault and target;
    ts is ISO 8601 and not decreasing (equal values happen); no late flag (the effect was still there at the clear)."""
    problems = []
    kinds = [event.get("event") for event in events]
    if kinds != ["inject", "clear", "recovered"]:
        problems.append(f"the events are {kinds}, expected ['inject', 'clear', 'recovered']")
        return problems
    if len({event.get("run_id") for event in events}) != 1:
        problems.append("the events do not share one run_id")
    for event in events:
        if event.get("fault_id") != fault_id or event.get("target") != target:
            problems.append(f"{event['event']}: fault_id/target is {event.get('fault_id')}/{event.get('target')}, "
                            f"expected {fault_id}/{target}")
        if "late" in event:
            problems.append(f"{event['event']} has a late flag: the effect was already gone before the clear")
    stamps = []
    for event in events:
        ts = event.get("ts")
        if not isinstance(ts, str) or not TS_RE.match(ts):
            problems.append(f"{event['event']}: ts {ts!r} is not ISO 8601 with milliseconds and Z")
        else:
            stamps.append(parse_ts(ts))
    if len(stamps) == 3 and not stamps[0] <= stamps[1] <= stamps[2]:
        problems.append("ts is not inject <= clear <= recovered")
    return problems


def parse_injected_line(line):
    """'injected F10 on postgres (run id F10-3f9a1c2e)' -> (fault id, target, run id), or None."""
    match = INJECTED_RE.search(line)
    return match.groups() if match else None


def incident_snapshot(incidents):
    """{incident id: (status, reopen_count)}. updated_at is left out on purpose: an open incident changes it every cycle."""
    return {i["incident_id"]: (i["status"], i["reopen_count"]) for i in incidents}


def snapshot_changes(before, after):
    """Texts that describe every difference between two snapshots (new, vanished, resolved, reopened incidents)."""
    changes = []
    for incident_id in sorted(after):
        if incident_id not in before:
            changes.append(f"{incident_id} was created ({after[incident_id][0]})")
        elif after[incident_id] != before[incident_id]:
            changes.append(f"{incident_id} changed from {before[incident_id]} to {after[incident_id]}")
    for incident_id in sorted(before):
        if incident_id not in after:
            changes.append(f"{incident_id} disappeared")
    return changes


def incidents_for_run(incidents, before):
    """(new incidents, ids of older incidents that were reopened) compared with the snapshot taken before the run.
    Incidents that already existed and did not change are ignored."""
    new = [i for i in incidents if i["incident_id"] not in before]
    reopened = [i["incident_id"] for i in incidents
                if i["incident_id"] in before and i["reopen_count"] != before[i["incident_id"]][1]]
    return sorted(new, key=lambda i: i["opened_at"]), reopened


def symptom_pairs(incident):
    return [(s["rule_id"], s["service"]) for s in incident["symptoms"]]


def missing_rules(incident, wanted):
    """The (rule id, service or None) pairs of `wanted` that no symptom of the incident matches."""
    pairs = symptom_pairs(incident)
    return [(rule, service) for rule, service in wanted
            if not any(r == rule and (service is None or s == service) for r, s in pairs)]


def describe_pair(rule, service):
    return f"{rule} ({service})" if service else rule


def incident_checks(incident, expectation, reference, inject_ts, recovered_ts, resolve_limit_s=RESOLVE_LIMIT_S):
    """[(name, ok, detail)] for ONE incident against the expectation of its fault, in a fixed order.

    reference: end of the inject (the limit counts from here); inject_ts: the `inject` event ts (about 2 s earlier);
    recovered_ts: ts of the `recovered` event. All are aware datetimes."""
    opened = parse_ts(incident["opened_at"])
    delay = seconds_between(opened, reference)
    delay_ts = seconds_between(opened, inject_ts)
    limit = expectation["limit_s"]
    checks = []
    detail = f"{delay:.1f} s after the end of the inject, {delay_ts:.1f} s after the inject ts"
    ok = 0 <= delay <= limit
    if delay < 0:
        detail += "; opened BEFORE the inject returned (clock difference between host and container?)"
    elif delay > limit:
        detail += f"; limit {limit} s"
    checks.append((f"opened within {limit} s of the end of the inject", ok, detail))

    absent = missing_rules(incident, expectation["rules"])
    present = ", ".join(sorted({describe_pair(r, s) for r, s in symptom_pairs(incident)}))
    wanted = ", ".join(describe_pair(r, s) for r, s in expectation["rules"])
    checks.append((f"the symptoms include {wanted}", not absent,
                   ("missing " + ", ".join(describe_pair(r, s) for r, s in absent) + "; symptoms: " + present)
                   if absent else f"{len(incident['symptoms'])} symptoms: {present}"))

    primary = incident["primary_service"]
    label = expectation["primary"] if expectation["primary"] else "null"
    checks.append((f"primary_service is {label}", primary == expectation["primary"],
                   f"primary_service is {primary!r}, candidates {incident['candidate_services']}"
                   if primary != expectation["primary"] else f"candidates {incident['candidate_services']}"))

    if incident["status"] != "resolved" or not incident.get("resolved_at"):
        checks.append((f"resolved within {resolve_limit_s} s after the recovered event", False,
                       f"status is {incident['status']!r}"))
    else:
        after = seconds_between(parse_ts(incident["resolved_at"]), recovered_ts)
        checks.append((f"resolved within {resolve_limit_s} s after the recovered event",
                       after <= resolve_limit_s, f"resolved {after:.1f} s after the recovered event"
                       + (f"; limit {resolve_limit_s} s" if after > resolve_limit_s else "")))
    active = [f"{s['rule_id']} {s['service']}" for s in incident["symptoms"] if s["active"]]
    checks.append(("not reopened and no symptom still active",
                   incident["reopen_count"] == 0 and not active,
                   f"reopen_count {incident['reopen_count']}" + (f"; active: {', '.join(active)}" if active else "")))
    return checks


def measurements(incident, reference, inject_ts, recovered_ts):
    """The numbers that the final table and docs/DETECTOR.md use."""
    opened = parse_ts(incident["opened_at"])
    first = min((parse_ts(s["first_seen"]) for s in incident["symptoms"]), default=opened)
    resolved = parse_ts(incident["resolved_at"]) if incident.get("resolved_at") else None
    return {"incident_id": incident["incident_id"],
            "delay_s": seconds_between(opened, reference),
            "delay_from_inject_ts_s": seconds_between(opened, inject_ts),
            "first_symptom_s": seconds_between(first, reference),
            "resolve_s": seconds_between(resolved, recovered_ts) if resolved else None,
            "symptoms": len(incident["symptoms"]),
            "rules": sorted({s["rule_id"] for s in incident["symptoms"]}),
            "primary": incident["primary_service"]}


def parse_vector(data):
    """Prometheus instant-query answer -> [(labels, float value)]. Raises ValueError on anything but a vector."""
    if data.get("status") != "success":
        raise ValueError(f"query error: {str(data.get('error', ''))[:100]}")
    payload = data["data"]
    if payload.get("resultType") != "vector":
        raise ValueError(f"unexpected result type {payload.get('resultType')!r}")
    return [(item["metric"], float(item["value"][1])) for item in payload["result"]]


def series_name(labels):
    return f"{labels.get('rule_id', '?')} {labels.get('subject') or '-'}"


def state_problems(max_state, min_state):
    """(firing, no_data): names of the series whose state was ever 1 (firing) or -1 (no_data) in the window.
    max_state / min_state: parse_vector() of max_over_time / min_over_time of detector_rule_state."""
    firing = sorted(series_name(m) for m, v in max_state if v > 0.5)
    no_data = sorted(series_name(m) for m, v in min_state if v < -0.5)
    return firing, no_data


def value_rows(max_value, rules):
    """Rows (rule, name, subject, highest value, 'of threshold', condition) for the baseline table, sorted. A rule without
    any series (no value in the window) gets one row 'no value'."""
    by_id = {r["id"]: r for r in rules}
    seen, rows = set(), []
    for labels, value in sorted(max_value, key=lambda item: (item[0].get("rule_id", ""), item[0].get("subject", ""))):
        rule = by_id.get(labels.get("rule_id"))
        seen.add(labels.get("rule_id"))
        condition = f"{rule['comparator']} {rule['threshold']:g}" if rule else "?"
        share = ""
        if rule and rule["comparator"] == ">" and rule["threshold"] > 0:
            share = f"{100.0 * value / rule['threshold']:.0f} %"
        rows.append((labels.get("rule_id", "?"), rule["name"] if rule else "?", labels.get("subject") or "-",
                     f"{value:.4g}", share, condition))
    for rule in rules:
        if rule["id"] not in seen:
            rows.append((rule["id"], rule["name"], "-", "no value", "", f"{rule['comparator']} {rule['threshold']:g}"))
    return sorted(rows, key=lambda r: (r[0], r[2]))


def sources_not_ok(status):
    """['prometheus (no_data)', ...] for every data source of the detector that is not in state ok."""
    return [f"{name} ({info['state']})" for name, info in sorted(status["sources"].items()) if info["state"] != "ok"]


def rule_state_problems(status, source, expected_state):
    """For the no-data step: the rules of `source` must all be in expected_state; returns texts for those that are not."""
    return [f"{r['rule_id']} is {r['state']}" for r in status["rules"] if r["source"] == source and r["state"] != expected_state]


def other_source_problems(status, source):
    """Rules of the OTHER data source must not be affected by the outage."""
    return [f"{r['rule_id']} is {r['state']}" for r in status["rules"] if r["source"] != source and r["state"] != "ok"]


def warmup_path(states):
    """The states seen after a restart, as a list in time order -> (problems, notes).

    Expected: no_data (the source is still down or not ready) ... warming_up ... ok. 'ok' before any 'warming_up' means
    the warm-up was skipped. A fall back to no_data after warming_up started is a note (Loki /ready can answer 503 again)."""
    collapsed = [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]]
    problems, notes = [], []
    if "warming_up" not in collapsed:
        problems.append("the status never showed warming_up" + (" (it went to ok at once)" if "ok" in collapsed else ""))
    elif "ok" in collapsed and collapsed.index("ok") < collapsed.index("warming_up"):
        problems.append("the status was ok before warming_up: the warm-up was skipped")
    if "ok" not in collapsed:
        problems.append("the status never reached ok")
    if "warming_up" in collapsed:
        start = collapsed.index("warming_up")
        if "no_data" in collapsed[start:]:
            notes.append("the source fell back to no_data after the warm-up had started: " + " -> ".join(collapsed))
    return problems, notes


def settle_wait_s(now, quiet_since, settle_s, resolved_at, reopen_within_s, margin_s=None):
    """Seconds to wait before the next fault: settle_s of quiet time since `quiet_since` (the previous `recovered` event),
    and at least reopen_within_s + margin_s after the previous incident resolved (else its opener would reopen it).
    margin_s defaults to REOPEN_MARGIN_S, looked up at call time."""
    margin_s = REOPEN_MARGIN_S if margin_s is None else margin_s
    wait = 0.0
    if quiet_since is not None:
        wait = max(wait, seconds_between(quiet_since, now) + settle_s)
    if resolved_at is not None:
        wait = max(wait, seconds_between(resolved_at, now) + reopen_within_s + margin_s)
    return max(0.0, wait)


def hold_for(limit_s, quick):
    return int(limit_s + (QUICK_HOLD_MARGIN_S if quick else HOLD_MARGIN_S))


def service_profile(service):
    if service not in SERVICE_PROFILE:
        raise KeyError(f"no compose profile known for {service!r}")
    return SERVICE_PROFILE[service]


def compose_command(base, profiles, args):
    """docker compose [-f ...] --profile P ... <args>. At least one profile is required: without it Compose does not
    know the profile-gated services (prometheus, loki, detector)."""
    if not profiles:
        raise ValueError("a compose call for a profile-gated service must name its profile")
    cmd = list(base)
    for profile in profiles:
        cmd += ["--profile", profile]
    return cmd + list(args)


def compose_args_for(service, *args):
    """(profiles, args) for one service, using its own profile."""
    return [service_profile(service)], list(args)


def skew_from_date(header, host_now):
    """Seconds the detector clock is ahead of the host (negative: behind), from an HTTP Date header (1 s resolution)."""
    try:
        sent = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return seconds_between(sent, host_now)


def wait_until(predicate, timeout_s, interval_s, clock=None, sleep=None):
    """Call predicate() until it returns a truthy value or timeout_s has passed. Returns (value, seconds waited).
    The value is falsy on a timeout. predicate runs at least once and once more at the deadline.
    clock and sleep default to time.monotonic and time.sleep, looked up at call time (a test can replace them)."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    start = clock()
    while True:
        value = predicate()
        elapsed = clock() - start
        if value:
            return value, elapsed
        if elapsed >= timeout_s:
            return value, elapsed
        sleep(min(interval_s, max(0.0, timeout_s - elapsed)))


def estimate_run_s(plan, quick, skip_nodata):
    """Rough upper estimate of the whole check for the banner."""
    healthy = QUICK_HEALTHY_S if quick else HEALTHY_S
    idle, load = (QUICK_IDLE_MIN, QUICK_LOAD_MIN) if quick else (IDLE_MIN, LOAD_MIN)
    settle = QUICK_SETTLE_S if quick else SETTLE_S
    total = 120 + healthy + 60 * (idle + load)
    for fid in plan:
        total += hold_for(EXPECTATIONS[fid]["limit_s"], quick) + 60 + 75 + max(settle, 130)
    if not skip_nodata:
        total += 2 * (OUTAGE_S + 90 + 45 + (QUICK_AFTER_OK_S if quick else AFTER_OK_S))
    return total


def final_table(rows):
    """Lines of the summary table: one row per fault."""
    headers = ["fault", "incident", "opened after (s)", "limit (s)", "rules in the incident", "primary", "resolved after (s)", "result"]
    table = []
    for r in rows:
        table.append([r["fault"], r.get("incident_id", "-"),
                      f"{r['delay_s']:.1f}" if r.get("delay_s") is not None else "-", r["limit_s"],
                      " ".join(r.get("rules", [])) or "-", r.get("primary") or "null",
                      f"{r['resolve_s']:.1f}" if r.get("resolve_s") is not None else "-", "PASS" if r["ok"] else "FAIL"])
    return format_table(headers, table)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(url, timeout=10):
    """(status, body text, headers); (0, error text, {}) when the host cannot be reached."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", errors="replace"), dict(exc.headers)
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 0, str(exc), {}


def wait_ready(url, attempts, delay_s, http=None, sleep=None):
    """GET url until HTTP 200. Returns (ok, detail). Loki /ready answers 503 now and then, so one failure must not fail the check."""
    http, sleep = http or http_get, sleep or time.sleep
    status = 0
    for attempt in range(1, attempts + 1):
        status = http(url, 5)[0]
        if status == 200:
            return True, "HTTP 200" + (f", attempt {attempt} of {attempts}" if attempt > 1 else "")
        if attempt < attempts:
            sleep(delay_s)
    return False, f"HTTP {status} after {attempts} attempts"


def api(path, timeout=8):
    status, text, _ = http_get(DETECTOR + path, timeout)
    if status != 200:
        raise ApiError(f"detector {path} answered HTTP {status}: {shorten(text, 80)}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ApiError(f"detector {path} did not return JSON ({exc})")


def get_status():
    return api("/internal/v1/status")


def get_incidents():
    return api("/internal/v1/incidents?limit=200")["incidents"]


def prom_vector(query):
    status, text, _ = http_get(PROMETHEUS + "/api/v1/query?" + urllib.parse.urlencode({"query": query}), 20)
    if status != 200:
        raise ApiError(f"Prometheus answered HTTP {status}")
    try:
        return parse_vector(json.loads(text))
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError(f"unexpected Prometheus answer ({exc})")


# ---------------------------------------------------------------------------
# Processes: docker, chaos.py (every call is an argument list, no shell, with a timeout)
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    @property
    def text(self):
        return self.stdout + self.stderr


def _group_flags():
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def run(args, timeout, env=None):
    """Run one command in the repo root. The child gets its own process group, so that a Ctrl+C typed in the console
    reaches only this script, which then cleans up."""
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
    try:
        done = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, shell=False, env=child_env, **_group_flags())
    except FileNotFoundError:
        return Result(127, "", f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout} s: {' '.join(args[:5])}")
    return Result(done.returncode, done.stdout or "", done.stderr or "")


def chaos(*args):
    return run([sys.executable, str(CHAOS_PY), *args], CHAOS_TIMEOUT_S)


def stream(args, on_line, poll, poll_every_s, timeout_s, env=None, started=None):
    """Run a command and hand every output line to on_line(utc arrival time, text) AS IT ARRIVES; call poll() every
    poll_every_s seconds meanwhile. Returns (returncode, lines). The process is killed after timeout_s (returncode 124).
    `started` (a list) receives the Popen object, so that the caller can end the child on Ctrl+C."""
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", **(env or {})}
    child = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1, shell=False, env=child_env, **_group_flags())
    if started is not None:
        started.append(child)
    lines = []

    def reader():
        for raw in child.stdout:
            text = raw.rstrip("\r\n")
            stamp = utcnow()
            lines.append(text)
            on_line(stamp, text)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_s
    code = None
    try:
        while code is None:
            try:
                code = child.wait(timeout=poll_every_s)
            except subprocess.TimeoutExpired:
                poll()
                if time.monotonic() > deadline:
                    child.kill()
                    child.wait()
                    code = 124
                    lines.append(f"timed out after {timeout_s} s")
    finally:
        if child.poll() is None:
            child.kill()
        thread.join(timeout=5)
        child.stdout.close()
    return code, lines


def compose_base():
    cmd = ["docker", "compose"]
    for name in BASE_COMPOSE_FILES:
        if (ROOT / name).exists():
            cmd += ["-f", name]
    return cmd


def compose(profiles, *args, timeout=DOCKER_TIMEOUT_S, env=None):
    return run(compose_command(compose_base(), profiles, args), timeout, env=env)


def compose_service(service, *args, timeout=DOCKER_TIMEOUT_S):
    """A compose call for one profile-gated service, with ITS profile."""
    profiles, rest = compose_args_for(service, *args)
    return compose(profiles, *rest, timeout=timeout)


def parse_ps(text):
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def stack_problems(rows, services):
    """Services that are not running AND healthy, from `docker compose ps` rows."""
    states = {row.get("Service"): (str(row.get("State", "")).lower(), str(row.get("Health", "")).lower()) for row in rows}
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


def compose_ps():
    result = compose(["observability", "detector"], "ps", "--all", "--format", "json")
    if result.returncode != 0:
        raise RuntimeError(f"docker compose ps failed: {shorten(result.text, 120)}")
    return parse_ps(result.stdout)


def detector_restarts():
    """(RestartCount, status) of the detector container, or None when it cannot be read."""
    ids = compose_service(DETECTOR_SERVICE, "ps", "-q", DETECTOR_SERVICE).stdout.split()
    if not ids:
        return None
    out = run(["docker", "inspect", "--format", "{{.RestartCount}} {{.State.Status}}", ids[0]], DOCKER_TIMEOUT_S).stdout.split()
    if len(out) != 2:
        return None
    return int(out[0]), out[1]


# -- the background load (same approach as scripts/chaos_check.py) --------------------------------

def load_filters():
    return ["--filter", f"name=^/{LOAD_CONTAINER}$",
            "--filter", f"label=com.docker.compose.project={PROJECT_NAME}",
            "--filter", f"label=com.docker.compose.service={LOAD_SERVICE}",
            "--filter", "label=com.docker.compose.oneoff=True"]


def load_ids():
    return run(["docker", "ps", "-a", "-q", *load_filters()], DOCKER_TIMEOUT_S).stdout.split()


def load_status():
    ids = load_ids()
    if not ids:
        return "missing"
    out = run(["docker", "inspect", "--format", "{{.State.Status}}", ids[0]], DOCKER_TIMEOUT_S).stdout.strip()
    return "running" if out in ("running", "restarting") else "exited"


def remove_load():
    ids = load_ids()
    if not ids:
        return False
    result = run(["docker", "rm", "-f", ids[0]], DOCKER_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(f"docker rm -f failed: {shorten(result.text, 120)}")
    return True


def start_load(duration_s):
    env = {"USERS": str(LOAD_USERS), "SPAWN_RATE": str(LOAD_SPAWN_RATE), "DURATION": f"{int(duration_s)}s"}
    cmd = compose_base() + ["--profile", "load", "run", "-d", "--no-deps", "--name", LOAD_CONTAINER, LOAD_SERVICE]
    result = run(cmd, LOAD_START_TIMEOUT_S, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"the background load did not start: {shorten(result.text, 200)}")
    return env


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

class Check:
    def __init__(self, catalogue, plan, quick, skip_nodata):
        self.catalogue = catalogue
        self.plan = plan
        self.quick = quick
        self.skip_nodata = skip_nodata
        self.settle_s = QUICK_SETTLE_S if quick else SETTLE_S
        self.healthy_s = QUICK_HEALTHY_S if quick else HEALTHY_S
        self.idle_s = 60 * (QUICK_IDLE_MIN if quick else IDLE_MIN)
        self.load_s = 60 * (QUICK_LOAD_MIN if quick else LOAD_MIN)
        self.after_ok_s = QUICK_AFTER_OK_S if quick else AFTER_OK_S
        self.load_duration_s = int(1.3 * estimate_run_s(plan, quick, skip_nodata)) + 300
        self.load_started = False
        self.stopped = set()                 # prometheus / loki while they are stopped by this script
        self.children = []                   # chaos.py run processes (ended on Ctrl+C)
        self.restarts_at_start = None
        self.rules = []
        self.known = {}                      # incident snapshot at the end of the last step
        self.quiet_since = None              # `recovered` ts of the last fault (UTC)
        self.last_resolved = None            # resolved_at of the last incident (UTC)
        self.reopen_within_s = 120

    # -- preflight ---------------------------------------------------------------------------
    def preflight(self):
        section("Preflight")
        ok, detail = wait_ready(f"{PROMETHEUS}/-/ready", PROMETHEUS_READY_ATTEMPTS, PROMETHEUS_READY_DELAY_S)
        if not record("Prometheus is ready", ok, detail):
            say("    This check needs: docker compose --profile observability --profile detector up -d --build --wait")
            raise Abort("Prometheus is not reachable")
        ok, detail = wait_ready(f"{LOKI}/ready", LOKI_READY_ATTEMPTS, LOKI_READY_DELAY_S)
        if not record("Loki is ready", ok, detail):
            say(f"    Loki /ready did not answer 200 within about {LOKI_READY_ATTEMPTS * LOKI_READY_DELAY_S} s. Look at:")
            say("    docker compose --profile observability logs loki --tail 50")
            raise Abort("Loki is not ready")
        ok, detail = wait_ready(f"{DETECTOR}/health", DETECTOR_READY_ATTEMPTS, DETECTOR_READY_DELAY_S)
        if not record("detector /health answers 200", ok, detail):
            say("    docker compose --profile detector logs detector --tail 50")
            raise Abort("the detector is not running (docker compose --profile observability --profile detector up -d --build --wait)")
        ok, detail = wait_ready(f"{DETECTOR}/ready", DETECTOR_READY_ATTEMPTS, DETECTOR_READY_DELAY_S)
        if not record("detector /ready answers 200 (the evaluation loop finished a cycle)", ok, detail):
            say("    docker compose --profile detector logs detector --tail 50")
            raise Abort("the detector is not ready")

        rows = compose_ps()
        problems = running_problems(rows, M1_SERVICES + OBSERVABILITY_SERVICES)
        if not record("11 containers are running (6 M1 + 5 observability)", not problems, ", ".join(problems)):
            raise Abort("the stack is not running")
        problems = stack_problems(rows, M1_SERVICES + [DETECTOR_SERVICE])
        if not record("the 6 M1 containers and the detector are healthy", not problems, ", ".join(problems)):
            raise Abort("a container is not healthy (repair with: python chaos/chaos.py clear --all)")
        self.restarts_at_start = detector_restarts()
        if self.restarts_at_start is None:
            warn("the RestartCount of the detector cannot be read: that check is skipped")

        up, _ = self.prometheus_value('up{job="detector"}')
        ok = up == 1
        if not ok:      # the scrape config may have just been reloaded
            up, _ = wait_until(lambda: self.prometheus_value('up{job="detector"}')[0] == 1, 30, 5)
            ok = bool(up)
        if not record("Prometheus target detector is up", ok, "" if ok else 'up{job="detector"} is not 1'):
            say("    Reload the Prometheus configuration: docker compose --profile observability kill -s SIGHUP prometheus")
            raise Abort("Prometheus does not scrape the detector")

        status = self.wait_sources_ok()
        self.reopen_within_s = int(status["settings"].get("reopen_within_s", 120))
        self.rules = api("/internal/v1/rules")["rules"]
        self.wait_no_open_incident()
        result = chaos("status")
        if not record('chaos.py status says "no active faults"', result.returncode == 0
                      and result.stdout.strip() == "no active faults", f"exit code {result.returncode}"):
            show_output(result.text)
            raise Abort("a fault is still recorded as active: run 'python chaos/chaos.py clear --all' first")
        self.clock_check()
        self.known = incident_snapshot(get_incidents())
        say(f"    {len(self.known)} earlier incident(s) in the store are ignored; the detector reopens an incident that "
            f"resolved less than {self.reopen_within_s} s ago")
        left = remove_load()
        record("no leftover background load container", True, "removed one from an earlier run" if left else "")

    @staticmethod
    def prometheus_value(query):
        try:
            vector = prom_vector(query)
        except ApiError as exc:
            return None, str(exc)
        return (vector[0][1] if vector else None), ""

    def wait_sources_ok(self):
        def ready():
            status = get_status()
            return status if not sources_not_ok(status) else None
        try:
            status, seconds = wait_until(ready, SOURCES_OK_WAIT_S, 5)
        except ApiError as exc:
            record("both data sources of the detector are ok", False, str(exc))
            raise Abort("the detector status cannot be read")
        if status:
            record("both data sources of the detector are ok", True,
                   "prometheus ok, loki ok" + (f" after waiting {seconds:.0f} s (warm-up)" if seconds > 1 else ""))
            return status
        bad = sources_not_ok(get_status())
        record("both data sources of the detector are ok", False, ", ".join(bad) + f" after {SOURCES_OK_WAIT_S} s")
        say("    docker compose --profile detector logs detector --tail 50")
        raise Abort("a data source of the detector is not ok")

    def wait_no_open_incident(self):
        status = get_status()
        if not status.get("open_incident_id"):
            record("no incident is open", True)
            return
        open_id = status["open_incident_id"]
        say(f"    {open_id} is still open (an earlier run?): waiting up to {OPEN_INCIDENT_WAIT_S} s for it to resolve ...")
        gone, seconds = wait_until(lambda: not get_status().get("open_incident_id"), OPEN_INCIDENT_WAIT_S, 5)
        if not record(f"the open incident {open_id} resolves within {OPEN_INCIDENT_WAIT_S} s", bool(gone), f"{seconds:.0f} s"):
            say("    It did not resolve. Look at: docker compose --profile detector logs detector --tail 50")
            say("    and at http://127.0.0.1:9100/internal/v1/incidents/" + open_id + ". If a fault is still active, run: python chaos/chaos.py clear --all")
            raise Abort(f"{open_id} is still open")

    def clock_check(self):
        status, _, headers = http_get(DETECTOR + "/health", 5)
        skew = skew_from_date(headers.get("Date"), utcnow()) if status == 200 else None
        if skew is None:
            warn("the clock of the detector container cannot be compared with this host (no Date header)")
        elif abs(skew) > CLOCK_WARN_S:
            warn(f"the clock of the detector container differs from this host by {skew:+.1f} s", "delays below are affected")
        else:
            say(f"    clock of the detector container minus this host: {skew:+.1f} s (HTTP Date header, 1 s resolution)")

    # -- the background load -----------------------------------------------------------------
    def start_background_load(self):
        env = start_load(self.load_duration_s)
        self.load_started = True
        record("background load started", True, f"USERS={env['USERS']}, SPAWN_RATE={env['SPAWN_RATE']}, DURATION={env['DURATION']}, "
                                                f"container {LOAD_CONTAINER}")
        time.sleep(5)
        status = load_status()
        if not record("background load is running after 5 s", status == "running", status):
            say(f"    See: docker logs --tail 20 {LOAD_CONTAINER}")
            raise Abort("the background load does not run")

    def ensure_background_load(self):
        if load_status() == "running":
            return
        warn("the background load had stopped: it is started again")
        remove_load()
        self.start_background_load()
        self.quiet_since = utcnow()

    # -- the baseline ------------------------------------------------------------------------
    def healthy_for(self, seconds, max_wait_s=300):
        """Wait until the detector sources have been ok for `seconds` in a row (the timer restarts when one is not)."""
        say(f"waiting until the stack has been healthy for {seconds} s ...")
        good_since = time.monotonic()
        start = good_since
        while True:
            try:
                bad = sources_not_ok(get_status())
            except ApiError:
                bad = ["detector status unreadable"]
            now = time.monotonic()
            if bad:
                good_since = now
            if now - good_since >= seconds:
                break
            if now - start > max_wait_s + seconds:
                record(f"the stack was healthy for {seconds} s", False, ", ".join(bad))
                raise Abort("the stack did not become healthy")
            time.sleep(POLL_S)
        rows = compose_ps()
        problems = stack_problems(rows, M1_SERVICES + [DETECTOR_SERVICE])
        if not record(f"the stack has been healthy for {seconds} s", not problems, ", ".join(problems)):
            raise Abort("a container is not healthy")

    def baseline(self):
        section("Baseline (no false positive)")
        self.healthy_for(self.healthy_s)
        self.phase("idle", self.idle_s, with_load=False)
        self.phase("10 users", self.load_s, with_load=True)

    def phase(self, name, seconds, with_load):
        if with_load:
            self.start_background_load()
        label = f"baseline {name}"
        before = incident_snapshot(get_incidents())
        started, live = time.monotonic(), []
        say(f"{label}: {seconds} s ({seconds / 60:.1f} min){' with ' + str(LOAD_USERS) + ' load-test users' if with_load else ', no load'} ...")
        while time.monotonic() - started < seconds:
            try:
                status = get_status()
                for r in status["rules"]:
                    for s in r["subjects"]:
                        if s["state"] != "ok":
                            live.append(f"+{time.monotonic() - started:.0f} s {r['rule_id']} {s['subject'] or '-'} {s['state']}")
                bad = sources_not_ok(status)
                if bad:
                    live.append(f"+{time.monotonic() - started:.0f} s source {', '.join(bad)}")
            except ApiError as exc:
                live.append(f"+{time.monotonic() - started:.0f} s {exc}")
            time.sleep(POLL_S)
        elapsed = time.monotonic() - started
        baseline_rows.append((label, elapsed))
        window = max(10, int(elapsed))
        try:
            max_state = prom_vector(f"max_over_time(detector_rule_state[{window}s])")
            min_state = prom_vector(f"min_over_time(detector_rule_state[{window}s])")
            max_value = prom_vector(f"max_over_time(detector_rule_value[{window}s])")
        except ApiError as exc:
            record(f"{label}: the detector metrics can be read from Prometheus", False, str(exc))
            return
        expected_series = sum(len(r["subjects"]) for r in get_status()["rules"])
        record(f"{label}: Prometheus holds detector_rule_state for {len(max_state)} rule/subject series", len(max_state) >= expected_series,
               f"expected at least {expected_series}, window {window} s")
        firing, no_data = state_problems(max_state, min_state)
        record(f"{label}: no rule fired", not firing, ", ".join(firing))
        record(f"{label}: no rule reported no_data", not no_data, ", ".join(no_data))
        changes = snapshot_changes(before, incident_snapshot(get_incidents()))
        record(f"{label}: no new incident", not changes, "; ".join(changes))
        record(f"{label}: both data sources stayed ok", not [l for l in live if " source " in l], "; ".join(l for l in live if " source " in l)[:160])
        if live:
            say("    seen while polling: " + "; ".join(live[:6]) + (" ..." if len(live) > 6 else ""))
        say(f"    highest value per rule and subject over the last {window} s (detector_rule_value through Prometheus):")
        for line in format_table(["rule", "name", "subject", "highest", "of threshold", "condition"], value_rows(max_value, self.rules)):
            say("      " + line)
        self.known = incident_snapshot(get_incidents())

    # -- one fault ---------------------------------------------------------------------------
    def wait_before_fault(self):
        wait = settle_wait_s(utcnow(), self.quiet_since, self.settle_s, self.last_resolved, self.reopen_within_s)
        if wait > 0:
            say(f"waiting {int(math.ceil(wait))} s: {self.settle_s} s after the last recovery and {self.reopen_within_s + REOPEN_MARGIN_S} s "
                f"after the last incident resolved (an opening symptom within {self.reopen_within_s} s would reopen it) ...")
            time.sleep(wait)

    def check_fault(self, fid):
        exp = EXPECTATIONS[fid]
        fault = self.catalogue[fid]
        target = fault["default_target"]
        section(f"{fid} {fault['name']} (target {target})")
        row = {"fault": fid, "limit_s": exp["limit_s"], "ok": False}
        summary_rows.append(row)
        self.ensure_background_load()
        self.wait_before_fault()

        now = incident_snapshot(get_incidents())
        gap = snapshot_changes(self.known, now)
        record(f"{fid} no incident appeared or changed since the last step", not gap, "; ".join(gap))
        before = now
        restarts_before = detector_restarts()
        offset = GROUND_TRUTH_PATH.stat().st_size if GROUND_TRUTH_PATH.exists() else 0
        hold = hold_for(exp["limit_s"], self.quick)

        health = {"polls": 0, "bad": []}
        injected = {}

        def on_line(stamp, text):
            hit = parse_injected_line(text)
            if hit and "returned" not in injected:
                injected.update(returned=stamp, fault=hit[0], target=hit[1], run_id=hit[2])
                say(f"    inject returned (run id {hit[2]}); the fault is held for {hold} s")

        def poll():
            health["polls"] += 1
            status = http_get(DETECTOR + "/health", 4)[0]
            if status != 200:
                health["bad"].append(status)

        command = [sys.executable, str(CHAOS_PY), "run", fid, "--warmup", "0", "--hold", str(hold), "--cooldown", "0"]
        say(f"python chaos/chaos.py run {fid} --warmup 0 --hold {hold} --cooldown 0")
        code, lines = stream(command, on_line, poll, HEALTH_POLL_S, hold + 400, started=self.children)
        self.children.clear()
        if not record(f"{fid} chaos.py run", code == 0 and "returned" in injected,
                      f"exit code {code}" + ("" if "returned" in injected else "; the 'injected ...' line did not appear")):
            show_output("\n".join(lines))
            chaos("clear", "--all")
            return

        raw = GROUND_TRUTH_PATH.read_bytes()[offset:] if GROUND_TRUTH_PATH.exists() else b""
        events, bad = parse_event_lines(raw)
        mine = run_events(events, injected["run_id"])
        problems = run_problems(mine, fid, target) if not bad else [f"bad ground-truth line(s): {bad}"]
        if not record(f"{fid} ground truth: inject, clear, recovered in file order, one run id, no late flag", not problems,
                      "; ".join(problems)):
            return
        inject_ts, recovered_ts = parse_ts(mine[0]["ts"]), parse_ts(mine[2]["ts"])
        reference = injected["returned"]
        self.quiet_since = recovered_ts

        record(f"{fid} detector /health stayed 200 during the fault", not health["bad"],
               f"{health['polls']} polls" + (f"; answers: {health['bad']}" if health["bad"] else ""))
        restarts_after = detector_restarts()
        if restarts_before is not None and restarts_after is not None:
            record(f"{fid} the detector did not restart", restarts_after == restarts_before,
                   f"RestartCount {restarts_before[0]} -> {restarts_after[0]}, state {restarts_after[1]}")

        self.wait_for_incident(before, recovered_ts)
        incidents = get_incidents()
        after = incident_snapshot(incidents)
        new, reopened = incidents_for_run(incidents, before)
        record(f"{fid} exactly one new incident for the run, none reopened", len(new) == 1 and not reopened,
               f"{len(new)} new ({', '.join(i['incident_id'] for i in new) or 'none'})"
               + (f"; reopened: {', '.join(reopened)}" if reopened else ""))
        self.known = after
        if not new:
            return
        incident = new[0]
        checks = incident_checks(incident, exp, reference, inject_ts, recovered_ts)
        for name, ok, detail in checks:
            record(f"{fid} {name}", ok, detail)
        m = measurements(incident, reference, inject_ts, recovered_ts)
        say(f"    measured: {m['incident_id']} opened {m['delay_s']:.1f} s after the end of the inject "
            f"({m['delay_from_inject_ts_s']:.1f} s after the inject ts), first symptom {m['first_symptom_s']:.1f} s after it, "
            + (f"resolved {m['resolve_s']:.1f} s after the recovered event, " if m["resolve_s"] is not None else "not resolved, ")
            + f"{m['symptoms']} symptoms ({' '.join(m['rules'])}), title: {incident['title']}")
        row.update(m)
        row["ok"] = all(ok for _, ok, _ in checks) and len(new) == 1 and not reopened and not health["bad"]
        if incident.get("resolved_at"):
            self.last_resolved = parse_ts(incident["resolved_at"])

    def wait_for_incident(self, before, recovered_ts):
        """Wait until the run's incident is resolved: at most RESOLVE_LIMIT_S after the recovered event."""
        deadline = recovered_ts + timedelta(seconds=RESOLVE_LIMIT_S + 5)
        say(f"waiting for the incident to resolve (limit {RESOLVE_LIMIT_S} s after the recovered event) ...")

        def done():
            try:
                new, _ = incidents_for_run(get_incidents(), before)
            except ApiError:
                return None
            return new[0] if new and new[0]["status"] == "resolved" else None

        remaining = max(1.0, seconds_between(deadline, utcnow()))
        incident, _ = wait_until(done, remaining, POLL_S)
        return incident

    # -- the no-data step --------------------------------------------------------------------
    def restart_service(self, service):
        """Start a stopped service again and wait until it answers. Returns (ok, detail); never raises, because it runs in
        finally blocks and a failed restart must not hide the first error."""
        try:
            if service == "prometheus":
                result = compose_service("prometheus", "up", "-d", "--wait", "prometheus", timeout=COMPOSE_UP_TIMEOUT_S)
                ready, detail = wait_ready(f"{PROMETHEUS}/-/ready", 12, 5)
            else:
                result = compose_service("loki", "up", "-d", "loki", timeout=COMPOSE_UP_TIMEOUT_S)
                ready, detail = wait_ready(f"{LOKI}/ready", LOKI_RESTART_READY_ATTEMPTS, LOKI_READY_DELAY_S)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if result.returncode != 0:
            show_output(result.text)
            return False, f"docker compose exit code {result.returncode}"
        if ready:
            self.stopped.discard(service)
        return ready, detail

    def nodata(self, service):
        section(f"No data: {service} stopped for {OUTAGE_S} s")
        other = "loki" if service == "prometheus" else "prometheus"
        status = get_status()
        if not record(f"{service} is ok before the outage", status["sources"][service]["state"] == "ok", status["sources"][service]["state"]):
            raise Abort(f"{service} is not ok")
        before = incident_snapshot(get_incidents())
        stop_time = None
        outage_problems = []
        try:
            result = compose_service(service, "stop", service)
            self.stopped.add(service)
            stop_time = time.monotonic()
            if not record(f"{service} stopped (docker compose --profile observability stop {service})", result.returncode == 0,
                          f"exit code {result.returncode}"):
                show_output(result.text)
                raise Abort(f"{service} could not be stopped")

            def reported():
                try:
                    return get_status()["sources"][service]["state"] == "no_data"
                except ApiError:
                    return False
            ok, seconds = wait_until(reported, NODATA_DETECT_S, 2)
            record(f"the detector reports {service} no_data within {NODATA_DETECT_S} s", bool(ok), f"{seconds:.0f} s")

            while time.monotonic() - stop_time < OUTAGE_S:
                time.sleep(POLL_S)
                try:
                    status = get_status()
                except ApiError as exc:
                    outage_problems.append(str(exc))
                    continue
                outage_problems += [f"{p}" for p in rule_state_problems(status, service, "no_data")
                                    if time.monotonic() - stop_time > NODATA_DETECT_S]
                outage_problems += other_source_problems(status, service)
                outage_problems += [f"/ready answered {s}" for s in [http_get(DETECTOR + "/ready", 4)[0]] if s != 200]
                outage_problems += [f"/health answered {s}" for s in [http_get(DETECTOR + "/health", 4)[0]] if s != 200]
            record(f"during the outage the rules of {service} are no_data, the others ok, /ready and /health answer 200",
                   not outage_problems, "; ".join(sorted(set(outage_problems)))[:200])
            changes = snapshot_changes(before, incident_snapshot(get_incidents()))
            record(f"no incident was created or resolved during the {service} outage", not changes, "; ".join(changes))
        finally:
            up, detail = self.restart_service(service)
            record(f"{service} is running again", up, detail)

        # after the restart: warming_up, then ok
        states = []
        warmup_s = int(get_status()["settings"].get("warmup_s", 30))
        started = time.monotonic()

        def reached_ok():
            try:
                state = get_status()["sources"][service]["state"]
            except ApiError:
                return False
            if not states or states[-1] != state:
                states.append(state)
            return state == "ok"
        ok, seconds = wait_until(reached_ok, warmup_s + 90, 2)
        problems, notes = warmup_path(states)
        record(f"after the restart the status of {service} goes through warming_up to ok", bool(ok) and not problems,
               " -> ".join(states) + f", ok after {seconds:.0f} s" + ("; " + "; ".join(problems) if problems else ""))
        for note in notes:
            warn(f"{service}: {note}")
        say(f"    waiting {self.after_ok_s} s: no new incident may appear ...")
        end = time.monotonic() + self.after_ok_s
        stray = []
        while time.monotonic() < end:
            time.sleep(POLL_S)
            try:
                stray += snapshot_changes(before, incident_snapshot(get_incidents()))
            except ApiError as exc:
                stray.append(str(exc))
        record(f"{self.after_ok_s} s after {service} returned there is still no new incident", not stray, "; ".join(sorted(set(stray)))[:200])
        record(f"both data sources are ok again after the {service} test", not sources_not_ok(get_status()),
               ", ".join(sources_not_ok(get_status())))
        self.known = incident_snapshot(get_incidents())

    # -- finish ------------------------------------------------------------------------------
    def finish(self):
        section("Finish")
        removed = remove_load()
        self.load_started = False
        record("background load removed", True, "" if removed else "it had already ended")
        result = chaos("status")
        record('chaos.py status says "no active faults"', result.stdout.strip() == "no active faults", f"exit code {result.returncode}")
        after = detector_restarts()
        if self.restarts_at_start is not None and after is not None:
            record("the detector never restarted during the check", after == self.restarts_at_start,
                   f"RestartCount {self.restarts_at_start[0]} -> {after[0]}, state {after[1]}")
        status = get_status()
        record("no incident is open at the end", not status.get("open_incident_id"), str(status.get("open_incident_id") or ""))
        record("both data sources are ok at the end", not sources_not_ok(status), ", ".join(sources_not_ok(status)))


def emergency_cleanup(check):
    """After Ctrl+C, a failed preflight or an error: end the chaos.py child, remove the load, clear every fault and start
    Prometheus and Loki again if this script stopped them. Ctrl+C is ignored meanwhile."""
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        say("\ncleaning up: ending chaos.py, removing the background load, clear --all, starting Prometheus and Loki if stopped ...")
        for child in list(check.children):
            if child.poll() is None:
                child.terminate()
        try:
            remove_load()
        except RuntimeError as exc:
            say(f"  could not remove the background load: {exc}")
        result = chaos("clear", "--all")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        say(f"  clear --all: exit code {result.returncode}" + (f", last line: {lines[-1]}" if lines else ""))
        for service in sorted(check.stopped):
            up, detail = check.restart_service(service)
            say(f"  {service} started again: {'ok' if up else 'FAILED (' + detail + ')'}")
    finally:
        signal.signal(signal.SIGINT, old)


def print_final():
    if baseline_rows:
        say("\nBaseline durations: " + ", ".join(f"{name.replace('baseline ', '')} {secs:.0f} s" for name, secs in baseline_rows))
    if summary_rows:
        say("\nDetection per fault (delay counted from the end of the inject; limits of docs/CHAOS.md):")
        for line in final_table(summary_rows):
            say("  " + line)


def summary(complete=True):
    print_final()
    failed = [name for name, ok, _ in results if not ok]
    say(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"   ({len(warnings)} warning(s))" if warnings else ""))
    if failed:
        say("FAILED: " + "; ".join(failed))
        say("Look at: docker compose --profile detector logs detector --tail 50")
        say("Repair:  python chaos/chaos.py clear --all   (and: docker compose --profile observability up -d --wait prometheus loki)")
        return 1
    if not complete:
        say("DETECTOR CHECK INCOMPLETE: it stopped before the last step")
        return 1
    say("DETECTOR CHECK PASSED")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="SentinelOps M4 detector check")
    parser.add_argument("--only", nargs="+", metavar="ID", help="check only these faults, for example F10 F13")
    parser.add_argument("--quick", action="store_true", help="shorter waits (baseline 1 + 2 min, healthy 30 s, hold limit + 25 s)")
    parser.add_argument("--skip-nodata", action="store_true", help="leave out the Prometheus / Loki no-data step")
    args = parser.parse_args(argv)

    plan = list(FAULT_ORDER)
    if args.only:
        wanted = [name.upper() for name in args.only]
        unknown = [name for name in wanted if name not in EXPECTATIONS]
        if unknown:
            print(f"unknown fault(s): {', '.join(unknown)}. Faults in this version: {', '.join(FAULT_ORDER)}", file=sys.stderr)
            return 2
        plan = [fid for fid in FAULT_ORDER if fid in wanted]
    try:
        catalogue = {fault["id"]: fault for fault in json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))}
        for fid in plan:
            if catalogue[fid]["default_target"] != EXPECTATIONS[fid]["target"]:
                print(f"{fid}: the default target in the catalogue is {catalogue[fid]['default_target']}, this script expects "
                      f"{EXPECTATIONS[fid]['target']}", file=sys.stderr)
                return 1
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot read {CATALOGUE_PATH}: {exc}", file=sys.stderr)
        return 1

    check = Check(catalogue, plan, args.quick, args.skip_nodata)
    minutes = int(math.ceil(estimate_run_s(plan, args.quick, args.skip_nodata) / 60))
    say(f"SentinelOps M4 detector check: {', '.join(plan)}{'' if args.skip_nodata else ' + no-data step'} | "
        f"{'quick' if args.quick else 'normal'} mode | baseline {check.idle_s // 60} min idle + {check.load_s // 60} min with {LOAD_USERS} users | "
        f"at most about {minutes} minutes")
    say("delays are counted from the END of the inject (the moment chaos.py printed 'injected ...'); the inject ts of the ground truth is about 2 s earlier")
    try:
        check.preflight()
        check.baseline()
        for fid in plan:
            check.check_fault(fid)
        if not args.skip_nodata:
            for source in ("prometheus", "loki"):
                check.nodata(source)
        check.finish()
        return summary()
    except Abort as exc:
        say(f"\nstopped: {exc}")
        emergency_cleanup(check)
        summary(complete=False)
        return 1
    except KeyboardInterrupt:
        say("\ninterrupted")
        emergency_cleanup(check)
        summary(complete=False)
        return 130
    except Exception as exc:                          # a bug or an unexpected error: never leave a fault or a stopped service behind
        say(f"\nunexpected error: {type(exc).__name__}: {exc}")
        emergency_cleanup(check)
        summary(complete=False)
        return 1


if __name__ == "__main__":
    sys.exit(main())
