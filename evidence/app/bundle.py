"""Evidence bundle assembly (pure logic): window, detector hint, services section, truncation, validation.

The query side (Prometheus, Loki, container inspection) fills ServiceEvidence objects; this module only
assembles and checks them. The result is a plain dict in the field order of the contract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from .graph import DependencyGraph
from .model import (
    BUNDLE_MAX_BYTES, SCHEMA_VERSION, SOURCE_STATES, TRIGGERS, ChangeRecord, DetectorHint, HintSymptom,
    ModelError, RankingResult, ServiceEvidence, fmt_ts, parse_ts, validate_bundle,
)
from .ranking import RankingConfig, rank
from .redact import clean_text, scrub
from .signals import WindowConfig
from .topology import Topology


class BundleError(ValueError):
    """A bundle could not be built or does not satisfy the contract."""


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    baseline_start: datetime
    baseline_end: datetime
    baseline_clean: bool

    def to_dict(self) -> dict:
        return {
            "start": fmt_ts(self.start), "end": fmt_ts(self.end),
            "baseline_start": fmt_ts(self.baseline_start), "baseline_end": fmt_ts(self.baseline_end),
            "baseline_clean": self.baseline_clean,
        }


# ---------------------------------------------------------------- incident helpers

def _symptom_times(incident: Mapping) -> tuple[datetime, datetime]:
    symptoms = incident.get("symptoms") or []
    if not symptoms:
        raise BundleError("incident has no symptoms")
    try:
        firsts = [parse_ts(s["first_seen"]) for s in symptoms]
        lasts = [parse_ts(s["last_seen"]) for s in symptoms]
    except (KeyError, ModelError) as exc:
        raise BundleError(f"bad symptom timestamps: {exc}") from exc
    return min(firsts), max(lasts)


def _incident_span(incident: Mapping, now: datetime) -> tuple[datetime, datetime]:
    first, last = _symptom_times(incident)
    if incident.get("status") == "resolved":
        end = parse_ts(incident["resolved_at"]) if incident.get("resolved_at") else last
        return first, max(end, last)
    return first, now


def compute_window(incident: Mapping, others: Sequence[Mapping], now: datetime, cfg: WindowConfig) -> Window:
    """Window and baseline of one incident. Other incidents that overlap the baseline make it unclean."""
    first, last = _symptom_times(incident)
    start = first - timedelta(seconds=cfg.pre_s)
    if incident.get("status") == "resolved":
        end = last + timedelta(seconds=cfg.post_resolved_s)
    else:
        end = now
    if end < start:
        end = start
    baseline_start = start - timedelta(seconds=cfg.baseline_s)
    clean = True
    for other in others:
        if other.get("incident_id") == incident.get("incident_id"):
            continue
        try:
            o_first, o_end = _incident_span(other, now)
        except BundleError:
            continue
        if o_first < start and o_end > baseline_start:
            clean = False
            break
    return Window(start, end, baseline_start, start, clean)


def make_hint(incident: Mapping) -> DetectorHint:
    """Compact detector hint: primary_service, candidate_services and the symptom list."""
    symptoms = []
    for s in incident.get("symptoms") or []:
        try:
            symptoms.append(HintSymptom(s["rule_id"], s.get("service"), s["role"], parse_ts(s["first_seen"])))
        except (KeyError, ModelError) as exc:
            raise BundleError(f"bad symptom: {exc}") from exc
    symptoms.sort(key=lambda x: (x.first_seen, x.rule_id, x.service or ""))
    return DetectorHint(incident.get("primary_service"), tuple(incident.get("candidate_services") or ()), tuple(symptoms))


# ---------------------------------------------------------------- assembly

def _sources_section(states: Mapping[str, tuple], names: Sequence[str]) -> dict:
    out = {}
    for name in names:
        if name not in states:
            raise BundleError(f"missing source state: {name}")
        state, detail = states[name]
        if state not in SOURCE_STATES:
            raise BundleError(f"bad source state {state!r} for {name}")
        out[name] = {"state": state, "detail": clean_text(str(detail))}
    return out


def _changes_in_scope(changes: Sequence[ChangeRecord], start: datetime, end: datetime, lookback_s: int) -> list:
    lo = start - timedelta(seconds=lookback_s)
    keep = [c for c in changes if lo <= c.ts <= end]
    keep.sort(key=lambda c: (c.ts, c.kind))
    return [c.to_dict() for c in keep]


def _service_section(ev: ServiceEvidence, window: Window, lookback_s: int) -> dict:
    sigs = sorted(ev.signals, key=lambda s: (s.base, s.label or ""))
    return {
        "tier": ev.tier,
        "signals": [s.to_entry() for s in sigs],
        "logs": None if ev.logs is None else ev.logs.to_dict(),
        "container": None if ev.container is None else ev.container.to_section(),
        "changes": _changes_in_scope(ev.changes, window.start, window.end, lookback_s),
    }


def _data_gaps(states: Mapping[str, tuple], names: Sequence[str], services: Mapping[str, ServiceEvidence]) -> list:
    gaps = []
    for name in names:
        state, detail = states[name]
        if state != "ok":
            gaps.append({"source": name, "service": None, "signal": None, "detail": clean_text(f"{state}: {detail}")})
    for svc in sorted(services):
        for signal, detail in sorted(services[svc].gaps):
            source = "loki" if signal == "logs" else "prometheus"  # "logs" is the log evidence of a service
            gaps.append({"source": source, "service": svc, "signal": signal, "detail": clean_text(str(detail))})
    return gaps


def size_of(bundle: Mapping) -> int:
    return len(json.dumps(bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _cut_samples(b):
    n = 0
    for item in b["services"].values():
        if item["logs"] and item["logs"]["samples"]:
            n += len(item["logs"]["samples"])
            item["logs"]["samples"] = []
    return "services.*.logs.samples" if n else None


def _cut_request_ids(b):
    n = 0
    for item in b["services"].values():
        if item["logs"] and item["logs"]["request_ids"]:
            n += len(item["logs"]["request_ids"])
            item["logs"]["request_ids"] = []
    return "services.*.logs.request_ids" if n else None


def _cut_events(b):
    n = 0
    for item in b["services"].values():
        if item["logs"] and len(item["logs"]["top_events"]) > 2:
            n += 1
            item["logs"]["top_events"] = item["logs"]["top_events"][:2]
    return "services.*.logs.top_events (kept 2)" if n else None


def _cut_quiet_signals(b):
    n = 0
    for item in b["services"].values():
        keep = [s for s in item["signals"] if s["anomalous"]]
        if len(keep) != len(item["signals"]):
            n += 1
            item["signals"] = keep
    return "services.*.signals (kept anomalous)" if n else None


def _cut_changes(b):
    n = 0
    for item in b["services"].values():
        if len(item["changes"]) > 5:
            n += 1
            item["changes"] = item["changes"][-5:]
    return "services.*.changes (kept newest 5)" if n else None


def _cut_symptoms(b):
    if len(b["detector_hint"]["symptoms"]) > 20:
        b["detector_hint"]["symptoms"] = b["detector_hint"]["symptoms"][:20]
        return "detector_hint.symptoms (kept 20)"
    return None


def _cut_reasons(b):
    n = 0
    for sus in b["suspects"]:
        if len(sus["reasons"]) > 2:
            n += 1
            sus["reasons"] = sus["reasons"][:2]
    return "suspects.*.reasons (kept 2)" if n else None


CUTS = (_cut_samples, _cut_request_ids, _cut_events, _cut_quiet_signals, _cut_changes, _cut_symptoms, _cut_reasons)


def enforce_size(bundle: dict, limit: int = BUNDLE_MAX_BYTES) -> dict:
    """Cut the least useful parts, in a fixed order, until the bundle fits; every cut is listed in `truncated`."""
    for cut in CUTS:
        if size_of(bundle) <= limit:
            break
        label = cut(bundle)
        if label:
            bundle["truncated"].append(label)
    if size_of(bundle) > limit:
        raise BundleError(f"bundle is {size_of(bundle)} bytes after all cuts; limit {limit}")
    return bundle


def build_bundle(
    *,
    bundle_id: str,
    incident: Mapping,
    trigger: str,
    built_at: datetime,
    build_ms: int,
    window: Window,
    source_states: Mapping[str, tuple],
    source_names: Sequence[str],
    services: Mapping[str, ServiceEvidence],
    topology: Topology,
    graph: DependencyGraph,
    ranking_cfg: RankingConfig,
    window_cfg: WindowConfig,
    max_bytes: int = BUNDLE_MAX_BYTES,
) -> dict:
    if trigger not in TRIGGERS:
        raise BundleError(f"bad trigger {trigger!r}")
    unknown = set(services) - set(topology.services)
    if unknown:
        raise BundleError(f"services outside the topology: {sorted(unknown)}")
    anomalous = sorted(name for name, ev in services.items() if ev.is_anomalous())
    min_dep = ranking_cfg.param("shared_dependency", "min_dependents")
    graph_section = graph.section(anomalous, min_dep)
    deps = {name: tuple(graph.dependencies(name)) for name in graph.nodes()}
    hint = make_hint(incident)
    result: RankingResult = rank(services, deps, graph_section["shared_dependencies"], hint, ranking_cfg)
    bundle = {
        "bundle_id": bundle_id,
        "schema_version": SCHEMA_VERSION,
        "incident_id": incident.get("incident_id"),
        "trigger": trigger,
        "built_at": fmt_ts(built_at),
        "build_ms": int(build_ms),
        "window": window.to_dict(),
        "sources": _sources_section(source_states, source_names),
        "detector_hint": hint.to_dict(),
        "services": {name: _service_section(services[name], window, window_cfg.change_lookback_s) for name in sorted(services)},
        "graph": graph_section,
        "suspects": [s.to_dict() for s in result.suspects],
        "no_single_root": result.no_single_root,
        "confidence": result.confidence,
        "data_gaps": _data_gaps(source_states, source_names, services),
        "truncated": [],
    }
    clean, withheld = scrub(bundle)
    if withheld:
        clean["truncated"].append(f"{withheld} text value(s) withheld (fault-injection marker)")
    enforce_size(clean, max_bytes)
    problems = validate_bundle(clean, tuple(source_names))
    if problems:
        raise BundleError("bundle does not satisfy the contract: " + "; ".join(problems))
    return clean
