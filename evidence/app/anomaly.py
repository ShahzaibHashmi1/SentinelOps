"""Anomaly rules (pure logic, no I/O).

A series is a list of Point(ts, value) taken from a range query with a 5 s step.

- high_is_bad: anomalous when the value passes max(margin x baseline_max, absolute_min); with an unclean or
  missing baseline only absolute_min counts.
- down: anomalous when the value is 0 (presence 0). An empty result is not 0: it has no verdict.
- restarts: the series holds the container start time; the signal is the number of increases of that value
  in the window.

first_anomalous_at is the first 5 s step that passes. A failed query is not handled here: the caller records
no_data and never calls these functions with invented values.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Iterable, Sequence

from .model import Point, SignalResult, epoch_to_dt
from .signals import SignalDef

_COMPARE = {">": operator.gt, ">=": operator.ge}
RESTART_EPSILON_S = 0.5  # start times are in seconds; a smaller change is noise


@dataclass(frozen=True)
class Outcome:
    base: str
    label: str | None
    state: str  # ok | no_data
    result: SignalResult | None
    detail: str = ""

    @property
    def name(self) -> str:
        return self.base if self.label is None else f"{self.base}:{self.label}"


def _key(ts: float) -> float:
    return round(ts, 3)


def apply_guard(points: Sequence[Point], guard_points: Sequence[Point], minimum: float) -> tuple[list[Point], int]:
    """Keep the points whose step has a guard value >= minimum. Returns (kept, number of steps dropped)."""
    ok = {_key(g.ts) for g in guard_points if g.value >= minimum}
    kept = [p for p in points if _key(p.ts) in ok]
    return kept, len(points) - len(kept)


def _max(points: Sequence[Point]) -> float | None:
    return max((p.value for p in points), default=None)


def _count_increases(points: Sequence[Point], start_value: float | None):
    """Return (list of (ts, running count) at every increase, last value seen)."""
    events = []
    last = start_value
    count = 0
    for p in sorted(points, key=lambda x: x.ts):
        if last is not None and p.value > last + RESTART_EPSILON_S:
            count += 1
            events.append((p.ts, count))
        last = p.value
    return events, last


def evaluate_series(
    defn: SignalDef,
    service: str,
    label: str | None,
    window: Sequence[Point],
    baseline: Sequence[Point],
    *,
    baseline_clean: bool = True,
    margin: float = 1.5,
    guard_window: Sequence[Point] | None = None,
    guard_baseline: Sequence[Point] | None = None,
) -> Outcome:
    window = sorted(window, key=lambda p: p.ts)
    baseline = sorted(baseline, key=lambda p: p.ts)
    if defn.guard is not None:
        if guard_window is None:
            return Outcome(defn.name, label, "no_data", None, "guard query unavailable")
        window, dropped = apply_guard(window, guard_window, defn.guard.min)
        if defn.guard.on_fail == "no_data" and not window and dropped:
            return Outcome(defn.name, label, "no_data", None, f"guard failed on {dropped} steps")
        if guard_baseline is None:
            baseline = []
        else:
            baseline, _ = apply_guard(baseline, guard_baseline, defn.guard.min)

    if defn.kind == "down":
        return _down(defn, label, window, baseline)
    if defn.kind == "restarts":
        return _restarts(defn, service, label, window, baseline, baseline_clean, margin)
    return _high(defn, service, label, window, baseline, baseline_clean, margin)


def _high(defn, service, label, window, baseline, baseline_clean, margin) -> Outcome:
    zero = 0.0 if defn.empty == "zero" else None
    window_max = _max(window)
    if window_max is None:
        window_max = zero
    baseline_max = _max(baseline)
    if baseline_max is None:
        baseline_max = zero
    entry = defn.threshold_for(service)
    if entry is None:  # reported, no verdict
        return Outcome(defn.name, label, "ok", SignalResult(defn.name, label, defn.unit, defn.kind, window_max, baseline_max, False, None))
    threshold = float(entry.absolute_min)
    if baseline_clean and baseline_max is not None:
        threshold = max(margin * baseline_max, threshold)
    compare = _COMPARE[defn.comparator]
    first = next((p for p in window if compare(p.value, threshold)), None)
    ratio = None if window_max is None or threshold <= 0 else window_max / threshold
    result = SignalResult(
        defn.name, label, defn.unit, defn.kind, window_max, baseline_max, first is not None,
        None if first is None else epoch_to_dt(first.ts), threshold, ratio,
    )
    return Outcome(defn.name, label, "ok", result)


def _down(defn, label, window, baseline) -> Outcome:
    first = next((p for p in window if abs(p.value) < 1e-9), None)
    result = SignalResult(
        defn.name, label, defn.unit, defn.kind, _max(window), _max(baseline), first is not None,
        None if first is None else epoch_to_dt(first.ts), None, None,
    )
    return Outcome(defn.name, label, "ok", result)


def _restarts(defn, service, label, window, baseline, baseline_clean, margin) -> Outcome:
    entry = defn.threshold_for(service)
    base_events, base_last = _count_increases(baseline, None)
    baseline_count = float(len(base_events)) if baseline else None
    events, _ = _count_increases(window, base_last)
    window_count = float(len(events)) if window else None
    if entry is None:
        return Outcome(defn.name, label, "ok", SignalResult(defn.name, label, defn.unit, defn.kind, window_count, baseline_count, False, None))
    threshold = float(entry.absolute_min)
    if baseline_clean and baseline_count is not None:
        threshold = max(margin * baseline_count, threshold)
    compare = _COMPARE[defn.comparator]
    first_ts = next((ts for ts, running in events if compare(running, threshold)), None)
    ratio = None if window_count is None else window_count / threshold
    result = SignalResult(
        defn.name, label, defn.unit, defn.kind, window_count, baseline_count, first_ts is not None,
        None if first_ts is None else epoch_to_dt(first_ts), threshold, ratio,
    )
    return Outcome(defn.name, label, "ok", result)


def evaluate_grouped(
    defn: SignalDef,
    service: str,
    windows: dict,
    baselines: dict,
    *,
    baseline_clean: bool = True,
    margin: float = 1.5,
    guard_window: Sequence[Point] | None = None,
    guard_baseline: Sequence[Point] | None = None,
) -> list[Outcome]:
    """One outcome per label value (for example per dependency), sorted by label."""
    labels = sorted(set(windows) | set(baselines))
    return [
        evaluate_series(defn, service, label, windows.get(label, ()), baselines.get(label, ()), baseline_clean=baseline_clean,
                        margin=margin, guard_window=guard_window, guard_baseline=guard_baseline)
        for label in labels
    ]


def no_data(defn: SignalDef, label: str | None, detail: str) -> Outcome:
    """A failed query: never healthy, never anomalous."""
    return Outcome(defn.name, label, "no_data", None, detail)


def split_outcomes(outcomes: Iterable[Outcome]) -> tuple[list[SignalResult], list[tuple[str, str]]]:
    """(results of the ok outcomes, [(signal name, detail)] of the no_data outcomes) in input order."""
    results, gaps = [], []
    for item in outcomes:
        if item.state == "ok" and item.result is not None:
            results.append(item.result)
        else:
            gaps.append((item.name, item.detail))
    return results, gaps
