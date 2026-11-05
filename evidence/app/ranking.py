"""Suspect ranking (pure logic) and the loader of evidence/config/ranking.json.

Nine components, each with a cap and a reason (ranking.json). Points of a component are 0..cap, except
effect_demotion which subtracts up to its cap. The score is the clipped sum (0..100). Ties are broken by
service name. Only the data in ServiceEvidence, the dependency map, the shared dependencies and the detector
hint are used.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Mapping

from .model import CHANGE_KINDS, DetectorHint, ModelError, RankingResult, ServiceEvidence, Suspect, load_json_file

COMPONENT_ORDER = (
    "anomaly_strength", "precedence", "hard_container_evidence", "recent_change", "origin_position",
    "shared_dependency", "effect_demotion", "log_evidence", "detector_hint",
)
HARD_STATES = ("exited", "dead")


class RankingConfigError(ValueError):
    """ranking.json is invalid."""


@dataclass(frozen=True)
class ComponentConfig:
    name: str
    cap: float
    params: dict
    kind: str  # design | measured
    reason: str
    evidence: str | None


@dataclass(frozen=True)
class RankingConfig:
    margin: float
    components: dict  # name -> ComponentConfig

    def cap(self, name: str) -> float:
        return self.components[name].cap

    def param(self, name: str, key: str):
        return self.components[name].params[key]


# ---------------------------------------------------------------- config

def _frac(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise RankingConfigError(f"{where}: must be a number from 0 to 1")
    return float(value)


def _pos(value, where, minimum=0.0, integer=False, strict=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RankingConfigError(f"{where}: must be a number")
    if integer and int(value) != value:
        raise RankingConfigError(f"{where}: must be an integer")
    if (value <= minimum) if strict else (value < minimum):
        raise RankingConfigError(f"{where}: must be {'>' if strict else '>='} {minimum}")
    return int(value) if integer else float(value)


def _kinds(value, where):
    if not isinstance(value, list) or not value or any(k not in CHANGE_KINDS for k in value):
        raise RankingConfigError(f"{where}: non-empty list of {CHANGE_KINDS}")
    return list(value)


_PARAMS = {
    "anomaly_strength": {"floor": lambda v, w: _frac(v, w), "full_ratio": lambda v, w: _pos(v, w, 1.0, strict=True)},
    "precedence": {"window_s": lambda v, w: _pos(v, w, 0.0, strict=True), "tie_s": lambda v, w: _pos(v, w, 0.0)},
    "hard_container_evidence": {"crash_loop_restarts": lambda v, w: _pos(v, w, 2, integer=True),
                                "single_restart_fraction": lambda v, w: _frac(v, w)},
    "recent_change": {"lookback_s": lambda v, w: _pos(v, w, 0.0, strict=True), "observe_slack_s": lambda v, w: _pos(v, w, 0.0),
                      "kinds": _kinds},
    "origin_position": {},
    "shared_dependency": {"min_dependents": lambda v, w: _pos(v, w, 2, integer=True)},
    "effect_demotion": {},
    "log_evidence": {"own_errors_fraction": lambda v, w: _frac(v, w), "dependency_error_fraction": lambda v, w: _frac(v, w),
                     "dependency_latency_fraction": lambda v, w: _frac(v, w), "dependents_for_full": lambda v, w: _pos(v, w, 1, integer=True)},
    "detector_hint": {"candidate_points": lambda v, w: _pos(v, w, 0.0)},
}


def parse_ranking(raw) -> RankingConfig:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "no_single_root_margin", "components"}:
        raise RankingConfigError("ranking.json: keys must be schema_version, no_single_root_margin, components")
    if raw["schema_version"] != 1:
        raise RankingConfigError("schema_version must be 1")
    margin = _pos(raw["no_single_root_margin"], "no_single_root_margin", 0.0)
    comps = raw["components"]
    if not isinstance(comps, dict) or set(comps) != set(COMPONENT_ORDER):
        raise RankingConfigError(f"components must be exactly {list(COMPONENT_ORDER)}")
    out = {}
    for name in COMPONENT_ORDER:
        item = comps[name]
        where = f"components.{name}"
        if not isinstance(item, dict) or set(item) != {"cap", "params", "provenance"}:
            raise RankingConfigError(f"{where}: keys must be cap, params, provenance")
        cap = _pos(item["cap"], f"{where}.cap", 0.0, strict=True)
        if cap > 100:
            raise RankingConfigError(f"{where}.cap: must be at most 100")
        params = item["params"]
        expected = _PARAMS[name]
        if not isinstance(params, dict) or set(params) != set(expected):
            raise RankingConfigError(f"{where}.params: keys must be {sorted(expected)}")
        checked = {k: expected[k](params[k], f"{where}.params.{k}") for k in expected}
        if name == "detector_hint" and checked["candidate_points"] > cap:
            raise RankingConfigError(f"{where}.params.candidate_points: must not exceed the cap")
        prov = item["provenance"]
        if not isinstance(prov, dict) or set(prov) != {"kind", "reason", "evidence"}:
            raise RankingConfigError(f"{where}.provenance: keys must be kind, reason, evidence")
        if prov["kind"] not in ("design", "measured"):
            raise RankingConfigError(f"{where}.provenance.kind: design or measured")
        if not isinstance(prov["reason"], str) or not prov["reason"].strip():
            raise RankingConfigError(f"{where}.provenance.reason: every weight needs a reason")
        if prov["kind"] == "measured" and not (isinstance(prov["evidence"], str) and prov["evidence"].strip()):
            raise RankingConfigError(f"{where}.provenance.evidence: a measured weight names the run it comes from")
        out[name] = ComponentConfig(name, cap, checked, prov["kind"], prov["reason"], prov["evidence"])
    return RankingConfig(margin, out)


def load_ranking(path: str) -> RankingConfig:
    try:
        return parse_ranking(load_json_file(path))
    except (OSError, ModelError) as exc:
        raise RankingConfigError(str(exc)) from exc


def format_table(cfg: RankingConfig) -> list[str]:
    lines = ["| component | cap | params | kind | reason |", "|---|---|---|---|---|"]
    for name in COMPONENT_ORDER:
        c = cfg.components[name]
        params = ", ".join(f"{k}={v}" for k, v in c.params.items()) or "-"
        lines.append(f"| {name} | {c.cap:g} | {params} | {c.kind} | {c.reason} |")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.ranking <ranking.json>", file=sys.stderr)
        return 2
    try:
        cfg = load_ranking(argv[1])
    except RankingConfigError as exc:
        print(f"[FAIL] {exc}")
        return 1
    for line in format_table(cfg):
        print(line)
    design = sum(1 for c in cfg.components.values() if c.kind == "design")
    print(f"{len(cfg.components)} components valid, no_single_root margin {cfg.margin:g}")
    print(f"design weights: {design} of {len(cfg.components)} (to be calibrated with measured runs in CP4 and CP5)")
    return 0


# ---------------------------------------------------------------- scoring

def _r(value: float) -> float:
    return round(value + 0.0, 1)


def _anomalous_names(services: Mapping[str, ServiceEvidence]) -> set:
    return {name for name, ev in services.items() if ev.is_anomalous()}


def rank(
    services: Mapping[str, ServiceEvidence],
    deps: Mapping[str, tuple],
    shared: Mapping[str, list],
    hint: DetectorHint,
    cfg: RankingConfig,
) -> RankingResult:
    """Rank the services. `deps` maps a service to its direct dependencies, `shared` is the graph's
    shared_dependencies (dependency -> anomalous direct callers)."""
    anomalous = _anomalous_names(services)
    firsts = {name: services[name].first_anomalous_at() for name in anomalous}
    earliest_name, earliest = None, None
    for name in sorted(anomalous):
        if firsts[name] is not None and (earliest is None or firsts[name] < earliest):
            earliest_name, earliest = name, firsts[name]
    rows = []
    for name in sorted(services):
        ev = services[name]
        comps: dict = {}
        reasons: dict = {}

        def put(key: str, points: float, why: list) -> None:
            comps[key] = _r(points)
            if points != 0 and why:
                reasons[key] = why

        put(*_strength(ev, cfg))
        put(*_precedence(name, ev, firsts, earliest_name, earliest, cfg))
        put(*_hard(ev, cfg))
        put(*_change(ev, cfg))
        put(*_origin(name, anomalous, deps, cfg))
        put(*_shared(name, services, anomalous, shared, cfg))
        put(*_demotion(name, anomalous, deps, cfg))
        put(*_log(name, services, deps, cfg))
        put(*_hint(name, hint, cfg))
        total = max(0.0, min(100.0, sum(comps.values())))
        rows.append((name, _r(total), comps, [r for key in COMPONENT_ORDER for r in reasons.get(key, [])]))
    keep = [row for row in rows if row[0] in anomalous or row[1] > 0]
    keep.sort(key=lambda row: (-row[1], row[0]))
    suspects = tuple(Suspect(i, name, score, comps, tuple(why)) for i, (name, score, comps, why) in enumerate(keep, start=1))
    has_load = any(s.role == "load" for s in hint.symptoms)
    if not suspects:
        no_single, confidence = True, 0.0
    elif len(suspects) == 1:
        no_single, confidence = has_load, suspects[0].score
    else:
        gap = suspects[0].score - suspects[1].score
        no_single, confidence = has_load or gap <= cfg.margin, _r(gap)
    return RankingResult(suspects, no_single, confidence)


def _strength(ev: ServiceEvidence, cfg: RankingConfig):
    key, cap = "anomaly_strength", cfg.cap("anomaly_strength")
    sigs = ev.anomalous_signals()
    if not sigs:
        return key, 0.0, []
    floor, full = cfg.param(key, "floor"), cfg.param(key, "full_ratio")
    best, best_sig = -1.0, None
    for sig in sigs:
        if sig.ratio is None:
            frac = 1.0
        else:
            frac = floor + (1 - floor) * min(1.0, max(0.0, (sig.ratio - 1.0) / (full - 1.0)))
        if frac > best:
            best, best_sig = frac, sig
    if best_sig.ratio is None:
        why = f"strongest signal {best_sig.name} is down"
    else:
        why = f"strongest signal {best_sig.name} reached {best_sig.ratio:.1f}x its threshold"
    return key, cap * best, [why]


def _precedence(name, ev, firsts, earliest_name, earliest, cfg):
    key, cap = "precedence", cfg.cap("precedence")
    if name not in firsts or firsts[name] is None or earliest is None:
        return key, 0.0, []
    tie, window = cfg.param(key, "tie_s"), cfg.param(key, "window_s")
    lag = (firsts[name] - earliest).total_seconds()
    if lag <= tie:
        frac = 1.0
        why = "earliest anomalous service" if name == earliest_name else f"anomalous within {tie:g} s of the earliest service ({earliest_name})"
    else:
        frac = max(0.0, 1.0 - (lag - tie) / window)
        why = f"first anomaly {lag:.0f} s after the earliest anomalous service ({earliest_name})"
    return key, cap * frac, [why]


def _hard(ev: ServiceEvidence, cfg: RankingConfig):
    key, cap = "hard_container_evidence", cfg.cap("hard_container_evidence")
    found: list = []
    by_base = {s.base: s for s in ev.signals if s.anomalous and s.label is None}
    if "container_present" in by_base:
        found.append((1.0, "container missing (no memory series)"))
    if "up" in by_base:
        found.append((1.0, "scrape target is down"))
    box = ev.container
    if box is not None:
        if box.status in HARD_STATES:
            found.append((1.0, f"container state is {box.status}"))
        if box.status == "restarting":
            found.append((1.0, "container is restarting"))
        if box.oom_killed:
            found.append((1.0, "container was OOM-killed"))
    if "restarts" in by_base:
        count = by_base["restarts"].window_max or 0
        if count >= cfg.param(key, "crash_loop_restarts"):
            found.append((1.0, f"crash loop ({count:g} restarts in the window)"))
        else:
            found.append((cfg.param(key, "single_restart_fraction"), f"{count:g} restart in the window"))
    if not found:
        return key, 0.0, []
    frac = max(f for f, _ in found)
    seen, why = set(), []
    for _, text in found:
        if text not in seen:
            seen.add(text)
            why.append(text)
    return key, cap * frac, why


def _change(ev: ServiceEvidence, cfg: RankingConfig):
    key, cap = "recent_change", cfg.cap("recent_change")
    first = ev.first_anomalous_at()
    if first is None:
        return key, 0.0, []
    kinds, lookback, slack = cfg.param(key, "kinds"), cfg.param(key, "lookback_s"), cfg.param(key, "observe_slack_s")
    best = None
    for ch in ev.changes:
        if ch.kind not in kinds:
            continue
        delta = (first - ch.ts).total_seconds()  # positive: the change came first
        if -slack <= delta <= lookback and (best is None or abs(delta) < abs(best[0])):
            best = (delta, ch)
    if best is None:
        return key, 0.0, []
    delta, ch = best
    when = f"{delta:.0f} s before" if delta >= 0 else f"{-delta:.0f} s after (poller delay)"
    return key, cap, [f"change record '{ch.kind}' {when} its first anomaly"]


def _origin(name, anomalous, deps, cfg):
    key, cap = "origin_position", cfg.cap("origin_position")
    if name not in anomalous:
        return key, 0.0, []
    below = tuple(deps.get(name, ()))
    if any(d in anomalous for d in below):
        return key, 0.0, []
    if below:
        return key, cap, [f"anomalous while all its dependencies ({', '.join(below)}) are healthy"]
    return key, cap, ["anomalous and has no dependency"]


def _shared(name, services, anomalous, shared, cfg):
    """Callers count only when they are implicated: the dependency is anomalous itself, or the caller reports
    an anomalous dependency signal that names it. Otherwise a healthy service would earn the bonus just
    because its callers are anomalous for another reason."""
    key, cap = "shared_dependency", cfg.cap("shared_dependency")
    callers = []
    for caller in shared.get(name, ()):
        names_it = any(sig.anomalous and sig.label == name for sig in services[caller].signals) if caller in services else False
        if name in anomalous or names_it:
            callers.append(caller)
    if len(callers) >= cfg.param(key, "min_dependents"):
        return key, cap, [f"{len(callers)} anomalous services call it directly ({', '.join(callers)})"]
    return key, 0.0, []


def _demotion(name, anomalous, deps, cfg):
    key, cap = "effect_demotion", cfg.cap("effect_demotion")
    if name not in anomalous:
        return key, 0.0, []
    bad = [d for d in deps.get(name, ()) if d in anomalous]
    if not bad:
        return key, 0.0, []
    return key, -cap, [f"its dependency {', '.join(bad)} is anomalous and explains its symptoms"]


def _log(name, services, deps, cfg):
    key, cap = "log_evidence", cfg.cap("log_evidence")
    ev = services[name]
    own = 0.0
    why: list = []
    if ev.logs is not None and ev.logs.state == "ok" and ev.logs.window_count is not None and ev.logs.baseline_count is not None:
        if ev.logs.window_count > ev.logs.baseline_count:
            own = cfg.param(key, "own_errors_fraction")
            why.append(f"{ev.logs.window_count} WARNING/ERROR lines in the window against {ev.logs.baseline_count} in the baseline")
    need = cfg.param(key, "dependents_for_full")
    err, lat = [], []
    for other in sorted(services):
        if other == name or name not in deps.get(other, ()):
            continue
        for sig in services[other].signals:
            if not sig.anomalous or sig.label != name:
                continue
            if sig.base == "dependency_error_rate":
                err.append(other)
            elif sig.base == "dependency_latency_p95":
                lat.append(other)
    frac = own
    if err:
        frac += cfg.param(key, "dependency_error_fraction") * min(1.0, len(err) / need)
        why.append(f"dependency errors against it reported by {', '.join(sorted(set(err)))}")
    if lat:
        frac += cfg.param(key, "dependency_latency_fraction") * min(1.0, len(lat) / need)
        why.append(f"dependency latency against it reported by {', '.join(sorted(set(lat)))}")
    return key, cap * min(1.0, frac), why


def _hint(name, hint: DetectorHint, cfg):
    key, cap = "detector_hint", cfg.cap("detector_hint")
    if hint.primary_service == name:
        return key, cap, ["named primary_service by the detector"]
    if name in hint.candidate_services:
        return key, cfg.param(key, "candidate_points"), ["listed as candidate service by the detector"]
    return key, 0.0, []


if __name__ == "__main__":
    sys.exit(main(sys.argv))
