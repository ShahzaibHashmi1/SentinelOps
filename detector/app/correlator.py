"""Correlator: observations -> symptoms -> incidents. Pure logic, no I/O, no queries (D61).

It receives ONLY observations (plus RuleMeta: name, severity, role, opens_incident, for_s of each
rule id). It knows nothing about PromQL, thresholds or SQL, so a later detector (EWMA) needs no
change here.

Rules in short (full text: docs/DETECTOR.md, CP5):
  hold      a rule must fire in EVERY evaluation for for_s seconds before it becomes a symptom
            (an ok, a no_data or a missing observation resets the hold). first_seen is the
            first firing evaluation of the hold, opened_at is the cycle in which the hold was met.
  incident  one open incident at a time. The first symptom whose rule has opens_incident creates
            it; every later symptom (any opens_incident value) attaches to it. A symptom of a rule
            that cannot open an incident (log evidence) waits and attaches when an incident opens.
  resolve   when no symptom has been active for resolve_after_s. A data gap pauses this timer.
  reopen    an opening symptom within reopen_within_s after a resolve reopens the same incident.
  no_data   freezes symptoms (active, last_seen and last_value stay), resets holds, pauses the
            resolve timer and is recorded as a data gap. It never fires and never clears.
  primary   the one service of all root_signal symptoms, unless a load symptom exists; else null.

Time: holds and the resolve timer use the monotonic clock handed in by the loop; stored times
(first_seen, opened_at, ...) are UTC datetimes; the reopen window compares UTC times (resolved_at
is a stored time, so it also works after a restart).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .model import (FIRING, NO_DATA, OK, OPEN, RESOLVED, SEVERITY_RANK, DataGap, Incident,
                    Observation, RuleMeta, Symptom)

_DEPENDENCY = re.compile(r"dependency=([A-Za-z0-9_.-]+)")
# A hold is met when elapsed + tolerance >= for_s, so a cycle that starts a few hundred
# milliseconds early (scheduler jitter) does not cost a whole extra interval.
HOLD_TOLERANCE_S = 0.5
TITLE_PARTS = 3


@dataclass
class CycleResult:
    changed: list = field(default_factory=list)   # incidents to write to the store
    events: list = field(default_factory=list)    # dicts for the log: {"event": ..., ...}


class Correlator:
    def __init__(self, metas, resolve_after_s: float = 60.0, reopen_within_s: float = 120.0,
                 hold_tolerance_s: float = HOLD_TOLERANCE_S,
                 next_incident_seq: int = 1, next_symptom_seq: int = 1):
        self._metas: dict = {m.rule_id: m for m in metas}
        self.resolve_after_s = float(resolve_after_s)
        self.reopen_within_s = float(reopen_within_s)
        self._tolerance = hold_tolerance_s
        self._next_incident = next_incident_seq
        self._next_symptom = next_symptom_seq
        self.incident: Optional[Incident] = None      # the latest incident, open or resolved
        self._holds: dict = {}                        # (rule_id, subject) -> (monotonic, utc) of first firing
        self._pending: dict = {}                      # qualified symptoms that cannot open an incident yet
        self._quiet: Optional[float] = None           # seconds without an active symptom (None = not counting)
        self._last_mono: Optional[float] = None
        self._prev_gap = False                        # the previous cycle had a data gap
        self._open_gaps: dict = {}                    # source -> DataGap that is open now

    # ---------------------------------------------------------------- restart

    def restore(self, incident: Optional[Incident]) -> None:
        """Take over the latest incident read from the store after a restart.

        Hold counters and the resolve timer are not persisted: they start from zero. Active
        symptoms stay active; data gaps that were open are closed by the first cycle that has
        no gap (the time the detector was down counts as a gap).
        """
        self.incident = incident
        self._open_gaps = {}
        if incident is not None and incident.status == OPEN:
            for gap in incident.data_gaps:
                if gap.end is None:
                    self._open_gaps[gap.source] = gap

    @property
    def next_incident_seq(self) -> int:
        return self._next_incident

    @property
    def next_symptom_seq(self) -> int:
        return self._next_symptom

    # ---------------------------------------------------------------- one cycle

    def process(self, observations, now_mono: float, now_utc: datetime) -> CycleResult:
        result = CycleResult()
        changed: dict = {}
        elapsed = 0.0 if self._last_mono is None else max(0.0, now_mono - self._last_mono)
        self._last_mono = now_mono

        usable = []
        for obs in sorted(observations, key=lambda o: (o.rule_id, o.subject or "")):
            if obs.rule_id in self._metas:
                usable.append(obs)
            else:
                result.events.append({"event": "unknown_rule_ignored", "rule_id": obs.rule_id})

        gap_sources = {o.source for o in usable if o.state == NO_DATA}
        self._update_gaps(gap_sources, now_utc, changed, result)

        # 1. update known symptoms, collect the ones whose hold is met
        open_incident = self.incident if (self.incident and self.incident.status == OPEN) else None
        new_holds: dict = {}
        qualified: list = []   # (observation, first firing utc)
        for obs in usable:
            key = (obs.rule_id, obs.subject)
            meta = self._metas[obs.rule_id]
            known = self._find(self.incident, key)
            if obs.state == NO_DATA:
                continue                                  # freeze: nothing changes, the hold is not kept
            if obs.state == OK:
                if known is not None and known.active and open_incident is not None:
                    known.active = False
                    changed[open_incident.seq] = open_incident
                    result.events.append({"event": "symptom_cleared", "incident_id": open_incident.incident_id,
                                          "rule_id": key[0], "subject": key[1]})
                continue
            if known is not None and known.active and open_incident is not None:
                self._update_symptom(known, obs)
                changed[open_incident.seq] = open_incident
                continue
            first = self._holds.get(key) or (now_mono, obs.ts)
            new_holds[key] = first
            if now_mono - first[0] + self._tolerance >= meta.for_s:
                qualified.append((obs, first[1]))
        self._holds = new_holds

        # 2. attach qualified symptoms, open or reopen an incident when an opener qualified
        candidates: list = []   # (pending symptom or None, observation, first firing utc)
        for obs, first in qualified:
            pending = self._pending.get((obs.rule_id, obs.subject))
            if pending is not None:
                self._update_symptom(pending, obs)
            candidates.append((pending, obs, first))
        if candidates:
            incident = open_incident
            if incident is None and any(self._metas[o.rule_id].opens_incident for _, o, _ in candidates):
                incident = self._open_or_reopen(now_utc, changed, result)
            if incident is not None:
                for symptom, obs, first in candidates:
                    self._attach(incident, symptom, obs, first, result)
                changed[incident.seq] = incident
                self._pending = {}
            else:
                # Evidence without an incident: wait. It is dropped when it stops firing.
                self._pending = {(o.rule_id, o.subject): (s if s is not None else self._build(o, first))
                                 for s, o, first in candidates}
        else:
            self._pending = {}

        # 3. resolve timer
        incident = self.incident if (self.incident and self.incident.status == OPEN) else None
        # The interval that ends in a cycle after a gap is not quiet time: the gap covered it.
        credit = 0.0 if self._prev_gap else elapsed
        self._prev_gap = bool(gap_sources)
        if incident is not None:
            self._check_resolve(incident, bool(gap_sources), credit, now_utc, changed, result)

        # 4. derived fields of every changed incident
        for incident in changed.values():
            self._refresh(incident, now_utc)
        result.changed = sorted(changed.values(), key=lambda i: i.seq)
        return result

    # ---------------------------------------------------------------- gaps

    def _update_gaps(self, gap_sources: set, now_utc: datetime, changed: dict, result: CycleResult) -> None:
        incident = self.incident if (self.incident and self.incident.status == OPEN) else None
        for source in sorted(gap_sources - set(self._open_gaps)):
            gap = DataGap(source=source, start=now_utc)
            self._open_gaps[source] = gap
            result.events.append({"event": "data_gap_opened", "source": source})
            if incident is not None:
                incident.data_gaps.append(gap)
                changed[incident.seq] = incident
        for source in sorted(set(self._open_gaps) - gap_sources):
            gap = self._open_gaps.pop(source)
            gap.end = now_utc
            result.events.append({"event": "data_gap_closed", "source": source})
            if incident is not None and any(g is gap for g in incident.data_gaps):
                changed[incident.seq] = incident

    # ---------------------------------------------------------------- symptoms

    @staticmethod
    def _find(incident: Optional[Incident], key) -> Optional[Symptom]:
        if incident is None:
            return None
        for symptom in incident.symptoms:
            if (symptom.rule_id, symptom.service) == key:
                return symptom
        return None

    def _build(self, obs: Observation, first_seen: datetime) -> Symptom:
        meta = self._metas[obs.rule_id]
        symptom = Symptom(seq=self._next_symptom, rule_id=obs.rule_id, detector=obs.detector,
                          source=obs.source, service=obs.subject, role=meta.role, severity=meta.severity,
                          opens_incident=meta.opens_incident, first_seen=first_seen, last_seen=obs.ts,
                          active=True, flaps=0, comparator=obs.comparator, threshold=obs.threshold,
                          peak_value=obs.value, last_value=obs.value, detail=obs.detail, dependencies=[])
        self._next_symptom += 1
        self._merge_dependencies(symptom, obs.detail)
        return symptom

    def _update_symptom(self, symptom: Symptom, obs: Observation) -> None:
        symptom.last_seen = obs.ts
        symptom.last_value = obs.value
        symptom.detail = obs.detail
        symptom.threshold = obs.threshold
        if obs.value is not None:
            # peak = the most extreme firing value; for "==" (state rules) it is simply the latest
            if obs.comparator == ">" and symptom.peak_value is not None:
                symptom.peak_value = max(symptom.peak_value, obs.value)
            else:
                symptom.peak_value = obs.value
        self._merge_dependencies(symptom, obs.detail)

    def _merge_dependencies(self, symptom: Symptom, detail: str) -> None:
        if symptom.role != "dependency_effect":
            return
        for name in _DEPENDENCY.findall(detail or ""):
            if name not in symptom.dependencies:
                symptom.dependencies.append(name)
        symptom.dependencies.sort()

    def _attach(self, incident: Incident, symptom: Optional[Symptom], obs: Observation, first: datetime,
                result: CycleResult) -> None:
        """Add a qualified symptom to the incident, or reactivate the one with the same key."""
        existing = self._find(incident, (obs.rule_id, obs.subject))
        if existing is not None:
            existing.active = True
            existing.flaps += 1
            self._update_symptom(existing, obs)
            result.events.append({"event": "symptom_reactivated", "incident_id": incident.incident_id,
                                  "rule_id": obs.rule_id, "subject": obs.subject, "flaps": existing.flaps})
            return
        if symptom is None:
            symptom = self._build(obs, first)
        incident.symptoms.append(symptom)
        result.events.append({"event": "symptom_added", "incident_id": incident.incident_id,
                              "rule_id": obs.rule_id, "subject": obs.subject,
                              "first_seen": symptom.first_seen.isoformat()})

    # ---------------------------------------------------------------- incidents

    def _open_or_reopen(self, now_utc: datetime, changed: dict, result: CycleResult) -> Incident:
        last = self.incident
        self._quiet = None
        if (last is not None and last.status == RESOLVED and last.resolved_at is not None
                and (now_utc - last.resolved_at).total_seconds() <= self.reopen_within_s):
            last.status = OPEN
            last.resolved_at = None
            last.reopen_count += 1
            for gap in self._open_gaps.values():
                if not any(g is gap for g in last.data_gaps):
                    last.data_gaps.append(gap)
            changed[last.seq] = last
            result.events.append({"event": "incident_reopened", "incident_id": last.incident_id,
                                  "reopen_count": last.reopen_count})
            return last
        incident = Incident(seq=self._next_incident, status=OPEN, opened_at=now_utc, updated_at=now_utc)
        self._next_incident += 1
        incident.data_gaps.extend(self._open_gaps.values())
        self.incident = incident
        changed[incident.seq] = incident
        result.events.append({"event": "incident_opened", "incident_id": incident.incident_id})
        return incident

    def _check_resolve(self, incident: Incident, gap_now: bool, elapsed: float, now_utc: datetime,
                       changed: dict, result: CycleResult) -> None:
        if any(s.active for s in incident.symptoms):
            self._quiet = None
            return
        if gap_now:
            return                                        # a data gap pauses the timer, it never counts as quiet
        self._quiet = 0.0 if self._quiet is None else self._quiet + elapsed
        if self._quiet + self._tolerance >= self.resolve_after_s:
            incident.status = RESOLVED
            incident.resolved_at = now_utc
            self._quiet = None
            changed[incident.seq] = incident
            result.events.append({"event": "incident_resolved", "incident_id": incident.incident_id})

    # ---------------------------------------------------------------- derived fields

    def _refresh(self, incident: Incident, now_utc: datetime) -> None:
        symptoms = incident.symptoms
        incident.updated_at = now_utc
        if symptoms:
            incident.severity = max((s.severity for s in symptoms), key=lambda v: SEVERITY_RANK[v])
        incident.primary_service = self._primary(symptoms)
        first_seen: dict = {}
        affected: dict = {}
        for symptom in symptoms:
            if symptom.service is not None:
                affected[symptom.service] = min(affected.get(symptom.service, symptom.first_seen), symptom.first_seen)
                first_seen[symptom.service] = affected[symptom.service]
            for name in symptom.dependencies:
                first_seen[name] = min(first_seen.get(name, symptom.first_seen), symptom.first_seen)
        order = lambda table: [name for name, _ in sorted(table.items(), key=lambda kv: (kv[1], kv[0]))]
        incident.candidate_services = order(first_seen)
        incident.affected_services = order(affected)
        incident.title = self._title(symptoms)

    @staticmethod
    def _primary(symptoms: list) -> Optional[str]:
        """D61: no claim of a root cause. One service only when every root_signal names it
        and nothing in the incident is a load symptom."""
        if any(s.role == "load" for s in symptoms):
            return None
        roots = {s.service for s in symptoms if s.role == "root_signal"}
        if len(roots) == 1 and None not in roots:
            return next(iter(roots))
        return None

    def _title(self, symptoms: list) -> str:
        labels: list = []
        for symptom in sorted(symptoms, key=lambda s: (s.first_seen, s.rule_id, s.service or "")):
            name = self._metas[symptom.rule_id].name if symptom.rule_id in self._metas else symptom.rule_id
            label = f"{name} {symptom.service}" if symptom.service else name
            if label not in labels:
                labels.append(label)
        text = ", ".join(labels[:TITLE_PARTS])
        if len(labels) > TITLE_PARTS:
            text += f" and {len(labels) - TITLE_PARTS} more"
        return text
