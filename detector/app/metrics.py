"""Prometheus text format, written by hand (no client library).

Never use a label named "service" on these metrics (docs/OBSERVABILITY.md gotcha): the labels are
source, rule_id and subject. The scrape job is called "detector".
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

STATE_VALUE = {"firing": 1, "ok": 0, "no_data": -1}


@dataclass
class MetricsSnapshot:
    """Everything /metrics shows, filled by the evaluation loop once per cycle."""
    cycles: int = 0
    last_evaluation_timestamp_s: float = 0.0
    duration_s: float = 0.0
    source_ok: dict = field(default_factory=dict)        # source -> True when the state is "ok"
    rule_states: dict = field(default_factory=dict)      # (rule_id, subject) -> "firing" | "ok" | "no_data"
    rule_values: dict = field(default_factory=dict)      # (rule_id, subject) -> last evaluated value
    incidents_open: int = 0
    incidents_total: int = 0
    query_errors: dict = field(default_factory=dict)     # source -> failed queries so far


def _num(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape("" if v is None else str(v))}"' for k, v in labels.items()) + "}"


class Metrics:
    """Thread-safe holder: the loop publishes a snapshot, the API thread renders it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = MetricsSnapshot()

    def publish(self, snapshot: MetricsSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def render(self) -> str:
        with self._lock:
            s = self._snapshot
        out: list = []

        def block(name: str, kind: str, help_text: str, samples: list) -> None:
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")
            out.extend(f"{name}{labels} {_num(value)}" for labels, value in samples)

        block("detector_evaluation_cycles_total", "counter", "Evaluation cycles finished since the start.",
              [("", s.cycles)])
        block("detector_last_evaluation_timestamp_seconds", "gauge",
              "Unix time at which the last evaluation cycle finished (0 before the first one).",
              [("", s.last_evaluation_timestamp_s)])
        block("detector_evaluation_duration_seconds", "gauge", "Duration of the last evaluation cycle.",
              [("", s.duration_s)])
        block("detector_source_status", "gauge", "1 when the data source is ok, 0 when it is no_data or warming_up.",
              [(_labels(source=name), 1 if ok else 0) for name, ok in sorted(s.source_ok.items())])
        block("detector_rule_state", "gauge", "Last state per rule and subject: 1 firing, 0 ok, -1 no_data.",
              [(_labels(rule_id=rid, subject=sub), STATE_VALUE[state])
               for (rid, sub), state in sorted(s.rule_states.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))])
        block("detector_rule_value", "gauge",
              "Last evaluated value per rule and subject (no series while there is no value).",
              [(_labels(rule_id=rid, subject=sub), value)
               for (rid, sub), value in sorted(s.rule_values.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))])
        block("detector_incidents_open", "gauge", "Open incidents (0 or 1).", [("", s.incidents_open)])
        block("detector_incidents_total", "counter", "Incidents created so far (continues across restarts).",
              [("", s.incidents_total)])
        block("detector_query_errors_total", "counter", "Failed queries per data source since the start.",
              [(_labels(source=name), count) for name, count in sorted(s.query_errors.items())])
        return "\n".join(out) + "\n"
