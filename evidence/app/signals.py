"""Signals and windows configuration (evidence/config/signals.json): loader, validation, provenance table.

Every threshold carries a provenance block. A number is `documented` or `measured` (then it must keep the
1.5x margin over the baseline maximum and stay at or below the first fault reading) or `assumed` (reused
from another documented number, to be measured later). The table printed by `python -m app.signals` lists
all of them; assumed numbers are counted at the end.

Query templates use @name@ placeholders (PromQL and LogQL contain braces, so str.format cannot be used).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from .model import ModelError, load_json_file

KINDS = ("high_is_bad", "down", "restarts")
COMPARATORS = (">", ">=", "==")
EMPTY_MODES = ("no_value", "zero")
GUARD_FAIL = ("quiet", "no_data")
PROVENANCE_KINDS = ("documented", "measured", "assumed")
TIERS = ("app", "infra")
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEHOLDER = re.compile(r"@([a-z_]+)@")
MARGIN = 1.5


class SignalsConfigError(ValueError):
    """signals.json is invalid."""


def render(template: str, **values) -> str:
    """Replace @name@ placeholders; every placeholder must be given and every value is used as text."""
    wanted = set(_PLACEHOLDER.findall(template))
    missing = wanted - set(values)
    if missing:
        raise SignalsConfigError(f"missing template values: {sorted(missing)}")
    out = template
    for key in wanted:
        out = out.replace(f"@{key}@", str(values[key]))
    return out


@dataclass(frozen=True)
class Provenance:
    kind: str
    source: str
    baseline_max: float | None
    fault_value_min: float | None


@dataclass(frozen=True)
class Threshold:
    services: tuple  # ("*",) or explicit service names
    absolute_min: float
    provenance: Provenance

    def margin_ratio(self) -> float | None:
        base = self.provenance.baseline_max
        if base is None or base <= 0:
            return None
        return self.absolute_min / base


@dataclass(frozen=True)
class Guard:
    query: str
    min: float
    on_fail: str
    source: str


@dataclass(frozen=True)
class SignalDef:
    name: str
    unit: str
    kind: str
    comparator: str
    tiers: tuple
    query: str
    by: str | None
    guard: Guard | None
    empty: str
    thresholds: tuple
    provenance: Provenance | None  # for down signals

    def applies_to(self, tier: str) -> bool:
        return tier in self.tiers

    def threshold_for(self, service: str) -> Threshold | None:
        """The first threshold entry naming the service (explicit names win over "*")."""
        wildcard = None
        for entry in self.thresholds:
            if service in entry.services:
                return entry
            if "*" in entry.services and wildcard is None:
                wildcard = entry
        return wildcard


@dataclass(frozen=True)
class WindowConfig:
    pre_s: int
    post_resolved_s: int
    baseline_s: int
    step_s: int
    baseline_margin: float
    change_lookback_s: int
    query_timeout_s: float


@dataclass(frozen=True)
class LogsConfig:
    selectors: dict
    count_query: str
    events_query: str
    samples_query: str
    events_tiers: tuple
    top_events: int
    samples: int
    fetch_lines: int
    sample_chars: int
    request_ids: int
    provenance: Provenance


@dataclass(frozen=True)
class SignalsConfig:
    sources: tuple
    window: WindowConfig
    signals: tuple
    logs: LogsConfig

    def by_name(self, name: str) -> SignalDef:
        for item in self.signals:
            if item.name == name:
                return item
        raise KeyError(name)

    def signals_for(self, tier: str) -> list:
        return [s for s in self.signals if s.applies_to(tier)]

    def assumed_count(self) -> int:
        count = 0
        for sig in self.signals:
            for th in sig.thresholds:
                if th.provenance.kind == "assumed":
                    count += 1
            if sig.provenance is not None and sig.provenance.kind == "assumed":
                count += 1
        return count


# ---------------------------------------------------------------- parsing helpers

def _num(value, where: str, minimum: float | None = None, allow_none: bool = False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalsConfigError(f"{where}: must be a number")
    if minimum is not None and value < minimum:
        raise SignalsConfigError(f"{where}: must be >= {minimum}")
    return float(value) if isinstance(value, float) else value


def _str(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalsConfigError(f"{where}: must be a non-empty string")
    return value


def _exact(obj, allowed: set, where: str, optional: set | None = None) -> None:
    if not isinstance(obj, dict):
        raise SignalsConfigError(f"{where}: must be an object")
    optional = optional or set()
    extra = set(obj) - allowed - optional
    missing = allowed - set(obj)
    if extra:
        raise SignalsConfigError(f"{where}: unknown keys {sorted(extra)}")
    if missing:
        raise SignalsConfigError(f"{where}: missing keys {sorted(missing)}")


def _provenance(obj, where: str) -> Provenance:
    _exact(obj, {"kind", "baseline_max", "fault_value_min", "source"}, where)
    kind = obj["kind"]
    if kind not in PROVENANCE_KINDS:
        raise SignalsConfigError(f"{where}.kind: must be one of {PROVENANCE_KINDS}")
    return Provenance(
        kind=kind,
        source=_str(obj["source"], f"{where}.source"),
        baseline_max=_num(obj["baseline_max"], f"{where}.baseline_max", 0, allow_none=True),
        fault_value_min=_num(obj["fault_value_min"], f"{where}.fault_value_min", 0, allow_none=True),
    )


def _check_placeholders(text: str, allowed: set, where: str) -> None:
    found = set(_PLACEHOLDER.findall(text))
    if not found <= allowed:
        raise SignalsConfigError(f"{where}: unknown placeholders {sorted(found - allowed)}")


def _parse_signal(raw, index: int, known_services: set | None, tiers_of: dict | None) -> SignalDef:
    where = f"signals[{index}]"
    _exact(raw, {"name", "unit", "kind", "comparator", "tiers", "query", "by", "guard", "empty", "thresholds"},
           where, optional={"provenance"})
    name = _str(raw["name"], f"{where}.name")
    if not _NAME.match(name):
        raise SignalsConfigError(f"{where}.name: bad name {name!r}")
    where = f"signal {name}"
    kind = raw["kind"]
    if kind not in KINDS:
        raise SignalsConfigError(f"{where}.kind: must be one of {KINDS}")
    if raw["comparator"] not in COMPARATORS:
        raise SignalsConfigError(f"{where}.comparator: must be one of {COMPARATORS}")
    if (kind == "down") != (raw["comparator"] == "=="):
        raise SignalsConfigError(f"{where}: comparator '==' is for down signals only")
    tiers = raw["tiers"]
    if not isinstance(tiers, list) or not tiers or any(t not in TIERS for t in tiers) or len(set(tiers)) != len(tiers):
        raise SignalsConfigError(f"{where}.tiers: non-empty list of {TIERS}")
    query = _str(raw["query"], f"{where}.query")
    _check_placeholders(query, {"service"}, f"{where}.query")
    if "@service@" not in query:
        raise SignalsConfigError(f"{where}.query: must contain @service@")
    by = raw["by"]
    if by is not None and (not isinstance(by, str) or not _NAME.match(by)):
        raise SignalsConfigError(f"{where}.by: label name or null")
    if raw["empty"] not in EMPTY_MODES:
        raise SignalsConfigError(f"{where}.empty: must be one of {EMPTY_MODES}")
    guard = None
    if raw["guard"] is not None:
        g = raw["guard"]
        _exact(g, {"query", "min", "on_fail", "source"}, f"{where}.guard")
        _check_placeholders(g["query"], {"service"}, f"{where}.guard.query")
        if g["on_fail"] not in GUARD_FAIL:
            raise SignalsConfigError(f"{where}.guard.on_fail: must be one of {GUARD_FAIL}")
        guard = Guard(_str(g["query"], f"{where}.guard.query"), _num(g["min"], f"{where}.guard.min", 0),
                      g["on_fail"], _str(g["source"], f"{where}.guard.source"))
    thresholds = []
    if not isinstance(raw["thresholds"], list):
        raise SignalsConfigError(f"{where}.thresholds: must be a list")
    seen_services: set = set()
    for t_index, t in enumerate(raw["thresholds"]):
        t_where = f"{where}.thresholds[{t_index}]"
        _exact(t, {"services", "absolute_min", "provenance"}, t_where)
        services = t["services"]
        if not isinstance(services, list) or not services or not all(isinstance(s, str) for s in services):
            raise SignalsConfigError(f"{t_where}.services: non-empty list of names or '*'")
        for s in services:
            if s in seen_services:
                raise SignalsConfigError(f"{t_where}.services: {s!r} appears in two entries")
            seen_services.add(s)
            if s != "*" and known_services is not None and s not in known_services:
                raise SignalsConfigError(f"{t_where}.services: unknown service {s!r}")
            if s != "*" and tiers_of is not None and tiers_of.get(s) not in tiers:
                raise SignalsConfigError(f"{t_where}.services: {s!r} is not in a tier the signal applies to")
        th = Threshold(tuple(services), _num(t["absolute_min"], f"{t_where}.absolute_min", 0), _provenance(t["provenance"], f"{t_where}.provenance"))
        if th.absolute_min <= 0:
            raise SignalsConfigError(f"{t_where}.absolute_min: must be > 0")
        _check_margin(th, t_where)
        thresholds.append(th)
    provenance = None
    if "provenance" in raw:
        provenance = _provenance(raw["provenance"], f"{where}.provenance")
    if kind == "down":
        if thresholds or provenance is None:
            raise SignalsConfigError(f"{where}: a down signal has a provenance block and no thresholds")
    elif not thresholds:
        raise SignalsConfigError(f"{where}: needs at least one threshold entry")
    elif provenance is not None:
        raise SignalsConfigError(f"{where}: provenance belongs inside the threshold entries")
    return SignalDef(name, _str(raw["unit"], f"{where}.unit"), kind, raw["comparator"], tuple(tiers), query, by, guard,
                     raw["empty"], tuple(thresholds), provenance)


def _check_margin(th: Threshold, where: str) -> None:
    p = th.provenance
    if p.kind == "assumed":
        return
    if p.baseline_max is None:
        raise SignalsConfigError(f"{where}: a {p.kind} number needs a baseline_max")
    if p.baseline_max > 0 and th.absolute_min < MARGIN * p.baseline_max - 1e-9:
        raise SignalsConfigError(
            f"{where}: absolute_min {th.absolute_min} is below {MARGIN} x baseline_max {p.baseline_max}")
    if p.fault_value_min is not None and th.absolute_min > p.fault_value_min + 1e-9:
        raise SignalsConfigError(
            f"{where}: absolute_min {th.absolute_min} is above the first fault reading {p.fault_value_min}")


def parse_signals(raw, known_services: set | None = None, tiers_of: dict | None = None) -> SignalsConfig:
    _exact(raw, {"schema_version", "sources", "window", "signals", "logs"}, "signals.json")
    if raw["schema_version"] != 1:
        raise SignalsConfigError("schema_version must be 1")
    names = raw["sources"]
    if (not isinstance(names, list) or len(names) != 5 or len(set(names)) != 5
            or not all(isinstance(n, str) and _NAME.match(n) for n in names)):
        raise SignalsConfigError("sources must be a list of exactly 5 distinct lowercase names")
    w = raw["window"]
    _exact(w, {"pre_s", "post_resolved_s", "baseline_s", "step_s", "baseline_margin", "change_lookback_s", "query_timeout_s"}, "window")
    window = WindowConfig(
        pre_s=int(_num(w["pre_s"], "window.pre_s", 1)), post_resolved_s=int(_num(w["post_resolved_s"], "window.post_resolved_s", 0)),
        baseline_s=int(_num(w["baseline_s"], "window.baseline_s", 1)), step_s=int(_num(w["step_s"], "window.step_s", 1)),
        baseline_margin=float(_num(w["baseline_margin"], "window.baseline_margin", 1)),
        change_lookback_s=int(_num(w["change_lookback_s"], "window.change_lookback_s", 0)),
        query_timeout_s=float(_num(w["query_timeout_s"], "window.query_timeout_s", 0.1)),
    )
    if not isinstance(raw["signals"], list) or not raw["signals"]:
        raise SignalsConfigError("signals: non-empty list")
    signals = []
    names = set()
    for index, item in enumerate(raw["signals"]):
        sig = _parse_signal(item, index, known_services, tiers_of)
        if sig.name in names:
            raise SignalsConfigError(f"duplicate signal name {sig.name!r}")
        names.add(sig.name)
        signals.append(sig)
    lg = raw["logs"]
    _exact(lg, {"selectors", "count_query", "events_query", "samples_query", "events_tiers", "top_events", "samples",
                "fetch_lines", "sample_chars", "request_ids", "provenance"}, "logs")
    if not isinstance(lg["selectors"], dict) or set(lg["selectors"]) != set(TIERS):
        raise SignalsConfigError("logs.selectors: one selector per tier (app, infra)")
    for tier, sel in lg["selectors"].items():
        _check_placeholders(_str(sel, f"logs.selectors.{tier}"), {"service"}, f"logs.selectors.{tier}")
    _check_placeholders(lg["count_query"], {"selector", "range"}, "logs.count_query")
    _check_placeholders(lg["events_query"], {"selector", "range", "top"}, "logs.events_query")
    _check_placeholders(lg["samples_query"], {"selector"}, "logs.samples_query")
    if not isinstance(lg["events_tiers"], list) or any(t not in TIERS for t in lg["events_tiers"]):
        raise SignalsConfigError("logs.events_tiers")
    logs = LogsConfig(
        selectors=dict(lg["selectors"]), count_query=lg["count_query"], events_query=lg["events_query"],
        samples_query=lg["samples_query"], events_tiers=tuple(lg["events_tiers"]),
        top_events=int(_num(lg["top_events"], "logs.top_events", 1)), samples=int(_num(lg["samples"], "logs.samples", 0)),
        fetch_lines=int(_num(lg["fetch_lines"], "logs.fetch_lines", 1)), sample_chars=int(_num(lg["sample_chars"], "logs.sample_chars", 20)),
        request_ids=int(_num(lg["request_ids"], "logs.request_ids", 0)), provenance=_provenance_loose(lg["provenance"]),
    )
    return SignalsConfig(tuple(raw["sources"]), window, tuple(signals), logs)


def _provenance_loose(obj) -> Provenance:
    _exact(obj, {"kind", "source"}, "logs.provenance")
    if obj["kind"] not in PROVENANCE_KINDS:
        raise SignalsConfigError("logs.provenance.kind")
    return Provenance(obj["kind"], _str(obj["source"], "logs.provenance.source"), None, None)


def load_signals(path: str, known_services: set | None = None, tiers_of: dict | None = None) -> SignalsConfig:
    try:
        raw = load_json_file(path)
    except (OSError, ModelError) as exc:
        raise SignalsConfigError(str(exc)) from exc
    return parse_signals(raw, known_services, tiers_of)


# ---------------------------------------------------------------- provenance table

def provenance_rows(cfg: SignalsConfig) -> list[dict]:
    rows = []
    for sig in cfg.signals:
        if sig.provenance is not None:
            p = sig.provenance
            rows.append({"signal": sig.name, "services": "tier " + "/".join(sig.tiers), "condition": "value == 0",
                         "baseline_max": p.baseline_max, "fault_value_min": p.fault_value_min, "margin": None, "kind": p.kind, "source": p.source})
        for th in sig.thresholds:
            p = th.provenance
            rows.append({"signal": sig.name, "services": ",".join(th.services), "condition": f"{sig.comparator} {th.absolute_min:g} {sig.unit}",
                         "baseline_max": p.baseline_max, "fault_value_min": p.fault_value_min,
                         "margin": th.margin_ratio(), "kind": p.kind, "source": p.source})
    return rows


def format_table(cfg: SignalsConfig) -> list[str]:
    def cell(value):
        return "-" if value is None else (f"{value:g}" if isinstance(value, (int, float)) else str(value))

    lines = ["| signal | services | condition | baseline max | first fault reading | margin | kind | source |",
             "|---|---|---|---|---|---|---|---|"]
    for r in provenance_rows(cfg):
        margin = "-" if r["margin"] is None else f"{r['margin']:.2f}x"
        lines.append(f"| {r['signal']} | {r['services']} | {r['condition']} | {cell(r['baseline_max'])} | {cell(r['fault_value_min'])} | {margin} | {r['kind']} | {r['source']} |")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.signals <signals.json>", file=sys.stderr)
        return 2
    try:
        cfg = load_signals(argv[1])
    except SignalsConfigError as exc:
        print(f"[FAIL] {exc}")
        return 1
    for line in format_table(cfg):
        print(line)
    print(f"{len(cfg.signals)} signals valid")
    print(f"assumed provenance: {cfg.assumed_count()} (to be measured in CP4 and CP5)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
