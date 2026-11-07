"""The build loop (one thread). Every EVIDENCE_INTERVAL_S seconds:

  1. readiness and warm-up of the detector, Prometheus and Loki (docs/DETECTOR.md section 8 rules);
  2. change records found by the poller thread are written to the store (this thread is the only writer);
  3. the incidents are read from the detector; for each one a trigger may fire:
       opened    SETTLE_S after this process first saw the incident open, when it has no bundle yet
       refresh   REFRESH_S after its last bundle, only while it is open and its symptom set or a last_seen changed
       resolved  once, after the incident resolved (only for an incident that has bundles or was seen open here;
                 older history is never back-filled)
  4. for each trigger a bundle is built and stored (at most MAX_BUILDS_PER_CYCLE per cycle).

A failed source is a state, never an exception that stops the loop. While Prometheus is not ok no bundle is
built (a bundle without signals would be immutable and useless); the trigger fires when it is ok again.
Loki or the container source not ok only leaves data_gaps in the bundle. Timers use the monotonic clock,
stored times are UTC datetimes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .bundle import build_bundle, compute_window, size_of
from .graph import DependencyGraph
from .logging_setup import log_event
from .metrics import Metrics
from .model import ChangeRecord, ModelError, fmt_ts, parse_ts
from .ranking import RankingConfig
from .signals import SignalsConfig
from .sources import QueryFailed, SourceStatus
from .store import Store, StoreReader
from .topology import Topology

MAX_BUILDS_PER_CYCLE = 2
RETRY_AFTER_FAILURE_S = 30.0
INCIDENT_LIMIT = 50
CHANGE_READ_LIMIT = 1000


class RealClock:
    def mono(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class Track:
    first_seen_mono: float
    last_build_mono: float | None = None
    signature: tuple | None = None
    seen_open: bool = False
    retry_after: float = 0.0
    skipped: bool = False  # resolved history that is deliberately not back-filled


@dataclass(frozen=True)
class LoopSnapshot:
    sources: dict
    last_cycle_at: datetime | None
    last_cycle_mono: float | None
    duration_s: float | None
    cycles: int
    open_incidents_tracked: int


class LoopState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = LoopSnapshot({}, None, None, None, 0, 0)

    def update(self, sources: dict, finished_at: datetime, finished_mono: float, duration_s: float, open_tracked: int) -> None:
        with self._lock:
            self._snap = LoopSnapshot(dict(sources), finished_at, finished_mono, duration_s, self._snap.cycles + 1, open_tracked)

    def snapshot(self) -> LoopSnapshot:
        with self._lock:
            return self._snap


def signature(incident: Mapping) -> tuple:
    """Symptom set (id, active) and the newest last_seen: a refresh is due when this changed."""
    syms = incident.get("symptoms") or []
    ids = tuple(sorted((str(s.get("symptom_id")), bool(s.get("active"))) for s in syms if isinstance(s, dict)))
    newest = max((str(s.get("last_seen") or "") for s in syms if isinstance(s, dict)), default="")
    return ids, newest


def to_change_record(row: Mapping) -> ChangeRecord | None:
    try:
        return ChangeRecord(row["service"], row["kind"], parse_ts(row["ts"]), dict(row.get("old") or {}), dict(row.get("new") or {}))
    except (KeyError, ModelError, TypeError):
        return None


class BuildLoop:
    def __init__(self, *, detector, prom, loki, runtime, poller, collector, store: Store, reader: StoreReader, topology: Topology,
                 graph: DependencyGraph, signals_cfg: SignalsConfig, ranking_cfg: RankingConfig, metrics: Metrics, state: LoopState,
                 logger: logging.Logger, interval_s: float, settle_s: float, refresh_s: float, clock=None):
        self.detector, self.prom, self.loki, self.runtime = detector, prom, loki, runtime
        self.poller, self.collector = poller, collector
        self.store, self.reader = store, reader
        self.topology, self.graph = topology, graph
        self.signals_cfg, self.ranking_cfg = signals_cfg, ranking_cfg
        self.metrics, self.state, self.logger = metrics, state, logger
        self.interval_s, self.settle_s, self.refresh_s = float(interval_s), float(settle_s), float(refresh_s)
        self.clock = clock or RealClock()
        self.tracks: dict[str, Track] = {}
        self._deferred_logged = False

    # ---------------------------------------------------------------- sources

    def source_statuses(self) -> dict:
        names = self.signals_cfg.sources
        runtime = self.signals_cfg.runtime_source
        out: dict = {}
        for name in names:
            if name == "detector":
                out[name] = self.detector.status()
            elif name == "prometheus":
                out[name] = self.prom.status()
            elif name == "loki":
                out[name] = self.loki.status()
            elif name == runtime:
                out[name] = self.runtime.status()
            else:
                out[name] = SourceStatus("ok", None, "")
        return out

    # ---------------------------------------------------------------- one cycle

    def run_cycle(self) -> None:
        started = self.clock.mono()
        now = self.clock.utcnow()
        try:
            for source in (self.detector, self.prom, self.loki):
                source.begin_cycle(started, now)
            self._persist_changes()
            self._process_incidents(started, now)
        except Exception as exc:  # the loop never stops because of one bad cycle
            log_event(self.logger, logging.ERROR, "cycle_failed", error=f"{exc.__class__.__name__}: {exc}")
        finally:
            statuses = self.source_statuses()
            for name, status in statuses.items():
                self.metrics.set_source(name, status.state == "ok")
            finished = self.clock.mono()
            duration = finished - started
            self.metrics.finish_cycle(self.clock.utcnow().timestamp(), duration)
            open_tracked = sum(1 for t in self.tracks.values() if t.seen_open and not t.skipped)
            self.state.update(statuses, self.clock.utcnow(), finished, duration, open_tracked)
            if duration > self.interval_s:
                log_event(self.logger, logging.WARNING, "cycle_late", duration_s=round(duration, 3), interval_s=self.interval_s)

    def _persist_changes(self) -> None:
        if self.poller is None:
            return
        records = self.poller.drain()
        if not records:
            return
        try:
            self.store.add_changes(records)
        except Exception as exc:
            self.poller.requeue(records)
            log_event(self.logger, logging.ERROR, "store_write_failed", what="changes", error=f"{exc.__class__.__name__}: {exc}")

    # ---------------------------------------------------------------- incidents

    def _process_incidents(self, mono: float, now: datetime) -> None:
        if not self.detector.is_ok():
            return
        try:
            incidents = self.detector.incidents(limit=INCIDENT_LIMIT)
        except QueryFailed as exc:
            log_event(self.logger, logging.WARNING, "incidents_unavailable", error=str(exc)[:160])
            return
        wanted = []
        for incident in incidents:
            try:
                trigger = self._decide(incident, mono, now)
            except Exception as exc:
                log_event(self.logger, logging.ERROR, "incident_check_failed", incident_id=str(incident.get("incident_id")),
                          error=f"{exc.__class__.__name__}: {exc}")
                continue
            if trigger is not None:
                wanted.append((trigger, incident))
        order = {"opened": 0, "resolved": 1, "refresh": 2}
        wanted.sort(key=lambda item: (order[item[0]], str(item[1].get("incident_id"))))
        built = 0
        for trigger, incident in wanted:
            if built >= MAX_BUILDS_PER_CYCLE:
                break
            if not self.prom.is_ok():
                if not self._deferred_logged:
                    log_event(self.logger, logging.WARNING, "build_deferred", reason=f"prometheus is {self.prom.status().state}", pending=len(wanted))
                    self._deferred_logged = True
                break
            self._deferred_logged = False
            if self._build(trigger, incident, incidents, mono, now):
                built += 1

    def _decide(self, incident: Mapping, mono: float, now: datetime) -> str | None:
        incident_id = incident.get("incident_id")
        status = incident.get("status")
        if not isinstance(incident_id, str) or status not in ("open", "resolved"):
            return None
        track = self.tracks.get(incident_id)
        if track is None:
            track = self.tracks[incident_id] = Track(first_seen_mono=mono)
        if status == "open":
            track.seen_open = True
            track.skipped = False  # a reopened incident is looked at again
        if track.skipped or mono < track.retry_after:
            return None
        bundles = self.reader.list_bundles(incident_id)
        triggers = {b["trigger"] for b in bundles}
        if status == "open":
            if not bundles:
                return "opened" if mono - track.first_seen_mono >= self.settle_s else None
            age = self._age_since_last_build(track, bundles, mono, now)
            if age >= self.refresh_s and signature(incident) != track.signature:
                return "refresh"
            return None
        if "resolved" in triggers:
            track.skipped = True  # nothing more to do for this incident
            return None
        if bundles or track.seen_open:
            return "resolved"
        track.skipped = True
        log_event(self.logger, logging.INFO, "incident_skipped_history", incident_id=incident_id)
        return None

    @staticmethod
    def _age_since_last_build(track: Track, bundles: list, mono: float, now: datetime) -> float:
        if track.last_build_mono is not None:
            return mono - track.last_build_mono
        try:
            return (now - parse_ts(bundles[-1]["built_at"])).total_seconds()
        except (KeyError, ModelError):
            return float("inf")

    # ---------------------------------------------------------------- build

    def _changes_by_service(self, since: datetime) -> dict:
        rows = self.reader.list_changes(since=fmt_ts(since), limit=CHANGE_READ_LIMIT)
        out: dict = {}
        for row in rows:
            rec = to_change_record(row)
            if rec is not None:
                out.setdefault(rec.service, []).append(rec)
        return out

    def _build(self, trigger: str, incident: Mapping, incidents: list, mono: float, now: datetime) -> bool:
        incident_id = incident["incident_id"]
        track = self.tracks[incident_id]
        started = self.clock.mono()
        try:
            window = compute_window(incident, incidents, now, self.signals_cfg.window)
            lookback = window.start.timestamp() - self.signals_cfg.window.change_lookback_s
            since = datetime.fromtimestamp(lookback, tz=timezone.utc)
            containers = self.poller.snapshot() if self.poller is not None else {}
            services = self.collector.collect(window, containers, self._changes_by_service(since))
            statuses = self.source_statuses()
            states = {name: (st.state, st.detail) for name, st in statuses.items()}
            build_ms = int((self.clock.mono() - started) * 1000)
            bundle = build_bundle(
                bundle_id=self.store.next_bundle_id(), incident=incident, trigger=trigger, built_at=self.clock.utcnow(),
                build_ms=build_ms, window=window, source_states=states, source_names=self.signals_cfg.sources, services=services,
                topology=self.topology, graph=self.graph, ranking_cfg=self.ranking_cfg, window_cfg=self.signals_cfg.window)
            bundle_id = self.store.insert_bundle(bundle)
        except Exception as exc:  # any failure of one build: log it, pause that incident, keep the loop alive
            track.retry_after = mono + RETRY_AFTER_FAILURE_S
            log_event(self.logger, logging.ERROR, "bundle_failed", incident_id=incident_id, trigger=trigger,
                      error=f"{exc.__class__.__name__}: {exc}")
            return False
        track.last_build_mono = mono
        track.signature = signature(incident)
        self.metrics.count_bundle(trigger, (self.clock.mono() - started))
        log_event(self.logger, logging.INFO, "bundle_written", bundle_id=bundle_id, incident_id=incident_id, trigger=trigger,
                  build_ms=build_ms, size_bytes=size_of(bundle), suspects=len(bundle["suspects"]),
                  top=bundle["suspects"][0]["service"] if bundle["suspects"] else None, truncated=len(bundle["truncated"]))
        return True

    # ---------------------------------------------------------------- thread

    def run(self, stop: threading.Event) -> None:
        next_at = self.clock.mono()
        while not stop.is_set():
            self.run_cycle()
            next_at += self.interval_s
            wait = next_at - self.clock.mono()
            if wait < 0:  # late: skip, do not catch up
                next_at = self.clock.mono()
                wait = 0
            stop.wait(wait)
