"""Change records (D73): what changed in a container between two observations.

ChangeDetector compares an observation with the previous one of the same service and returns ChangeRecords.
ChangePoller runs it in its own thread on a container source and hands the records to the build thread through a
queue (the store has exactly one writer thread, D71).

Kinds and the time a record gets:
  recreated       Created differs (a new container)            time: the new container's own Created
  config_changed  config-hash label differs                    time: the new container's own Created when it was
                                                               recreated, otherwise the observation time
  started         status becomes running after exited/dead/created   time: the container's own StartedAt
  stopped         status becomes exited or dead                time: the container's own FinishedAt
  restarted       RestartCount increased                       time: the container's own StartedAt
  health_changed  health status differs                        time: the observation time
The own times matter: a poller that only sees a change 10 s later must not date it 10 s late (D73, reviewer
decision of CP1). A time the runtime does not provide (year 1, or in the future) falls back to the observation time.
The first observation of a service gives no record (there is no history before the start).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .logging_setup import log_event
from .model import ChangeRecord, ContainerObservation, ModelError, parse_ts

STOPPED_STATES = ("exited", "dead")
MAX_PENDING = 1000
MIN_YEAR = 2000
FUTURE_SLACK_S = 60


def own_time(value: str | None, now: datetime) -> datetime:
    """The container's own timestamp, or `now` when it is missing, unreadable, from year 1 or in the future."""
    if not value:
        return now
    try:
        parsed = parse_ts(value)
    except ModelError:
        return now
    if parsed.year < MIN_YEAR or parsed > now + timedelta(seconds=FUTURE_SLACK_S):
        return now
    return parsed


def _pick(obs: ContainerObservation, *names: str) -> dict:
    return {name: getattr(obs, name) for name in names if getattr(obs, name) is not None}


class ChangeDetector:
    def __init__(self) -> None:
        self._last: dict[str, ContainerObservation] = {}

    def update(self, observations: Iterable[ContainerObservation], now: datetime) -> list[ChangeRecord]:
        records: list[ChangeRecord] = []
        for cur in observations:
            prev = self._last.get(cur.service)
            self._last[cur.service] = cur
            if prev is None:
                continue
            records.extend(self._compare(prev, cur, now))
        records.sort(key=lambda r: (r.ts, r.kind, r.service))
        return records

    def _compare(self, prev: ContainerObservation, cur: ContainerObservation, now: datetime) -> list[ChangeRecord]:
        svc = cur.service
        if prev.created is not None and cur.created is not None and prev.created != cur.created:
            when = own_time(cur.created, now)
            out = [ChangeRecord(svc, "recreated", when, _pick(prev, "created", "status"), _pick(cur, "created", "status"))]
            if prev.config_hash != cur.config_hash and cur.config_hash is not None:
                out.append(ChangeRecord(svc, "config_changed", when, _pick(prev, "config_hash"), _pick(cur, "config_hash")))
            return out
        out = []
        if prev.status != cur.status and cur.status is not None:
            if cur.status == "running" and prev.status in (*STOPPED_STATES, "created"):
                out.append(ChangeRecord(svc, "started", own_time(cur.started_at, now), _pick(prev, "status"), _pick(cur, "status")))
            elif cur.status in STOPPED_STATES and prev.status not in STOPPED_STATES:
                out.append(ChangeRecord(svc, "stopped", own_time(cur.finished_at, now), _pick(prev, "status"), _pick(cur, "status")))
        if prev.restart_count is not None and cur.restart_count is not None and cur.restart_count > prev.restart_count:
            out.append(ChangeRecord(svc, "restarted", own_time(cur.started_at, now), _pick(prev, "restart_count"), _pick(cur, "restart_count")))
        if prev.config_hash != cur.config_hash and cur.config_hash is not None and prev.config_hash is not None:
            out.append(ChangeRecord(svc, "config_changed", now, _pick(prev, "config_hash"), _pick(cur, "config_hash")))
        if prev.health != cur.health and cur.health is not None:
            out.append(ChangeRecord(svc, "health_changed", now, _pick(prev, "health"), _pick(cur, "health")))
        return out


class ChangePoller:
    """Polls a container source (status() and observe(now)), keeps the latest observations and a queue of new
    change records. The build thread calls drain() and writes the records to the store."""

    def __init__(self, source, metrics=None, logger: logging.Logger | None = None,
                 utcnow: Callable[[], datetime] | None = None):
        self._source = source
        self._metrics = metrics
        self._logger = logger
        self._utcnow = utcnow
        self._detector = ChangeDetector()
        self._lock = threading.Lock()
        self._pending: deque = deque()
        self._current: dict[str, ContainerObservation] = {}
        self.polls = 0

    def poll_once(self, now: datetime) -> int:
        """One poll. Returns the number of new change records."""
        self.polls += 1
        observed = self._source.observe(now)
        if observed is None:  # off or unreachable: the source status tells which
            with self._lock:
                self._current = {}
            return 0
        records = self._detector.update(observed, now)
        with self._lock:
            self._current = {o.service: o for o in observed}
            for rec in records:
                if len(self._pending) >= MAX_PENDING:
                    self._pending.popleft()
                self._pending.append(rec)
        for rec in records:
            if self._metrics is not None:
                self._metrics.count_change(rec.kind)
            if self._logger is not None:
                log_event(self._logger, logging.INFO, "change_detected", change_service=rec.service, kind=rec.kind)
        return len(records)

    def drain(self) -> list[ChangeRecord]:
        with self._lock:
            out = list(self._pending)
            self._pending.clear()
        return out

    def requeue(self, records: Iterable[ChangeRecord]) -> None:
        """Put records back at the front (the store refused them); order is kept."""
        with self._lock:
            for rec in reversed(list(records)):
                self._pending.appendleft(rec)
            while len(self._pending) > MAX_PENDING:
                self._pending.pop()

    def snapshot(self) -> dict:
        """Latest observation per service; empty when the last poll did not succeed (never stale data)."""
        with self._lock:
            return dict(self._current)

    def run(self, stop: threading.Event, interval_s: float) -> None:
        next_at = time.monotonic()
        while not stop.is_set():
            try:
                now = self._utcnow() if self._utcnow else datetime.now(timezone.utc)
                self.poll_once(now)
            except Exception as exc:  # a failed poll is a log line, never the end of the thread
                if self._logger is not None:
                    log_event(self._logger, logging.ERROR, "change_poll_failed", error=f"{exc.__class__.__name__}: {exc}")
            next_at += interval_s
            wait = next_at - time.monotonic()
            if wait < 0:  # late: do not try to catch up
                next_at = time.monotonic()
                wait = 0
            stop.wait(wait)
