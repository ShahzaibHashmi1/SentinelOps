#!/usr/bin/env python
"""SentinelOps M3: fault injection CLI (standard library only).

Usage (PowerShell, repo root):
    python chaos/chaos.py list
    python chaos/chaos.py status
    python chaos/chaos.py inject F10
    python chaos/chaos.py clear F10          (or: clear --all)
    python chaos/chaos.py run F10 --warmup 60 --hold 90 --cooldown 60

What this file does: argument parsing, the safety checks, the active-fault state and the
ground-truth file. The faults themselves (the docker commands) live in faults.py.

Files written at run time (both git-ignored):
    chaos/data/active.json         faults that are active right now (small, rewritten)
    chaos/data/ground_truth.jsonl  append-only log of every inject / clear / recovered / aborted event
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import faults
from faults import Docker, FaultError

CHAOS_DIR = Path(__file__).resolve().parent
REPO_ROOT = faults.REPO_ROOT
CATALOGUE_PATH = CHAOS_DIR / "catalogue.json"
DATA_DIR = CHAOS_DIR / "data"
STATE_PATH = DATA_DIR / "active.json"
GROUND_TRUTH_PATH = DATA_DIR / "ground_truth.jsonl"
ENV_PATH = REPO_ROOT / ".env"

# ---------------------------------------------------------------------------
# Allowed values (the catalogue is validated against these)
# ---------------------------------------------------------------------------

CATEGORIES = ["Resource", "Configuration", "Dependency", "Load"]
ROOT_CAUSE_TYPES = ["cpu_exhaustion", "memory_exhaustion", "misconfiguration", "db_auth_failure",
                    "database_unavailable", "db_lock_contention", "cache_unavailable", "capacity_limit"]
ACTIONS = ["restart_service", "scale_service", "rollback_deployment", "revert_config",
           "restore_network_path", "rotate_logs", "terminate_blocking_query"]
SYMPTOM_SOURCES = ["prometheus", "loki"]
SERVICES = faults.M1_SERVICES
HARD_CAP_S = 600                  # no fault, warm-up, hold or cool-down may exceed this
DEFAULT_HOLD_S = 60               # `run` hold time for faults that have no duration of their own
RUN_END_MARGIN_S = 30             # `run`: a self-ending fault lasts hold + this, so that clear (not the timer) ends it
TARGET_PLACEHOLDER = "$target"    # in expected_root_cause.service and in symptom queries

REQUIRED_FIELDS = ["id", "name", "category", "description", "default_target", "allowed_targets",
                   "default_duration_s", "max_duration_s", "params", "expected_root_cause",
                   "expected_action", "acceptable_actions", "expected_symptoms"]
CONDITION_RE = re.compile(r"^(>=|<=|==|>|<)\s*-?\d+(\.\d+)?$")
FAULT_ID_RE = re.compile(r"^F\d{2}$")


class ValidationError(Exception):
    """The request was refused by a safety check or a bad argument (exit code 2)."""


class CatalogueError(Exception):
    """catalogue.json is invalid (exit code 1)."""


class StateError(Exception):
    """chaos/data/active.json cannot be read (exit code 1)."""


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_catalogue(data):
    """Return a list of error strings (empty list = valid).

    expected_symptoms: each item is {source, query, condition, within_s}. `condition` is
    "<op> <number>" with op one of > >= < <= ==. The query returns one value; $target in the
    query is replaced by the real target. An empty query result counts as 0 (scripts/chaos_check.py).
    """
    if not isinstance(data, list) or not data:
        return ["the catalogue must be a non-empty JSON list"]
    errors = []
    seen = set()
    for index, fault in enumerate(data):
        if not isinstance(fault, dict):
            errors.append(f"entry {index}: not a JSON object")
            continue
        fid = str(fault.get("id", f"entry {index}"))

        def err(message, fid=fid):
            errors.append(f"{fid}: {message}")

        missing = [name for name in REQUIRED_FIELDS if name not in fault]
        if missing:
            err("missing fields: " + ", ".join(missing))
            continue
        if not FAULT_ID_RE.match(fid):
            err("id must look like F01")
        if fid in seen:
            err("duplicate id")
        seen.add(fid)
        for text_field in ("name", "description"):
            if not isinstance(fault[text_field], str) or not fault[text_field].strip():
                err(f"{text_field} must be a non-empty string")
        if fault["category"] not in CATEGORIES:
            err(f"category must be one of {CATEGORIES}")

        allowed = fault["allowed_targets"]
        if not isinstance(allowed, list) or not allowed or any(t not in SERVICES for t in allowed):
            err(f"allowed_targets must be a non-empty list of services {SERVICES}")
            allowed = []
        if fault["default_target"] not in allowed:
            err("default_target must be one of allowed_targets")

        max_s = fault["max_duration_s"]
        if not _is_int(max_s) or not 1 <= max_s <= HARD_CAP_S:
            err(f"max_duration_s must be an integer from 1 to {HARD_CAP_S}")
            max_s = HARD_CAP_S
        default_s = fault["default_duration_s"]
        if default_s is not None and (not _is_int(default_s) or not 1 <= default_s <= max_s):
            err("default_duration_s must be null (stays until clear) or an integer from 1 to max_duration_s")

        params = fault["params"]
        if not isinstance(params, list):
            err("params must be a list")
            params = []
        names = set()
        for param in params:
            if not isinstance(param, dict) or not isinstance(param.get("name"), str) or not param["name"]:
                err("every param needs a name")
                continue
            pname = param["name"]
            if pname in names:
                err(f"duplicate param {pname}")
            names.add(pname)
            if not all(_is_number(param.get(k)) for k in ("default", "min", "max")):
                err(f"param {pname}: default, min and max must be numbers")
            elif not param["min"] <= param["default"] <= param["max"]:
                err(f"param {pname}: need min <= default <= max")

        cause = fault["expected_root_cause"]
        if not isinstance(cause, dict) or "service" not in cause or "type" not in cause:
            err("expected_root_cause needs service and type")
        else:
            if cause["service"] is not None and cause["service"] != TARGET_PLACEHOLDER \
                    and cause["service"] not in SERVICES:
                err(f"expected_root_cause.service must be null, '{TARGET_PLACEHOLDER}' or one of {SERVICES}")
            if cause["type"] not in ROOT_CAUSE_TYPES:
                err(f"expected_root_cause.type must be one of {ROOT_CAUSE_TYPES}")

        if fault["expected_action"] not in ACTIONS:
            err(f"expected_action must be one of {ACTIONS}")
        acceptable = fault["acceptable_actions"]
        if not isinstance(acceptable, list) or not acceptable or any(a not in ACTIONS for a in acceptable):
            err("acceptable_actions must be a non-empty list of allowed actions")
        elif fault["expected_action"] not in acceptable:
            err("acceptable_actions must contain expected_action")

        symptoms = fault["expected_symptoms"]
        if not isinstance(symptoms, list) or not symptoms:
            err("expected_symptoms must be a non-empty list")
            symptoms = []
        for number, symptom in enumerate(symptoms, start=1):
            label = f"expected_symptoms[{number}]"
            if not isinstance(symptom, dict):
                err(f"{label} must be an object")
                continue
            if not isinstance(symptom.get("query"), str) or not symptom["query"].strip():
                err(f"{label}.query must be a non-empty string")
            if not isinstance(symptom.get("condition"), str) or not CONDITION_RE.match(symptom["condition"]):
                err(f"{label}.condition must look like '> 5' (operators > >= < <= ==)")
            if not _is_int(symptom.get("within_s")) or not 1 <= symptom["within_s"] <= HARD_CAP_S:
                err(f"{label}.within_s must be an integer from 1 to {HARD_CAP_S}")
            if "source" in symptom and symptom["source"] not in SYMPTOM_SOURCES:
                err(f"{label}.source must be one of {SYMPTOM_SOURCES}")
    return errors


def load_catalogue(path=CATALOGUE_PATH):
    """Read and validate the catalogue. Returns {fault_id: fault} in file order."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogueError(f"cannot read {path}: {exc}")
    errors = validate_catalogue(data)
    if errors:
        raise CatalogueError("catalogue.json is invalid:\n  " + "\n  ".join(errors))
    return {fault["id"]: fault for fault in data}


# ---------------------------------------------------------------------------
# Argument validation (safety checks before anything touches docker)
# ---------------------------------------------------------------------------

@dataclass
class Request:
    fault_id: str
    target: str
    duration_s: object          # int, or None for faults that stay until clear
    params: dict = field(default_factory=dict)


def parse_param_args(items):
    """['a=1', 'b=2'] -> {'a': '1', 'b': '2'}."""
    parsed = {}
    for item in items or []:
        if "=" not in item or item.startswith("="):
            raise ValidationError(f"bad --param '{item}': use name=value")
        name, value = item.split("=", 1)
        name = name.strip()
        if name in parsed:
            raise ValidationError(f"--param {name} given twice")
        parsed[name] = value.strip()
    return parsed


def validate_request(catalogue, fault_id, target=None, duration=None, param_items=(),
                     implemented=None, planned=None):
    """Check everything about an inject/run request. Raises ValidationError, returns a Request."""
    fid = str(fault_id).upper()
    if fid not in catalogue:
        raise ValidationError(f"unknown fault '{fault_id}'. Known faults: {', '.join(catalogue)}")
    fault = catalogue[fid]

    resolved_target = target if target is not None else fault["default_target"]
    if resolved_target not in fault["allowed_targets"]:
        raise ValidationError(f"target '{resolved_target}' is not allowed for {fid} "
                              f"(allowed: {', '.join(fault['allowed_targets'])})")

    resolved_duration = fault["default_duration_s"]
    if duration is not None:
        if fault["default_duration_s"] is None:
            raise ValidationError(f"{fid} has no duration of its own: it stays until 'clear {fid}' "
                                  f"(use 'run --hold' for a timed experiment)")
        if duration < 1:
            raise ValidationError("duration must be at least 1 second")
        if duration > fault["max_duration_s"]:
            raise ValidationError(f"duration {duration} s is above the cap of {fault['max_duration_s']} s for {fid}")
        resolved_duration = duration

    given = parse_param_args(param_items)
    known = {p["name"]: p for p in fault["params"]}
    for name in given:
        if name not in known:
            allowed = ", ".join(known) if known else "none"
            raise ValidationError(f"unknown parameter '{name}' for {fid} (parameters: {allowed})")
    values = {}
    for name, spec in known.items():
        if name not in given:
            values[name] = spec["default"]
            continue
        text = given[name]
        try:
            value = int(text) if isinstance(spec["default"], int) else float(text)
        except ValueError:
            kind = "a whole number" if isinstance(spec["default"], int) else "a number"
            raise ValidationError(f"parameter {name}={text} must be {kind}")
        if not spec["min"] <= value <= spec["max"]:
            raise ValidationError(f"parameter {name}={value} is outside the allowed range "
                                  f"{spec['min']} to {spec['max']}")
        values[name] = value
    # Checked last on purpose: a bad target or duration is reported even for a fault that is not built yet.
    if implemented is not None and fid not in implemented:
        when = (planned or {}).get(fid, "a later checkpoint")
        raise ValidationError(f"{fid} is in the catalogue but is not built yet (planned for {when})")
    return Request(fid, resolved_target, resolved_duration, values)


def validate_run_options(fault, warmup, hold, cooldown):
    """Check the timing options of `run`. Returns (warmup, hold, cooldown) with the default hold filled in."""
    for name, value in (("warmup", warmup), ("cooldown", cooldown)):
        if not 0 <= value <= HARD_CAP_S:
            raise ValidationError(f"--{name} must be between 0 and {HARD_CAP_S} seconds")
    if hold is None:
        hold = fault["default_duration_s"] or DEFAULT_HOLD_S
    if not 1 <= hold <= fault["max_duration_s"]:
        raise ValidationError(f"--hold must be between 1 and {fault['max_duration_s']} seconds")
    return warmup, hold, cooldown


# ---------------------------------------------------------------------------
# Secrets: never printed, never written
# ---------------------------------------------------------------------------

_URL_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)")
_KEY_VALUE_RE = re.compile(r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN))(\s*[=:]\s*)\S+")


def mask_patterns(text):
    """Mask only known password patterns: passwords inside URLs and NAME_PASSWORD=value pairs.

    Used for the ground-truth file. It never replaces a plain word, so a secret that happens to
    equal a printed word (for example "postgres") cannot corrupt the target field.
    """
    text = _URL_PASSWORD_RE.sub(r"\1***\2", text)
    return _KEY_VALUE_RE.sub(r"\1\2***", text)


def mask_secrets(text, secrets=()):
    """For printed output: replace every known secret value, then the known password patterns."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "***")
    return mask_patterns(text)


def mask_structure(value, mask):
    """Apply a text mask to every string value of a nested dict/list (keys are left alone)."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [mask_structure(item, mask) for item in value]
    if isinstance(value, dict):
        return {key: mask_structure(item, mask) for key, item in value.items()}
    return value


def load_env_secrets(path=ENV_PATH):
    """Values of the PASSWORD / SECRET / TOKEN entries in .env, only used to mask output."""
    secrets = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return secrets
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if re.search(r"(?i)(PASSWORD|SECRET|TOKEN)", key) and len(value) >= 3:
            secrets.append(value)
    return secrets


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def now_iso():
    """UTC time, ISO 8601 with milliseconds and a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def format_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def new_run_id(fault_id):
    return f"{fault_id}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Ground truth (append-only JSON lines) and active state
# ---------------------------------------------------------------------------

def resolve_root_cause(fault, target):
    cause = fault["expected_root_cause"]
    service = target if cause["service"] == TARGET_PLACEHOLDER else cause["service"]
    return {"service": service, "type": cause["type"]}


def make_event(event, record, fault, ts, **extra):
    """One ground-truth object. The key order here is the documented field order."""
    line = {
        "run_id": record["run_id"],
        "event": event,
        "fault_id": record["fault_id"],
        "category": fault["category"],
        "target": record["target"],
        "params": record["params"],
        "ts": ts,
        "expected_root_cause": resolve_root_cause(fault, record["target"]),
        "expected_action": fault["expected_action"],
        "acceptable_actions": fault["acceptable_actions"],
    }
    line.update(extra)            # recovery_s on "recovered", reason on "aborted"
    return line


def format_ground_truth_line(event, mask=mask_patterns):
    """One JSON object as one line ending in \\n. The mask is applied to the string values BEFORE
    serialising, so a masked value can never break the JSON."""
    return json.dumps(mask_structure(event, mask), ensure_ascii=False, separators=(",", ":")) + "\n"


def append_line(path, line):
    """Append in binary mode so Windows does not turn \\n into \\r\\n."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as handle:
        handle.write(line.encode("utf-8"))


def read_state(path=STATE_PATH):
    """{fault_id: record} of the active faults. A missing file means nothing is active."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        active = data["faults"]
        if not isinstance(active, dict):
            raise ValueError("'faults' is not an object")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"{path} cannot be read ({exc}). Run 'python chaos/chaos.py clear --all' "
                         f"and delete the file if the error stays.")
    return active


def write_state(active, path=STATE_PATH):
    """Write atomically: a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({"faults": active}, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, path)


def fault_status(record, running, process_counts=None, config_states=None, session_counts=None, load_states=None):
    """'active' or 'stale' (the effect is gone although the record still exists)."""
    gone = faults.effect_gone(record["fault_id"], record["target"], running, process_counts, config_states,
                              session_counts, load_states)
    return "stale" if gone else "active"


@dataclass
class Observed:
    """What docker says about the recorded faults. Anything that could not be read is left out of its dict."""
    running: set
    process_counts: dict        # F01/F02: processes with the marker
    config_states: dict         # F06/F08: faulted | clean | missing
    session_counts: dict        # F11: chaos-F11 sessions in postgres
    load_states: dict           # F18: running | exited | missing


def observe(dk, records, states=None):
    """Look at docker once for the given records. A value that cannot be read is never reported as stale."""
    states = states if states is not None else faults.service_states(dk.ps())
    running = faults.running_services(states)
    return Observed(running, faults.process_counts(dk, records, running), faults.config_states(dk, records),
                    faults.session_counts(dk, records, running), faults.load_states(dk, records))


def status_of(record, observed):
    return fault_status(record, observed.running, observed.process_counts, observed.config_states,
                        observed.session_counts, observed.load_states)


# ---------------------------------------------------------------------------
# Context and commands
# ---------------------------------------------------------------------------

class InterruptGuard:
    """While active, Ctrl+C is only counted: it does not raise KeyboardInterrupt.

    `run` uses it around the clear, so that pressing Ctrl+C again cannot stop the tool before the
    clear and recovered events are written. (docker children run in their own process group, see
    faults.default_runner, so they do not receive the console Ctrl+C either.)
    """

    def __init__(self, say):
        self.say = say
        self.count = 0
        self._old = None
        self._installed = False

    def _handler(self, _signum, _frame):
        self.count += 1
        if self.count == 1:
            self.say("Ctrl+C received while clearing: finishing the clear and the recording first ...")

    def __enter__(self):
        try:
            self._old = signal.signal(signal.SIGINT, self._handler)
            self._installed = True
        except ValueError:          # not the main thread: cannot install a handler
            pass
        return self

    def __exit__(self, *_exc):
        if self._installed:
            signal.signal(signal.SIGINT, self._old)
        return False


@dataclass
class Context:
    catalogue: dict
    dk: Docker
    say: object = print
    now: object = now_iso
    sleep: object = time.sleep
    state_path: Path = STATE_PATH
    truth_path: Path = GROUND_TRUTH_PATH
    mask: object = field(default=lambda text: text)      # printed output: known secret values + patterns
    mask_truth: object = field(default=mask_patterns)    # ground-truth file: patterns only


def build_context():
    secrets = load_env_secrets()

    def mask(text):
        return mask_secrets(text, secrets)

    def say(message=""):
        print(mask(str(message)), flush=True)

    return Context(catalogue=load_catalogue(), dk=Docker(echo=say, mask=mask), say=say, mask=mask,
                   mask_truth=mask_patterns)


def format_table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
             "  ".join("-" * w for w in widths)]
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def emit(ctx, event, record, ts, **extra):
    fault = ctx.catalogue[record["fault_id"]]
    line = format_ground_truth_line(make_event(event, record, fault, ts, **extra), ctx.mask_truth)
    append_line(ctx.truth_path, line)


def cmd_list(ctx, _args):
    rows = []
    for fault in ctx.catalogue.values():
        cause = resolve_root_cause(fault, fault["default_target"])
        built = "yes" if fault["id"] in faults.IMPLEMENTED else f"no ({faults.PLANNED.get(fault['id'], 'later')})"
        rows.append([fault["id"], fault["category"], fault["default_target"],
                     f"{cause['service'] or '(none)'} / {cause['type']}", fault["expected_action"], built])
    ctx.say(format_table(["ID", "Category", "Target", "Expected root cause", "Expected action", "Built"], rows))
    return 0


def effect_already_gone(ctx, record):
    """True when the effect of a recorded fault is already gone (a 'late' clear). Unknown counts as not gone."""
    try:
        observed = observe(ctx.dk, [record])
    except FaultError:
        return False
    return status_of(record, observed) == "stale"


def cmd_status(ctx, _args):
    state = read_state(ctx.state_path)
    if not state:
        ctx.say("no active faults")
        return 0
    try:
        observed = observe(ctx.dk, list(state.values()))
    except FaultError as exc:
        ctx.say(f"warning: cannot cross-check with docker ({exc}); showing the recorded state only")
        observed = None
    rows = []
    for fid, record in state.items():
        age = format_age((datetime.now(timezone.utc) - parse_iso(record["started_ts"])).total_seconds())
        status = status_of(record, observed) if observed is not None else "unknown"
        rows.append([fid, record["target"], record["run_id"], age, status])
    ctx.say(format_table(["ID", "Target", "Run id", "Age", "State"], rows))
    if any(row[4] == "stale" for row in rows):
        ctx.say("stale = the effect is gone (service recreated or restarted, or the fault's duration is over). "
                "Remove the record with: python chaos/chaos.py clear <ID>")
    return 0


def do_inject(ctx, request, mode="inject"):
    """Inject one fault: refuse unsafe requests, record state, run the docker action, record ground truth."""
    dk = ctx.dk
    fid = request.fault_id
    state = read_state(ctx.state_path)
    states = faults.service_states(dk.ps())
    if fid in state:
        record = state[fid]
        status = status_of(record, observe(dk, [record], states))
        if status == "stale":
            raise ValidationError(f"{fid} is recorded as active (run {record['run_id']}) but its effect is gone "
                                  f"(stale). Run first: python chaos/chaos.py clear {fid}")
        raise ValidationError(f"{fid} is already active (run {record['run_id']}). "
                              f"Run first: python chaos/chaos.py clear {fid}")
    bad = faults.not_healthy(states)
    if bad:
        raise ValidationError("the stack is not running and healthy: " + ", ".join(bad) +
                              ". Start it with 'docker compose --profile observability up -d --build --wait' "
                              "or repair it with 'python chaos/chaos.py clear --all'")

    ts = ctx.now()          # taken immediately before the first docker action of the fault
    record = {"run_id": new_run_id(fid), "fault_id": fid, "target": request.target,
              "params": request.params, "duration_s": request.duration_s,
              "started_ts": ts, "mode": mode, "inject_recorded": False}
    state[fid] = record
    write_state(state, ctx.state_path)          # written BEFORE the action, so a crash can still be cleared
    inject_fn, clear_fn = faults.IMPLEMENTED[fid]
    try:
        inject_fn(dk, {"target": request.target, "params": request.params, "duration_s": request.duration_s})
    except FaultError as exc:
        ctx.say(f"inject failed, undoing: {exc}")
        try:
            clear_fn(dk, record)
        except FaultError as undo_exc:
            ctx.say(f"undo failed too: {undo_exc}")
        state.pop(fid, None)
        write_state(state, ctx.state_path)
        raise FaultError(f"{fid} could not be injected (nothing was recorded in the ground truth)")
    # KeyboardInterrupt is deliberately not caught here: the record stays in active.json and
    # 'clear' (or the finally block of `run`) completes the experiment.
    emit(ctx, "inject", record, ts)
    record["inject_recorded"] = True
    state[fid] = record
    write_state(state, ctx.state_path)
    return record


def do_clear(ctx, record):
    """Undo one recorded fault. Returns (record, clear_ts, late). Leaves the record if the docker action fails.

    late = the effect was already gone before this clear (container restarted by hand, fault duration
    over, an earlier clear that was interrupted). The clear and recovered events then carry "late": true.
    """
    fid = record["fault_id"]
    if not record.get("inject_recorded"):
        # The injection was interrupted before its ground-truth line was written: write it now
        # (with the original timestamp) so that every experiment has an inject/clear pair.
        emit(ctx, "inject", record, record["started_ts"])
    late = effect_already_gone(ctx, record)
    _inject_fn, clear_fn = faults.IMPLEMENTED[fid]
    ts_clear = ctx.now()    # taken immediately before the first docker command that changes the stack
    clear_fn(ctx.dk, record)
    emit(ctx, "clear", record, ts_clear, **({"late": True} if late else {}))
    state = read_state(ctx.state_path)
    state.pop(fid, None)
    write_state(state, ctx.state_path)
    return record, ts_clear, late


def finish_recovery(ctx, cleared):
    """Wait for the whole stack to recover, then write one 'recovered' event per cleared fault."""
    ctx.say("waiting for all 6 containers to be healthy and the gateway to answer 200 ...")
    try:
        faults.wait_recovered(ctx.dk)
    except FaultError as exc:
        ctx.say(f"error: {exc}")
        return False
    ts_recovered = ctx.now()
    for record, ts_clear, late in cleared:
        seconds = (parse_iso(ts_recovered) - parse_iso(ts_clear)).total_seconds()
        emit(ctx, "recovered", record, ts_recovered, recovery_s=round(seconds, 1),
             **({"late": True} if late else {}))
        note = " (late clear: the effect was already gone)" if late else ""
        ctx.say(f"{record['fault_id']}: recovered {seconds:.1f} s after the clear{note}")
    return True


def cmd_inject(ctx, args):
    request = validate_request(ctx.catalogue, args.fault_id, args.target, args.duration, args.param,
                               implemented=faults.IMPLEMENTED, planned=faults.PLANNED)
    record = do_inject(ctx, request)
    ctx.say(f"injected {record['fault_id']} on {record['target']} (run id {record['run_id']})")
    if request.duration_s is None:
        ctx.say(f"it stays until: python chaos/chaos.py clear {record['fault_id']}")
    else:
        ctx.say(f"it ends by itself after {request.duration_s} s; end it earlier with: "
                f"python chaos/chaos.py clear {record['fault_id']}")
    return 0


def cmd_clear(ctx, args):
    if args.all == (args.fault_id is not None):
        raise ValidationError("give either a fault ID or --all (not both, not neither)")
    state = read_state(ctx.state_path)
    cleared = []
    failed = False

    if args.all:
        records = list(state.values())
    else:
        fid = args.fault_id.upper()
        if fid not in ctx.catalogue:
            raise ValidationError(f"unknown fault '{args.fault_id}'. Known faults: {', '.join(ctx.catalogue)}")
        if fid not in state:
            ctx.say(f"{fid} is not active: nothing to clear. "
                    f"(If the stack is still broken, run: python chaos/chaos.py clear --all)")
            return 0
        records = [state[fid]]

    for record in records:
        try:
            cleared.append(do_clear(ctx, record))
            ctx.say(f"cleared {record['fault_id']} (run id {record['run_id']})")
        except FaultError as exc:
            failed = True
            ctx.say(f"error while clearing {record['fault_id']}: {exc}")

    if args.all:
        try:
            # F06/F08: a service that still has a faulted environment is recreated from the base files
            targets = list(dict.fromkeys(t for fid in faults.CONFIG_FAULTS if fid in ctx.catalogue
                                         for t in ctx.catalogue[fid]["allowed_targets"]))
            for kind, target in faults.sweep_config_faults(ctx.dk, targets):
                ctx.say(f"recreated {target} from the base files (it still had the {kind} fault)")
        except FaultError as exc:
            failed = True
            ctx.say(f"error: {exc}")
        try:
            for name in faults.heal_stopped_services(ctx.dk):
                ctx.say(f"started stopped container: {name}")
        except FaultError as exc:
            failed = True
            ctx.say(f"error: {exc}")
        try:
            targets = {fid: ctx.catalogue[fid]["allowed_targets"] for fid in faults.PROCESS_FAULTS
                       if fid in ctx.catalogue}
            for fid, target in faults.sweep_process_faults(ctx.dk, targets):
                ctx.say(f"ended leftover {fid} processes in {target}")
        except FaultError as exc:
            ctx.say(f"warning: could not check for leftover fault processes: {exc}")
        try:
            sessions = faults.sweep_lock_sessions(ctx.dk)
            if sessions:
                ctx.say(f"ended {sessions} leftover F11 session(s) in postgres")
            if faults.sweep_load_container(ctx.dk):
                ctx.say(f"removed the leftover F18 load container {faults.LOAD_CONTAINER}")
        except FaultError as exc:
            ctx.say(f"warning: could not check for leftover F11/F18 effects: {exc}")

    if failed:
        return 1
    if not finish_recovery(ctx, cleared):
        return 1
    if args.all and not cleared:
        ctx.say("no active faults; the stack is healthy")
    return 0


def sleep_phase(ctx, label, seconds):
    if seconds > 0:
        ctx.say(f"{label}: {seconds} s ...")
        ctx.sleep(seconds)


def cmd_run(ctx, args):
    request = validate_request(ctx.catalogue, args.fault_id, args.target, args.duration, args.param,
                               implemented=faults.IMPLEMENTED, planned=faults.PLANNED)
    fault = ctx.catalogue[request.fault_id]
    warmup, hold, cooldown = validate_run_options(fault, args.warmup, args.hold, args.cooldown)
    fid = request.fault_id
    if fault["default_duration_s"] is not None and args.duration is None:
        # A self-ending fault must still be running when `clear` happens; otherwise the clear would be 'late'.
        request.duration_s = min(hold + RUN_END_MARGIN_S, fault["max_duration_s"])
    ctx.say(f"run {fid}: warm-up {warmup} s, hold {hold} s, cool-down {cooldown} s (Ctrl+C clears the fault)")

    aborted = False
    failure = None
    recovered_ok = True
    try:
        sleep_phase(ctx, "warm-up", warmup)
        record = do_inject(ctx, request, mode="run")
        ctx.say(f"injected {fid} on {record['target']} (run id {record['run_id']})")
        sleep_phase(ctx, "hold", hold)
    except KeyboardInterrupt:
        aborted = True
        ctx.say("interrupted: clearing the fault now (a second Ctrl+C is held back until the clear is recorded)")
    except (FaultError, ValidationError) as exc:
        failure = exc
    finally:
        guard = InterruptGuard(ctx.say)
        with guard:     # a second Ctrl+C must not stop the tool before clear and recovered are written
            # The state file is the source of truth: it also covers an inject that was interrupted half way.
            record = read_state(ctx.state_path).get(fid)
            if record is not None:
                if aborted:
                    emit(ctx, "aborted", record, ctx.now(), reason="interrupted by Ctrl+C")
                try:
                    cleared = [do_clear(ctx, record)]
                    recovered_ok = finish_recovery(ctx, cleared)
                except FaultError as exc:
                    ctx.say(f"error while clearing {fid}: {exc}")
                    recovered_ok = False
        interrupted_in_clear = guard.count > 0

    if failure is not None:
        raise failure
    if aborted or interrupted_in_clear:
        return 130
    if not recovered_ok:
        return 1
    sleep_phase(ctx, "cool-down", cooldown)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="python chaos/chaos.py",
                                     description="SentinelOps fault injection (module M3).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show the fault catalogue")
    sub.add_parser("status", help="show active faults")

    def add_fault_options(p):
        p.add_argument("fault_id", metavar="ID", help="fault id, for example F10")
        p.add_argument("--target", help="service to attack (default: the fault's default target)")
        p.add_argument("--duration", type=int, help="seconds, for faults that end by themselves")
        p.add_argument("--param", action="append", default=[], metavar="name=value",
                       help="fault parameter (repeatable)")

    p_inject = sub.add_parser("inject", help="inject one fault (returns immediately)")
    add_fault_options(p_inject)

    p_clear = sub.add_parser("clear", help="undo one fault, or all faults with --all")
    p_clear.add_argument("fault_id", metavar="ID", nargs="?", help="fault id")
    p_clear.add_argument("--all", action="store_true", help="return the whole stack to healthy")

    p_run = sub.add_parser("run", help="warm-up, inject, hold, clear, cool-down as one labelled experiment")
    add_fault_options(p_run)
    p_run.add_argument("--warmup", type=int, default=60, help="seconds before the injection (default 60)")
    p_run.add_argument("--hold", type=int, default=None, help="seconds the fault stays (default: its duration, else 60)")
    p_run.add_argument("--cooldown", type=int, default=60, help="seconds after recovery (default 60)")
    return parser


COMMANDS = {"list": cmd_list, "status": cmd_status, "inject": cmd_inject, "clear": cmd_clear, "run": cmd_run}


def main(argv=None, ctx=None):
    args = build_parser().parse_args(argv)
    try:
        ctx = ctx or build_context()
        return COMMANDS[args.command](ctx, args)
    except ValidationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (FaultError, CatalogueError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted. If a fault is active, run: python chaos/chaos.py clear --all", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
