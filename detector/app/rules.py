"""rules.json: loading and validation. An invalid file stops the detector with a clear error.

The file has a fixed schema (below). The validator also enforces the MARGIN RULE of
docs/DETECTOR.md, so a threshold that is too close to normal behaviour, or above the first
reading of the fault it must catch, cannot be loaded by accident:

  - a threshold must be at least 1.5 times the highest baseline value (comparator ">")
  - a threshold must not be above the first M3 fault reading (comparator ">")
  - margin_ratio = threshold / baseline_max, rounded to 3 decimals; it has no meaning when
    the baseline is 0 (empty or 0 %) or for a state rule ("==" such as up == 0): there it is null
  - the only way to leave baseline_max or fault_value_min empty is a derived_from text that
    starts with "PENDING" (a number that has not been measured yet)
"""
from __future__ import annotations

import json
import math
import re
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .model import COMPARATORS, ROLES, SEVERITIES, SOURCES, RuleMeta

RULES_SCHEMA_VERSION = 1
MIN_MARGIN = 1.5
ON_MISSING = ("zero", "ok")
PROVENANCE_KEYS = ("baseline_max", "fault_value_min", "margin_ratio", "derived_from")
REQUIRED_KEYS = ("id", "name", "detector", "source", "query", "subject_label", "comparator",
                 "threshold", "for_s", "severity", "role", "opens_incident", "on_missing",
                 "expected_subjects", "provenance", "description")
OPTIONAL_KEYS = ("guard", "min_traffic")
ID_PATTERN = re.compile(r"^R\d{2,}$")


class RulesError(Exception):
    """rules.json is invalid. str(error) lists every problem found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("invalid rules file:\n  - " + "\n  - ".join(self.problems))


@dataclass(frozen=True)
class Provenance:
    baseline_max: Optional[float]
    fault_value_min: Optional[float]
    margin_ratio: Optional[float]
    derived_from: str

    @property
    def pending(self) -> bool:
        return self.derived_from.startswith("PENDING")


@dataclass(frozen=True)
class MinTraffic:
    """Rate guard: the rule is evaluated only while the traffic query is at least `min`;
    below that the observation is "ok" (one failed request at 0.2 requests/s would read 100 %)."""
    query: str
    min: float
    subject_label: Optional[str]


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    detector: str
    source: str
    query: str
    subject_label: Optional[str]
    comparator: str
    threshold: float
    for_s: float
    severity: str
    role: str
    opens_incident: bool
    on_missing: str
    expected_subjects: Optional[tuple]
    guard: Optional[str]
    min_traffic: Optional[MinTraffic]
    provenance: Provenance
    description: str

    def meta(self) -> RuleMeta:
        return RuleMeta(self.id, self.name, self.severity, self.role, self.opens_incident, self.for_s)

    def to_dict(self) -> dict:
        """The rule as the API shows it (/internal/v1/rules)."""
        return {
            "id": self.id, "name": self.name, "detector": self.detector, "source": self.source,
            "query": self.query, "subject_label": self.subject_label,
            "comparator": self.comparator, "threshold": self.threshold, "for_s": self.for_s,
            "severity": self.severity, "role": self.role, "opens_incident": self.opens_incident,
            "on_missing": self.on_missing,
            "expected_subjects": list(self.expected_subjects) if self.expected_subjects is not None else None,
            "guard": self.guard,
            "min_traffic": ({"query": self.min_traffic.query, "min": self.min_traffic.min,
                             "subject_label": self.min_traffic.subject_label}
                            if self.min_traffic else None),
            "provenance": {"baseline_max": self.provenance.baseline_max,
                           "fault_value_min": self.provenance.fault_value_min,
                           "margin_ratio": self.provenance.margin_ratio,
                           "derived_from": self.provenance.derived_from},
            "description": self.description,
        }


# ------------------------------------------------------------------ validation

def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_text(x) -> bool:
    return isinstance(x, str) and x.strip() != ""


def _validate_provenance(where, comparator, threshold, prov, problems) -> Optional[Provenance]:
    if not isinstance(prov, dict):
        problems.append(f"{where}: provenance must be an object")
        return None
    extra = sorted(set(prov) - set(PROVENANCE_KEYS))
    missing = [k for k in PROVENANCE_KEYS if k not in prov]
    if extra:
        problems.append(f"{where}: provenance has unknown keys {extra}")
    if missing:
        problems.append(f"{where}: provenance is missing {missing}")
    if extra or missing:
        return None
    ok = True
    for key in ("baseline_max", "fault_value_min", "margin_ratio"):
        if prov[key] is not None and not _is_number(prov[key]):
            problems.append(f"{where}: provenance.{key} must be a number or null")
            ok = False
    if not _is_text(prov["derived_from"]):
        problems.append(f"{where}: provenance.derived_from must be a non-empty text")
        ok = False
    if not ok:
        return None
    p = Provenance(prov["baseline_max"], prov["fault_value_min"], prov["margin_ratio"], prov["derived_from"])
    n = len(problems)
    if (p.baseline_max is None or p.fault_value_min is None) and not p.pending:
        problems.append(f"{where}: baseline_max and fault_value_min are required "
                        f"(only derived_from starting with 'PENDING' may leave them empty)")
    if _is_number(threshold):
        if comparator == "==":
            if p.margin_ratio is not None:
                problems.append(f"{where}: margin_ratio must be null for a state rule (comparator '==')")
        else:
            if p.baseline_max is not None and p.baseline_max > 0:
                if threshold < MIN_MARGIN * p.baseline_max - 1e-9:
                    problems.append(f"{where}: margin rule violated: threshold {threshold} is below "
                                    f"{MIN_MARGIN} x baseline_max {p.baseline_max}")
                if p.margin_ratio is None:
                    problems.append(f"{where}: margin_ratio is required when baseline_max is above 0")
                elif abs(round(threshold / p.baseline_max, 3) - p.margin_ratio) > 0.0015:
                    problems.append(f"{where}: margin_ratio {p.margin_ratio} does not match "
                                    f"threshold / baseline_max = {round(threshold / p.baseline_max, 3)}")
            elif p.baseline_max is not None and p.margin_ratio is not None:
                problems.append(f"{where}: margin_ratio must be null when baseline_max is 0")
            if p.baseline_max is None and p.margin_ratio is not None:
                problems.append(f"{where}: margin_ratio must be null while baseline_max is not measured")
            if p.fault_value_min is not None and threshold > p.fault_value_min + 1e-9:
                problems.append(f"{where}: margin rule violated: threshold {threshold} is above the first "
                                f"fault reading {p.fault_value_min}")
    return p if len(problems) == n else None


def _validate_rule(index, raw, seen_ids, problems) -> Optional[Rule]:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be an object")
        return None
    if _is_text(raw.get("id")):
        where = f"rules[{index}] ({raw['id']})"
    n = len(problems)

    unknown = sorted(set(raw) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS))
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if unknown:
        problems.append(f"{where}: unknown fields {unknown}")
    if missing:
        problems.append(f"{where}: missing fields {missing}")
        return None

    rid = raw["id"]
    if not (isinstance(rid, str) and ID_PATTERN.match(rid)):
        problems.append(f"{where}: id must look like R01")
    elif rid in seen_ids:
        problems.append(f"{where}: duplicate id")
    else:
        seen_ids.add(rid)
    for key in ("name", "query", "description"):
        if not _is_text(raw[key]):
            problems.append(f"{where}: {key} must be a non-empty text")
    if raw["detector"] != "threshold":
        problems.append(f"{where}: detector must be 'threshold' in tier 1")
    if raw["source"] not in SOURCES:
        problems.append(f"{where}: source must be one of {list(SOURCES)}")
    if raw["subject_label"] is not None and not _is_text(raw["subject_label"]):
        problems.append(f"{where}: subject_label must be a label name or null")
    if raw["comparator"] not in COMPARATORS:
        problems.append(f"{where}: comparator must be one of {list(COMPARATORS)}")
    if not _is_number(raw["threshold"]):
        problems.append(f"{where}: threshold must be a number")
    if not (_is_number(raw["for_s"]) and raw["for_s"] >= 0):
        problems.append(f"{where}: for_s must be a number >= 0")
    if raw["severity"] not in SEVERITIES:
        problems.append(f"{where}: severity must be one of {list(SEVERITIES)}")
    if raw["role"] not in ROLES:
        problems.append(f"{where}: role must be one of {list(ROLES)}")
    if not isinstance(raw["opens_incident"], bool):
        problems.append(f"{where}: opens_incident must be true or false")
    elif raw["role"] == "evidence" and raw["opens_incident"]:
        problems.append(f"{where}: a rule with role 'evidence' must not open incidents")
    if raw["on_missing"] not in ON_MISSING:
        problems.append(f"{where}: on_missing must be one of {list(ON_MISSING)}")

    expected = raw["expected_subjects"]
    if expected is not None:
        if not (isinstance(expected, list) and expected and all(_is_text(s) for s in expected)):
            problems.append(f"{where}: expected_subjects must be a non-empty list of names or null")
        elif len(set(expected)) != len(expected):
            problems.append(f"{where}: expected_subjects has duplicates")
    if raw["on_missing"] == "zero":
        if expected is None:
            problems.append(f"{where}: on_missing 'zero' needs expected_subjects")
        if raw["subject_label"] is None:
            problems.append(f"{where}: on_missing 'zero' needs a subject_label")
    if expected is not None and raw["subject_label"] is None:
        problems.append(f"{where}: expected_subjects needs a subject_label")

    guard = raw.get("guard")
    if guard is not None and not _is_text(guard):
        problems.append(f"{where}: guard must be a query text or null")

    min_traffic = None
    mt = raw.get("min_traffic")
    if mt is not None:
        if not isinstance(mt, dict) or set(mt) != {"query", "min", "subject_label"}:
            problems.append(f"{where}: min_traffic must be null or an object with query, min, subject_label")
        elif not (_is_text(mt["query"]) and _is_number(mt["min"]) and mt["min"] > 0
                  and (mt["subject_label"] is None or _is_text(mt["subject_label"]))):
            problems.append(f"{where}: min_traffic needs a query, a number min > 0 and a subject_label or null")
        else:
            min_traffic = MinTraffic(mt["query"], float(mt["min"]), mt["subject_label"])

    prov = _validate_provenance(where, raw["comparator"], raw["threshold"], raw["provenance"], problems)
    if len(problems) != n or prov is None:
        return None
    return Rule(
        id=rid, name=raw["name"], detector=raw["detector"], source=raw["source"], query=raw["query"],
        subject_label=raw["subject_label"], comparator=raw["comparator"], threshold=float(raw["threshold"]),
        for_s=float(raw["for_s"]), severity=raw["severity"], role=raw["role"],
        opens_incident=raw["opens_incident"], on_missing=raw["on_missing"],
        expected_subjects=tuple(expected) if expected is not None else None,
        guard=guard, min_traffic=min_traffic, provenance=prov, description=raw["description"])


def parse_rules(document) -> list:
    """Validate a parsed JSON document and return the rules, or raise RulesError."""
    problems: list = []
    if not isinstance(document, dict):
        raise RulesError(["the file must contain a JSON object with schema_version and rules"])
    if document.get("schema_version") != RULES_SCHEMA_VERSION:
        problems.append(f"schema_version must be {RULES_SCHEMA_VERSION}")
    extra = sorted(set(document) - {"schema_version", "rules"})
    if extra:
        problems.append(f"unknown top-level fields {extra}")
    raw_rules = document.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        problems.append("rules must be a non-empty list")
        raise RulesError(problems)
    seen: set = set()
    rules = []
    for i, raw in enumerate(raw_rules):
        rule = _validate_rule(i, raw, seen, problems)
        if rule is not None:
            rules.append(rule)
    if problems:
        raise RulesError(problems)
    return rules


def load_rules(path) -> list:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")   # tolerates the BOM that Windows editors add
    except OSError as exc:
        raise RulesError([f"cannot read {path}: {exc}"]) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RulesError([f"{path} is not valid JSON: {exc}"]) from exc
    return parse_rules(document)


# ------------------------------------------------------------ provenance table

def _num(x) -> str:
    return "-" if x is None else f"{x:g}"


def provenance_table(rules) -> str:
    header = f"{'Id':<4} {'Name':<19} {'Rule':<9} {'for_s':>5} {'baseline_max':>12} {'fault_min':>10} {'margin':>7}"
    lines = [header, "-" * len(header)]
    for r in rules:
        cond = f"{r.comparator} {r.threshold:g}"
        lines.append(f"{r.id:<4} {r.name:<19} {cond:<9} {r.for_s:>5g} {_num(r.provenance.baseline_max):>12} "
                     f"{_num(r.provenance.fault_value_min):>10} {_num(r.provenance.margin_ratio):>7}")
    lines.append("")
    lines.append("Sources of the numbers:")
    for r in rules:
        lines.append(textwrap.fill(f"{r.id} {r.name}: {r.provenance.derived_from}", width=100,
                                   subsequent_indent="    "))
    return "\n".join(lines)


def main(argv=None) -> int:
    """python -m app.rules [path]   (with PYTHONPATH=detector from the repository root)"""
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent / "rules" / "rules.json"
    try:
        rules = load_rules(path)
    except RulesError as exc:
        print(f"RULES INVALID: {path}\n{exc}", file=sys.stderr)
        return 1
    print(f"rules file {path}: {len(rules)} rules valid")
    print()
    print(provenance_table(rules))
    pending = [r.id for r in rules if r.provenance.pending]
    print()
    print("pending provenance: " + (", ".join(pending) if pending else "none"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
