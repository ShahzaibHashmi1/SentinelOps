"""Plain data types and small helpers shared by the evidence modules (no I/O except load_json_file)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
TRIGGERS = ("opened", "refresh", "resolved")
SOURCE_STATES = ("ok", "no_data", "off", "warming_up")
CHANGE_KINDS = ("recreated", "started", "stopped", "restarted", "config_changed", "health_changed")
CHANGE_FIELDS = ("created", "status", "restart_count", "config_hash", "health")
BUNDLE_MAX_BYTES = 64 * 1024
BUNDLE_KEYS = (
    "bundle_id", "schema_version", "incident_id", "trigger", "built_at", "build_ms", "window",
    "sources", "detector_hint", "services", "graph", "suspects", "no_single_root", "confidence",
    "data_gaps", "truncated",
)
SIGNAL_ENTRY_KEYS = ("name", "unit", "window_max", "baseline_max", "anomalous", "first_anomalous_at")
CONTAINER_KEYS = ("state", "started_at", "restart_count", "oom_killed", "health", "limits")
ROLES = ("root_signal", "effect", "dependency_effect", "load", "evidence")


class ModelError(ValueError):
    """Raised for malformed input data."""


# ---------------------------------------------------------------- time

def parse_ts(text: str) -> datetime:
    """Parse an ISO 8601 timestamp (Z or offset). A timestamp without zone counts as UTC."""
    if not isinstance(text, str) or not text:
        raise ModelError("timestamp must be a non-empty string")
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ModelError(f"bad timestamp: {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    """UTC, milliseconds, trailing Z (the format used by the whole project)."""
    if dt.tzinfo is None:
        raise ModelError("naive datetime")
    u = dt.astimezone(timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


def epoch_to_dt(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def load_json_file(path: str) -> Any:
    """Read a JSON file (UTF-8, BOM accepted); duplicate object keys are an error."""

    def no_dupes(pairs):
        seen = {}
        for key, value in pairs:
            if key in seen:
                raise ModelError(f"duplicate key {key!r} in {path}")
            seen[key] = value
        return seen

    with open(path, "r", encoding="utf-8-sig") as handle:
        try:
            return json.loads(handle.read(), object_pairs_hook=no_dupes)
        except json.JSONDecodeError as exc:
            raise ModelError(f"{path}: invalid JSON: {exc}") from exc


# ---------------------------------------------------------------- evidence types

@dataclass(frozen=True)
class Point:
    ts: float  # epoch seconds
    value: float


@dataclass(frozen=True)
class SignalResult:
    """Outcome of one signal of one service. `name` is `base` or `base:label` (grouped signals)."""

    base: str
    label: str | None
    unit: str
    kind: str  # high_is_bad | down | restarts
    window_max: float | None
    baseline_max: float | None
    anomalous: bool
    first_anomalous_at: datetime | None
    threshold: float | None = None
    ratio: float | None = None  # window_max / threshold for ranking strength (None for down signals)

    @property
    def name(self) -> str:
        return self.base if self.label is None else f"{self.base}:{self.label}"

    def to_entry(self) -> dict:
        return {
            "name": self.name,
            "unit": self.unit,
            "window_max": None if self.window_max is None else round(self.window_max, 4),
            "baseline_max": None if self.baseline_max is None else round(self.baseline_max, 4),
            "anomalous": self.anomalous,
            "first_anomalous_at": None if self.first_anomalous_at is None else fmt_ts(self.first_anomalous_at),
        }


@dataclass(frozen=True)
class LogEvidence:
    state: str  # ok | no_data
    window_count: int | None = None
    baseline_count: int | None = None
    top_events: tuple = ()  # ((event, count), ...)
    samples: tuple = ()
    request_ids: tuple = ()

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "window_count": self.window_count,
            "baseline_count": self.baseline_count,
            "top_events": [{"event": e, "count": c} for e, c in self.top_events],
            "samples": list(self.samples),
            "request_ids": list(self.request_ids),
        }


@dataclass(frozen=True)
class ContainerObservation:
    """The allow-listed fields of one container inspection; nothing else is ever kept (D69)."""

    service: str
    image: str | None = None
    created: str | None = None
    status: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    oom_killed: bool | None = None
    health: str | None = None
    restart_count: int | None = None
    restart_policy: str | None = None
    memory_limit: int | None = None
    nano_cpus: int | None = None
    config_hash: str | None = None

    def to_section(self) -> dict:
        return {
            "state": self.status,
            "started_at": self.started_at,
            "restart_count": self.restart_count,
            "oom_killed": self.oom_killed,
            "health": self.health,
            "limits": {"memory_bytes": self.memory_limit, "nano_cpus": self.nano_cpus},
        }


@dataclass(frozen=True)
class ChangeRecord:
    service: str
    kind: str
    ts: datetime
    old: dict = field(default_factory=dict)
    new: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in CHANGE_KINDS:
            raise ModelError(f"unknown change kind {self.kind!r}")
        for side in (self.old, self.new):
            extra = set(side) - set(CHANGE_FIELDS)
            if extra:
                raise ModelError(f"change fields outside the allow-list: {sorted(extra)}")
            digest = side.get("config_hash")
            if digest is not None and len(str(digest)) > 12:
                raise ModelError("config_hash must be shortened to 12 characters")

    def to_dict(self) -> dict:
        return {"service": self.service, "kind": self.kind, "ts": fmt_ts(self.ts), "old": dict(self.old), "new": dict(self.new)}


@dataclass(frozen=True)
class ServiceEvidence:
    name: str
    tier: str  # app | infra
    signals: tuple = ()  # tuple[SignalResult]
    logs: LogEvidence | None = None
    container: ContainerObservation | None = None
    changes: tuple = ()  # tuple[ChangeRecord]
    gaps: tuple = ()  # ((signal name, error text), ...) signals that returned no_data

    def anomalous_signals(self) -> list:
        return [s for s in self.signals if s.anomalous]

    def is_anomalous(self) -> bool:
        return any(s.anomalous for s in self.signals)

    def first_anomalous_at(self) -> datetime | None:
        times = [s.first_anomalous_at for s in self.signals if s.anomalous and s.first_anomalous_at]
        return min(times) if times else None


@dataclass(frozen=True)
class HintSymptom:
    rule_id: str
    service: str | None
    role: str
    first_seen: datetime


@dataclass(frozen=True)
class DetectorHint:
    primary_service: str | None
    candidate_services: tuple = ()
    symptoms: tuple = ()  # tuple[HintSymptom]

    def to_dict(self) -> dict:
        return {
            "primary_service": self.primary_service,
            "candidate_services": list(self.candidate_services),
            "symptoms": [
                {"rule_id": s.rule_id, "service": s.service, "role": s.role, "first_seen": fmt_ts(s.first_seen)}
                for s in self.symptoms
            ],
        }


@dataclass(frozen=True)
class Suspect:
    rank: int
    service: str
    score: float
    components: dict
    reasons: tuple

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "service": self.service,
            "score": self.score,
            "components": dict(self.components),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class RankingResult:
    suspects: tuple
    no_single_root: bool
    confidence: float


# ---------------------------------------------------------------- bundle validation

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_ID = re.compile(r"^EVD-\d{5,}$")


def _is_ts(value) -> bool:
    return isinstance(value, str) and bool(_ISO_Z.match(value))


def validate_bundle(bundle: dict, source_names: tuple | None = None) -> list[str]:
    """Return a list of problems (empty list = valid). Keys and their order are part of the contract.

    The names of the five sources come from signals.json; when `source_names` is given they must match exactly.
    """
    problems: list[str] = []
    if not isinstance(bundle, dict):
        return ["bundle is not an object"]
    if tuple(bundle.keys()) != BUNDLE_KEYS:
        problems.append(f"top-level keys differ from the contract: {list(bundle.keys())}")
        return problems
    if not isinstance(bundle["bundle_id"], str) or not _ID.match(bundle["bundle_id"]):
        problems.append("bundle_id format")
    if bundle["schema_version"] != SCHEMA_VERSION:
        problems.append("schema_version")
    if not isinstance(bundle["incident_id"], str) or not bundle["incident_id"]:
        problems.append("incident_id")
    if bundle["trigger"] not in TRIGGERS:
        problems.append("trigger")
    if not _is_ts(bundle["built_at"]):
        problems.append("built_at format")
    if not isinstance(bundle["build_ms"], int) or isinstance(bundle["build_ms"], bool) or bundle["build_ms"] < 0:
        problems.append("build_ms")
    window = bundle["window"]
    if not isinstance(window, dict) or tuple(window.keys()) != ("start", "end", "baseline_start", "baseline_end", "baseline_clean"):
        problems.append("window keys")
    else:
        for key in ("start", "end", "baseline_start", "baseline_end"):
            if not _is_ts(window[key]):
                problems.append(f"window.{key} format")
        if not isinstance(window["baseline_clean"], bool):
            problems.append("window.baseline_clean")
        elif all(_is_ts(window[k]) for k in ("start", "end", "baseline_start", "baseline_end")):
            if not window["start"] <= window["end"]:
                problems.append("window.start after window.end")
            if not window["baseline_start"] <= window["baseline_end"]:
                problems.append("baseline_start after baseline_end")
    sources = bundle["sources"]
    if not isinstance(sources, dict) or not sources:
        problems.append("sources keys")
    elif source_names is not None and tuple(sources.keys()) != tuple(source_names):
        problems.append("sources keys")
    else:
        for name, item in sources.items():
            if not isinstance(item, dict) or set(item) != {"state", "detail"} or item["state"] not in SOURCE_STATES:
                problems.append(f"sources.{name}")
    hint = bundle["detector_hint"]
    if not isinstance(hint, dict) or tuple(hint.keys()) != ("primary_service", "candidate_services", "symptoms"):
        problems.append("detector_hint keys")
    else:
        for sym in hint["symptoms"]:
            if set(sym) != {"rule_id", "service", "role", "first_seen"} or sym["role"] not in ROLES or not _is_ts(sym["first_seen"]):
                problems.append("detector_hint.symptoms entry")
                break
    services = bundle["services"]
    if not isinstance(services, dict) or not services:
        problems.append("services")
    else:
        for name, item in services.items():
            if not isinstance(item, dict) or tuple(item.keys()) != ("tier", "signals", "logs", "container", "changes"):
                problems.append(f"services.{name} keys")
                continue
            if item["tier"] not in ("app", "infra"):
                problems.append(f"services.{name}.tier")
            for sig in item["signals"]:
                if tuple(sig.keys()) != SIGNAL_ENTRY_KEYS:
                    problems.append(f"services.{name}.signals entry keys")
                    break
                if sig["first_anomalous_at"] is not None and not _is_ts(sig["first_anomalous_at"]):
                    problems.append(f"services.{name}.signals first_anomalous_at")
                    break
            if item["container"] is not None and tuple(item["container"].keys()) != CONTAINER_KEYS:
                problems.append(f"services.{name}.container keys")
            for ch in item["changes"]:
                if set(ch) != {"service", "kind", "ts", "old", "new"} or ch["kind"] not in CHANGE_KINDS:
                    problems.append(f"services.{name}.changes entry")
                    break
    graph = bundle["graph"]
    if not isinstance(graph, dict) or tuple(graph.keys()) != ("nodes", "edges", "depth", "shared_dependencies"):
        problems.append("graph keys")
    suspects = bundle["suspects"]
    if not isinstance(suspects, list):
        problems.append("suspects")
    else:
        for index, sus in enumerate(suspects, start=1):
            if set(sus) != {"rank", "service", "score", "components", "reasons"} or sus["rank"] != index:
                problems.append("suspects entry")
                break
            if not 0 <= sus["score"] <= 100:
                problems.append("suspect score range")
                break
    if not isinstance(bundle["no_single_root"], bool):
        problems.append("no_single_root")
    if not isinstance(bundle["confidence"], (int, float)) or isinstance(bundle["confidence"], bool):
        problems.append("confidence")
    if not isinstance(bundle["data_gaps"], list):
        problems.append("data_gaps")
    if not isinstance(bundle["truncated"], list):
        problems.append("truncated")
    return problems
