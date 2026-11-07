"""The query side of a bundle: signals from Prometheus, log evidence from Loki, containers and changes.

One range query per signal covers baseline and window together (baseline_start to the end of the window) and is
split at window.start; guard queries are shared when their text is equal. The independent queries run in a small
thread pool so that a bundle stays far below the 5 s limit; results are assembled in a fixed order.

A failed query makes only that signal no_data (never healthy, never anomalous). When a source is no longer ok,
the remaining queries to it are skipped at once, so an outage costs one timeout, not one per query.
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Mapping, Sequence

from . import anomaly
from .bundle import Window
from .model import ChangeRecord, ContainerObservation, LogEvidence, Point, ServiceEvidence
from .redact import clean_line, clean_text
from .signals import SignalDef, SignalsConfig, render
from .sources import LokiSource, PrometheusSource, QueryFailed
from .topology import Topology

MAX_POINTS = 10000  # Prometheus refuses more than 11000 points per series; stay below


def choose_step(span_s: float, base_step: int) -> int:
    """The configured step, or a larger multiple of it when the span would exceed the point limit."""
    if span_s / base_step <= MAX_POINTS:
        return base_step
    return int(math.ceil(span_s / MAX_POINTS / base_step) * base_step)


def _split(points: Sequence[Point], boundary: float) -> tuple[list, list]:
    """(baseline points before the boundary, window points from the boundary on)."""
    return [p for p in points if p.ts < boundary], [p for p in points if p.ts >= boundary]


class Collector:
    def __init__(self, prom: PrometheusSource, loki: LokiSource, cfg: SignalsConfig, topology: Topology, workers: int = 4):
        self.prom = prom
        self.loki = loki
        self.cfg = cfg
        self.topology = topology
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="collect")

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    # ---------------------------------------------------------------- public

    def collect(self, window: Window, containers: Mapping[str, ContainerObservation],
                changes: Mapping[str, Sequence[ChangeRecord]]) -> dict:
        """ServiceEvidence for every service of the topology."""
        signals = self._collect_signals(window)
        logs = self._collect_logs(window)
        out = {}
        for service in self.topology.services:
            results, gaps = signals.get(service, ([], []))
            log_ev, log_gaps = logs.get(service, (None, []))
            out[service] = ServiceEvidence(
                name=service,
                tier=self.topology.tier(service),
                signals=tuple(results),
                logs=log_ev,
                container=containers.get(service),
                changes=tuple(changes.get(service, ())),
                gaps=tuple(gaps) + tuple(log_gaps),
            )
        return out

    # ---------------------------------------------------------------- signals

    def _range(self, expr: str, t0: float, t1: float, step: int, by: str | None) -> dict:
        if not self.prom.is_ok():
            raise QueryFailed(f"prometheus is {self.prom.status().state}")
        return self.prom.range_series(expr, t0, t1, step, by)

    def _collect_signals(self, window: Window) -> dict:
        t0, t1 = window.baseline_start.timestamp(), window.end.timestamp()
        boundary = window.start.timestamp()
        step = choose_step(t1 - t0, self.cfg.window.step_s)
        defs_by_tier = {tier: self.cfg.signals_for(tier) for tier in ("app", "infra")}

        # shared guard queries (same text -> one query)
        guard_texts: dict = {}
        for service in self.topology.services:
            for defn in defs_by_tier[self.topology.tier(service)]:
                if defn.guard is not None:
                    guard_texts[render(defn.guard.query, service=service)] = None
        guard_futures = {text: self._pool.submit(self._guard, text, t0, t1, step) for text in guard_texts}
        guards = {text: fut.result() for text, fut in guard_futures.items()}  # (points or None, error text)

        jobs = []
        for service in self.topology.services:
            for defn in defs_by_tier[self.topology.tier(service)]:
                jobs.append((service, defn, self._pool.submit(self._one_signal, service, defn, window, t0, t1, step, boundary, guards)))
        out: dict = {service: ([], []) for service in self.topology.services}
        for service, defn, fut in jobs:
            outcomes = fut.result()
            results, gaps = anomaly.split_outcomes(outcomes)
            out[service][0].extend(results)
            out[service][1].extend(gaps)
        return out

    def _guard(self, text: str, t0: float, t1: float, step: int):
        try:
            series = self._range(text, t0, t1, step, None)
        except QueryFailed as exc:
            return None, str(exc)
        return series.get(None, []), ""

    def _one_signal(self, service: str, defn: SignalDef, window: Window, t0, t1, step, boundary, guards) -> list:
        guard_window = guard_baseline = None
        if defn.guard is not None:
            points, error = guards[render(defn.guard.query, service=service)]
            if points is None:
                return [anomaly.no_data(defn, None, f"guard query failed: {clean_text(error)}")]
            guard_baseline, guard_window = _split(points, boundary)
        try:
            series = self._range(render(defn.query, service=service), t0, t1, step, defn.by)
        except QueryFailed as exc:
            return [anomaly.no_data(defn, None, clean_text(str(exc)))]
        kwargs = dict(baseline_clean=window.baseline_clean, margin=self.cfg.window.baseline_margin,
                      guard_window=guard_window, guard_baseline=guard_baseline)
        if defn.by is None:
            baseline, win = _split(series.get(None, []), boundary)
            return [anomaly.evaluate_series(defn, service, None, win, baseline, **kwargs)]
        windows, baselines = {}, {}
        for label, points in series.items():
            baselines[label], windows[label] = _split(points, boundary)
        return anomaly.evaluate_grouped(defn, service, windows, baselines, **kwargs)

    # ---------------------------------------------------------------- logs

    def _collect_logs(self, window: Window) -> dict:
        jobs = [(service, self._pool.submit(self._one_service_logs, service, window)) for service in self.topology.services]
        return {service: fut.result() for service, fut in jobs}

    def _one_service_logs(self, service: str, window: Window):
        cfg = self.cfg.logs
        tier = self.topology.tier(service)
        if not self.loki.is_ok():
            return LogEvidence("no_data"), [("logs", f"loki is {self.loki.status().state}")]
        selector = render(cfg.selectors[tier], service=service)
        win_s = max(1, int((window.end - window.start).total_seconds()))
        base_s = max(1, int((window.baseline_end - window.baseline_start).total_seconds()))
        try:
            window_count = self._count(selector, win_s, window.end.timestamp())
            baseline_count = self._count(selector, base_s, window.baseline_end.timestamp())
            events = ()
            if tier in cfg.events_tiers:
                events = self._events(selector, win_s, window.end.timestamp())
            samples, ids = self._samples(selector, window)
        except QueryFailed as exc:
            return LogEvidence("no_data"), [("logs", clean_text(str(exc)))]
        return LogEvidence("ok", window_count, baseline_count, events, samples, ids), []

    def _count(self, selector: str, range_s: int, at: float) -> int:
        expr = render(self.cfg.logs.count_query, selector=selector, range=range_s)
        rows = self.loki.instant_vector(expr, at)
        return int(sum(value for _labels, value in rows))  # an empty answer means no matching lines

    def _events(self, selector: str, range_s: int, at: float) -> tuple:
        expr = render(self.cfg.logs.events_query, selector=selector, range=range_s, top=self.cfg.logs.top_events)
        rows = []
        for labels, value in self.loki.instant_vector(expr, at):
            name = clean_text(str(labels.get("event", "")), 80)
            if name and name != "[withheld]":
                rows.append((name, int(value)))
        rows.sort(key=lambda r: (-r[1], r[0]))
        return tuple(rows[: self.cfg.logs.top_events])

    def _samples(self, selector: str, window: Window):
        cfg = self.cfg.logs
        if cfg.samples == 0 and cfg.request_ids == 0:
            return (), ()
        expr = render(cfg.samples_query, selector=selector)
        lines = self.loki.log_lines(expr, window.start.timestamp(), window.end.timestamp(), cfg.fetch_lines)
        samples, ids = [], []
        for _ts, line in lines:
            if len(samples) < cfg.samples:
                cleaned = clean_line(line, cfg.sample_chars)
                if cleaned is not None:
                    samples.append(cleaned)
            if len(ids) < cfg.request_ids:
                rid = _request_id(line)
                if rid and rid not in ids:
                    ids.append(rid)
            if len(samples) >= cfg.samples and len(ids) >= cfg.request_ids:
                break
        return tuple(samples), tuple(ids)


def _request_id(line: str) -> str | None:
    """The request_id field of a JSON log line, cleaned; None when absent, null or unusable."""
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line).get("request_id")
    except (ValueError, AttributeError):
        return None
    if not isinstance(value, str) or not value:
        return None
    cleaned = clean_text(value, 64)
    return None if cleaned == "[withheld]" or "[REDACTED]" in cleaned else cleaned
