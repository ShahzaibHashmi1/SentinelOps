"""Prometheus text format, written by hand. No label is named "service" (docs/OBSERVABILITY.md gotcha)."""
from __future__ import annotations

import threading

from .model import CHANGE_KINDS, TRIGGERS


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    return repr(float(value)) if isinstance(value, float) else str(value)


class Metrics:
    def __init__(self, source_names):
        self._lock = threading.Lock()
        self._cycles = 0
        self._last_cycle_ts = 0.0
        self._cycle_duration = 0.0
        self._bundles = {t: 0 for t in TRIGGERS}
        self._build_duration = 0.0
        self._sources = {name: 0 for name in source_names}
        self._changes = {k: 0 for k in CHANGE_KINDS}

    def finish_cycle(self, finished_ts: float, duration_s: float) -> None:
        with self._lock:
            self._cycles += 1
            self._last_cycle_ts = finished_ts
            self._cycle_duration = duration_s

    def count_bundle(self, trigger: str, build_s: float) -> None:
        with self._lock:
            self._bundles[trigger] = self._bundles.get(trigger, 0) + 1
            self._build_duration = build_s

    def set_source(self, name: str, ok: bool) -> None:
        with self._lock:
            self._sources[name] = 1 if ok else 0

    def count_change(self, kind: str) -> None:
        with self._lock:
            self._changes[kind] = self._changes.get(kind, 0) + 1

    def render(self) -> str:
        with self._lock:
            out = []

            def block(name, kind, help_text, samples):
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} {kind}")
                for labels, value in samples:
                    label_text = "" if not labels else "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels) + "}"
                    out.append(f"{name}{label_text} {_number(value)}")

            block("evidence_build_cycles_total", "counter", "Build cycles finished since the start.", [((), self._cycles)])
            block("evidence_last_cycle_timestamp_seconds", "gauge", "Unix time at which the last cycle finished (0 before the first).", [((), self._last_cycle_ts)])
            block("evidence_cycle_duration_seconds", "gauge", "Duration of the last cycle.", [((), self._cycle_duration)])
            block("evidence_bundles_total", "counter", "Bundles written since the start, by trigger.", [((("trigger", t),), n) for t, n in sorted(self._bundles.items())])
            block("evidence_build_duration_seconds", "gauge", "Duration of the last bundle build.", [((), self._build_duration)])
            block("evidence_source_status", "gauge", "1 when the source is ok, 0 when not (no_data, warming_up or off).", [((("source", s),), v) for s, v in sorted(self._sources.items())])
            block("evidence_changes_total", "counter", "Change records detected since the start, by kind.", [((("kind", k),), n) for k, n in sorted(self._changes.items())])
            return "\n".join(out) + "\n"
