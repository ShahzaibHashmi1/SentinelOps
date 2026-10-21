"""The evaluation loop: one thread, one cycle every EVAL_INTERVAL_S.

A cycle: every source checks its readiness (begin_cycle), every detector yields observations, the
correlator turns them into symptoms and incidents, the store writes what changed in ONE transaction,
then the state for the API and the metrics is published. A cycle never overlaps the next: when one
is late, the missed ticks are skipped and logged. Holds use time.monotonic (the start of the cycle),
stored times are UTC.

A failed store write is not lost: the incidents are written again in the next cycle.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .logging_setup import log_event
from .metrics import Metrics, MetricsSnapshot
from .model import FIRING, NO_DATA, OPEN, utc_now


@dataclass
class LoopSnapshot:
    """What the API shows; replaced as a whole after every cycle."""
    cycles: int = 0
    last_cycle_at: Optional[datetime] = None
    last_cycle_mono: Optional[float] = None
    duration_s: Optional[float] = None
    sources: dict = field(default_factory=dict)       # name -> SourceStatus
    observations: list = field(default_factory=list)  # the Observation list of the last cycle


class LoopState:
    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = LoopSnapshot()

    def publish(self, snapshot: LoopSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self) -> LoopSnapshot:
        with self._lock:
            return self._snapshot


class EvaluationLoop:
    def __init__(self, detectors, sources, correlator, store, interval_s: float, state: LoopState,
                 metrics: Metrics, logger: logging.Logger, mono=time.monotonic, utc=utc_now, wall=time.time):
        self.detectors = list(detectors)
        self.sources = list(sources)
        self.correlator = correlator
        self.store = store
        self.interval_s = float(interval_s)
        self.state = state
        self.metrics = metrics
        self.log = logger
        self._mono = mono
        self._utc = utc
        self._wall = wall
        self._cycles = 0
        self._unsaved: dict = {}            # incident seq -> Incident whose last write failed
        self._last_source_state: dict = {}

    # ------------------------------------------------------------------ one cycle

    def run_cycle(self) -> None:
        started = self._mono()
        now = self._utc()

        for source in self.sources:
            try:
                source.begin_cycle(started, now)
            except Exception as exc:  # a broken source check must not stop the loop
                log_event(self.log, logging.ERROR, "source_check_failed", source=source.name,
                          error=f"{exc.__class__.__name__}: {exc}")
        self._log_source_changes()

        observations: list = []
        for detector in self.detectors:
            try:
                observations.extend(detector.evaluate(now))
            except Exception as exc:  # no observations from this detector: holds reset, nothing fires
                log_event(self.log, logging.ERROR, "detector_failed", detector=detector.name,
                          error=f"{exc.__class__.__name__}: {exc}")

        result = self.correlator.process(observations, started, now)
        self._write(result)
        self._log_events(result)

        duration = self._mono() - started
        self._cycles += 1
        self._publish(observations, duration, now)
        log_event(self.log, logging.DEBUG, "cycle", cycle=self._cycles, duration_ms=round(duration * 1000, 1),
                  observations=len(observations),
                  firing=sum(1 for o in observations if o.state == FIRING),
                  no_data=sum(1 for o in observations if o.state == NO_DATA))

    def _write(self, result) -> None:
        for incident in result.changed:
            self._unsaved[incident.seq] = incident
        if not self._unsaved:
            return
        try:
            self.store.save(list(self._unsaved.values()))
            self._unsaved.clear()
        except Exception as exc:  # keep the incidents and retry in the next cycle
            log_event(self.log, logging.ERROR, "store_write_failed", incidents=sorted(self._unsaved),
                      error=f"{exc.__class__.__name__}: {exc}")

    def _log_events(self, result) -> None:
        by_id = {i.incident_id: i for i in result.changed}
        for event in result.events:
            fields = dict(event)
            name = fields.pop("event")
            incident = by_id.get(fields.get("incident_id"))
            if incident is not None and name in ("incident_opened", "incident_reopened", "incident_resolved"):
                fields.update(severity=incident.severity, title=incident.title,
                              primary_service=incident.primary_service,
                              candidate_services=incident.candidate_services)
            warn = name in ("data_gap_opened", "unknown_rule_ignored")
            log_event(self.log, logging.WARNING if warn else logging.INFO, name, **fields)

    def _log_source_changes(self) -> None:
        for source in self.sources:
            status = source.status()
            if self._last_source_state.get(source.name) != status.state:
                previous = self._last_source_state.get(source.name)
                self._last_source_state[source.name] = status.state
                level = logging.INFO if status.state == "ok" else logging.WARNING
                log_event(self.log, level, "source_state_changed", source=source.name, state=status.state,
                          previous=previous, detail=status.detail)

    def _publish(self, observations: list, duration: float, now: datetime) -> None:
        statuses = {s.name: s.status() for s in self.sources}
        self.state.publish(LoopSnapshot(cycles=self._cycles, last_cycle_at=now, last_cycle_mono=self._mono(),
                                        duration_s=duration, sources=statuses, observations=list(observations)))
        incident = self.correlator.incident
        snap = MetricsSnapshot(
            cycles=self._cycles,
            last_evaluation_timestamp_s=self._wall(),
            duration_s=duration,
            source_ok={name: status.state == "ok" for name, status in statuses.items()},
            rule_states={(o.rule_id, o.subject): o.state for o in observations},
            rule_values={(o.rule_id, o.subject): o.value for o in observations if o.value is not None},
            incidents_open=1 if (incident is not None and incident.status == OPEN) else 0,
            incidents_total=self.correlator.next_incident_seq - 1,
            query_errors={s.name: getattr(s, "query_errors", 0) for s in self.sources})
        self.metrics.publish(snap)

    # ------------------------------------------------------------------ forever

    def run_forever(self, stop: threading.Event) -> None:
        next_at = self._mono()
        while not stop.is_set():
            try:
                self.run_cycle()
            except Exception as exc:  # the thread must never die
                log_event(self.log, logging.ERROR, "cycle_failed", error=f"{exc.__class__.__name__}: {exc}")
            next_at += self.interval_s
            now = self._mono()
            if now >= next_at:
                skipped = int((now - next_at) // self.interval_s) + 1
                log_event(self.log, logging.WARNING, "cycle_late", late_s=round(now - next_at, 3),
                          skipped_ticks=skipped, interval_s=self.interval_s)
                next_at += skipped * self.interval_s
            stop.wait(max(0.0, next_at - self._mono()))
