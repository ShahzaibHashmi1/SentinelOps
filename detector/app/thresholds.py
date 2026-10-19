"""ThresholdDetector: rules + sources -> observations. It never decides about incidents.

For every rule and every subject (a service) it yields exactly one Observation per evaluation:
  firing   the condition is true
  ok       the condition is false, or the subject is missing and on_missing is "ok",
           or a rate guard says there is too little traffic to judge
  no_data  the rule could not be evaluated (source not ok, query failed, guard not satisfied,
           value is NaN or infinite). Missing data is a status, never a verdict (D59).

An EMPTY query result is a normal answer: a subject that is missing from it is handled by
on_missing ("zero": evaluated as 0, "ok": ok) and is never silently dropped. The subjects of a
rule are: expected_subjects, the subjects seen in earlier evaluations, and the subjects in the
current result.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from .model import FIRING, NO_DATA, OK, Detector, Observation
from .rules import Rule
from .sources import SOURCE_OK, QueryFailed, Sample, Source

# Labels that identify a series but say nothing new in a symptom detail.
_IGNORED_DETAIL_LABELS = {"__name__", "instance", "job", "service"}
_EQUAL_EPSILON = 1e-9


def compare(comparator: str, value: float, threshold: float) -> bool:
    """">" is a strict comparison: a value equal to the threshold does not fire."""
    if comparator == ">":
        return value > threshold
    if comparator == "==":
        return abs(value - threshold) < _EQUAL_EPSILON
    raise ValueError(f"unknown comparator {comparator!r}")


def _fmt(value: float) -> str:
    return f"{value:.4g}"


class ThresholdDetector(Detector):
    name = "threshold"

    def __init__(self, rules, sources: dict):
        self.rules = list(rules)
        self.sources = dict(sources)
        for rule in self.rules:
            if rule.source not in self.sources:
                raise ValueError(f"rule {rule.id} needs the source {rule.source!r}, which is not configured")
        # Subjects seen so far per rule: a subject that disappears later is evaluated by on_missing.
        self._known: dict = {rule.id: set() for rule in self.rules}

    def evaluate(self, now: datetime) -> list:
        observations: list = []
        for rule in self.rules:
            observations.extend(self._evaluate_rule(rule, now))
        return observations

    # ------------------------------------------------------------------ one rule

    def _evaluate_rule(self, rule: Rule, now: datetime) -> list:
        source: Source = self.sources[rule.source]
        status = source.status()
        if status.state != SOURCE_OK:
            why = f"{rule.source} {status.state}" + (f": {status.detail}" if status.detail else "")
            return self._all_no_data(rule, now, why)

        try:
            if rule.guard:
                guard = source.query(rule.guard)
                guard_value = guard[0].value if guard else None
                if guard_value is None or not math.isfinite(guard_value) or guard_value <= 0:
                    return self._all_no_data(rule, now, "guard not satisfied (data source not usable)")
            samples = source.query(rule.query)
            traffic: Optional[dict] = None
            if rule.min_traffic is not None:
                traffic = self._traffic(source.query(rule.min_traffic.query), rule.min_traffic.subject_label)
        except QueryFailed as exc:
            return self._all_no_data(rule, now, f"query failed: {exc}")

        by_subject: dict = {}
        for sample in samples:
            if rule.subject_label is None:
                subject = None
            else:
                subject = sample.labels.get(rule.subject_label)
                if not subject:
                    continue  # a series that names no subject cannot be attributed to anyone
            by_subject.setdefault(subject, []).append(sample)

        subjects = set(by_subject) | self._known[rule.id]
        if rule.expected_subjects:
            subjects |= set(rule.expected_subjects)
        if rule.subject_label is None:
            subjects = {None}
        self._known[rule.id] |= {s for s in by_subject if s is not None}

        result = []
        for subject in sorted(subjects, key=lambda s: (s is not None, s or "")):
            state, value, detail = self._judge(rule, by_subject.get(subject))
            if traffic is not None:   # the rate guard comes first: NaN without traffic is ok, not no_data
                seen = traffic.get(subject, 0.0)
                if seen < rule.min_traffic.min:
                    state, value = OK, None
                    detail = f"traffic {_fmt(seen)}/s is below the minimum {_fmt(rule.min_traffic.min)}/s"
            result.append(self._observation(rule, now, subject, state, value, detail))
        return result

    def _judge(self, rule: Rule, samples: Optional[list]):
        """(state, value, detail) of one subject; samples is None when the subject is missing."""
        if samples is None:
            if rule.on_missing == "zero":
                state = FIRING if compare(rule.comparator, 0.0, rule.threshold) else OK
                return state, 0.0, "missing from the result, evaluated as 0"
            return OK, None, "missing from the result"
        finite = [s for s in samples if math.isfinite(s.value)]
        if not finite:
            return NO_DATA, None, "value is NaN or infinite"
        firing = [s for s in finite if compare(rule.comparator, s.value, rule.threshold)]
        if firing:
            return FIRING, max(s.value for s in firing), self._detail(rule, firing)
        return OK, max(s.value for s in finite), ""

    @staticmethod
    def _detail(rule: Rule, firing: list) -> str:
        """Extra labels of the firing series, for example "dependency=postgres (0.41)" for R06."""
        parts = []
        for sample in sorted(firing, key=lambda s: -s.value):
            labels = " ".join(f"{k}={v}" for k, v in sorted(sample.labels.items())
                              if k not in _IGNORED_DETAIL_LABELS and k != rule.subject_label)
            if labels:
                parts.append(f"{labels} ({_fmt(sample.value)})")
        return "; ".join(parts)

    @staticmethod
    def _traffic(samples: list, subject_label: Optional[str]) -> dict:
        result: dict = {}
        for sample in samples:
            if not math.isfinite(sample.value):
                continue
            subject = None if subject_label is None else sample.labels.get(subject_label)
            if subject is None and subject_label is not None:
                continue
            result[subject] = result.get(subject, 0.0) + sample.value
        return result

    # --------------------------------------------------------------- observations

    def _all_no_data(self, rule: Rule, now: datetime, why: str) -> list:
        subjects = set(self._known[rule.id]) | set(rule.expected_subjects or ())
        if rule.subject_label is None or not subjects:
            subjects = {None}  # one observation without a subject still records the data gap
        return [self._observation(rule, now, s, NO_DATA, None, why)
                for s in sorted(subjects, key=lambda s: (s is not None, s or ""))]

    @staticmethod
    def _observation(rule: Rule, now: datetime, subject, state, value, detail) -> Observation:
        return Observation(rule_id=rule.id, detector="threshold", source=rule.source, subject=subject,
                           state=state, value=value, threshold=rule.threshold,
                           comparator=rule.comparator, ts=now, detail=detail)
