"""Data model of the detector. No logic about queries, thresholds or SQL lives here.

The only thing a detector produces is an Observation. The correlator turns observations
into symptoms and incidents. A later detector (EWMA / z-score) only has to implement the
Detector interface at the bottom of this file; nothing in this file has to change for it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

SCHEMA_VERSION = 1  # schema_version of the incident format (docs/DETECTOR.md, CP5)

# Observation states.
FIRING = "firing"
OK = "ok"
NO_DATA = "no_data"
STATES = (FIRING, OK, NO_DATA)

SOURCES = ("prometheus", "loki")
COMPARATORS = (">", "==")
SEVERITIES = ("low", "medium", "high")
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}
ROLES = ("root_signal", "effect", "dependency_effect", "load", "evidence")

OPEN = "open"
RESOLVED = "resolved"
INCIDENT_STATUSES = (OPEN, RESOLVED)


# ---------------------------------------------------------------- time helpers

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    """UTC ISO 8601 with milliseconds and a trailing Z, like the ground truth of M3."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    """ISO 8601 text -> aware UTC datetime. A time without a zone is taken as UTC (never as local time)."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ----------------------------------------------------------------- observation

@dataclass(frozen=True)
class Observation:
    """One evaluation result of one rule for one subject (a service name, or None).

    state: "firing" (condition true), "ok" (condition false) or "no_data" (could not be
    evaluated: missing data is a status, never a verdict, decision D59).
    detail: short text. Convention used by the correlator: a dependency is written as
    "dependency=<name>" (rule R06), so that it can become a candidate service.
    """
    rule_id: str
    detector: str
    source: str
    subject: Optional[str]
    state: str
    value: Optional[float]
    threshold: Optional[float]
    comparator: str
    ts: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"bad observation state {self.state!r}")
        if self.source not in SOURCES:
            raise ValueError(f"bad observation source {self.source!r}")
        if self.comparator not in COMPARATORS:
            raise ValueError(f"bad comparator {self.comparator!r}")


@dataclass(frozen=True)
class RuleMeta:
    """What the correlator needs to know about a rule. No query, no PromQL."""
    rule_id: str
    name: str
    severity: str
    role: str
    opens_incident: bool
    for_s: float


# ------------------------------------------------------------ symptom, incident

@dataclass
class Symptom:
    seq: int                      # database key; symptom_id is "SYM-" plus this number
    rule_id: str
    detector: str
    source: str
    service: Optional[str]
    role: str
    severity: str
    opens_incident: bool
    first_seen: datetime          # first firing evaluation of the hold (the delay is visible)
    last_seen: datetime           # last firing evaluation
    active: bool
    flaps: int
    comparator: str
    threshold: Optional[float]
    peak_value: Optional[float]
    last_value: Optional[float]
    detail: str
    dependencies: list = field(default_factory=list)  # internal, not in the API format

    @property
    def symptom_id(self) -> str:
        return f"SYM-{self.seq:05d}"

    def to_api(self) -> dict:
        return {
            "symptom_id": self.symptom_id,
            "rule_id": self.rule_id,
            "detector": self.detector,
            "source": self.source,
            "service": self.service,
            "role": self.role,
            "severity": self.severity,
            "opens_incident": self.opens_incident,
            "first_seen": iso_z(self.first_seen),
            "last_seen": iso_z(self.last_seen),
            "active": self.active,
            "flaps": self.flaps,
            "comparator": self.comparator,
            "threshold": self.threshold,
            "peak_value": self.peak_value,
            "last_value": self.last_value,
            "detail": self.detail,
        }


@dataclass
class DataGap:
    source: str
    start: datetime
    end: Optional[datetime] = None   # None while the gap is open

    def to_api(self) -> dict:
        return {"source": self.source, "start": iso_z(self.start),
                "end": iso_z(self.end) if self.end is not None else None}


@dataclass
class Incident:
    seq: int                      # database key; incident_id is "INC-" plus this number
    status: str
    opened_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    reopen_count: int = 0
    severity: str = "low"
    title: str = ""
    primary_service: Optional[str] = None
    candidate_services: list = field(default_factory=list)
    affected_services: list = field(default_factory=list)
    symptoms: list = field(default_factory=list)
    data_gaps: list = field(default_factory=list)

    @property
    def incident_id(self) -> str:
        return f"INC-{self.seq:04d}"

    def to_api(self) -> dict:
        """The fixed incident format, schema_version 1 (field order is part of the contract)."""
        return {
            "incident_id": self.incident_id,
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "opened_at": iso_z(self.opened_at),
            "updated_at": iso_z(self.updated_at),
            "resolved_at": iso_z(self.resolved_at) if self.resolved_at is not None else None,
            "reopen_count": self.reopen_count,
            "severity": self.severity,
            "title": self.title,
            "primary_service": self.primary_service,
            "candidate_services": list(self.candidate_services),
            "affected_services": list(self.affected_services),
            "symptoms": [s.to_api() for s in self.symptoms],
            "data_gaps": [g.to_api() for g in self.data_gaps],
        }


# -------------------------------------------------------------------- interface

class Detector(ABC):
    """A detector has a name and evaluate(now) -> list of Observation.

    Tier 1 registers one detector (ThresholdDetector). EWMA / z-score would be a second
    class with name "ewma"; the loop, the correlator, the store and the API stay as they are.
    """
    name: str = ""

    @abstractmethod
    def evaluate(self, now: datetime) -> list:
        """Return one Observation per rule and subject. `now` is a timezone-aware UTC datetime."""
