"""Fake sources, a fake clock and a harness that drives detector + correlator. No network, no Docker."""
import _bootstrap  # noqa: F401
from datetime import datetime, timedelta, timezone

from app.correlator import Correlator
from app.model import Observation
from app.rules import load_rules
from app.sources import SOURCE_OK, Sample, Source, SourceStatus
from app.thresholds import ThresholdDetector

T0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
APP4 = ["gateway", "orders", "payments", "inventory"]
ALL6 = APP4 + ["postgres", "redis"]


class FakeClock:
    def __init__(self, start=T0):
        self.mono = 1000.0
        self.utc = start

    def advance(self, seconds):
        self.mono += seconds
        self.utc += timedelta(seconds=seconds)


class FakeSource(Source):
    """query() answers from a dict keyed by the exact expression; an unknown expression is an
    EMPTY result (a normal answer), an Exception value is raised."""

    def __init__(self, name):
        self.name = name
        self.state = SOURCE_OK
        self.detail = ""
        self.data = {}
        self.queries = []

    def status(self):
        return SourceStatus(self.state, None, self.detail)

    def query(self, expr):
        self.queries.append(expr)
        value = self.data.get(expr, [])
        if isinstance(value, Exception):
            raise value
        return value


def by(label, mapping, **extra):
    return [Sample({label: k, **extra}, float(v)) for k, v in mapping.items()]


def scalar(value):
    return [Sample({}, float(value))]


class Driver:
    """Feeds hand-made observations to a Correlator on a fake clock (5 s per step)."""

    def __init__(self, metas, interval=5, **kwargs):
        self.clock = FakeClock()
        self.correlator = Correlator(metas, **kwargs)
        self.interval = interval
        self.results = []

    def obs(self, rule_id, subject, state, value=None, source="prometheus", detail="", comparator=">", threshold=1.0):
        return Observation(rule_id, "threshold", source, subject, state, value, threshold, comparator,
                           self.clock.utc, detail)

    def step(self, *observations):
        result = self.correlator.process(list(observations), self.clock.mono, self.clock.utc)
        self.results.append(result)
        self.clock.advance(self.interval)
        return result

    def run(self, seconds, *observations_factory):
        """Repeat the same observations (given as tuples for self.obs) for `seconds`."""
        last = None
        for _ in range(int(seconds // self.interval)):
            last = self.step(*[self.obs(*spec[:3], **(spec[3] if len(spec) > 3 else {}))
                               for spec in observations_factory])
        return last

    @property
    def incident(self):
        return self.correlator.incident


class Harness:
    """Real rules.json + fake sources + the real ThresholdDetector and Correlator."""

    def __init__(self, resolve_after_s=60, reopen_within_s=120, interval=5):
        from _bootstrap import RULES_JSON
        self.rules = load_rules(RULES_JSON)
        self.by_id = {r.id: r for r in self.rules}
        self.prom = FakeSource("prometheus")
        self.loki = FakeSource("loki")
        self.detector = ThresholdDetector(self.rules, {"prometheus": self.prom, "loki": self.loki})
        self.correlator = Correlator([r.meta() for r in self.rules], resolve_after_s=resolve_after_s,
                                     reopen_within_s=reopen_within_s)
        self.clock = FakeClock()
        self.interval = interval
        self.events = []
        self.healthy()

    def source_of(self, rule):
        return self.prom if rule.source == "prometheus" else self.loki

    def set(self, rule_id, samples, traffic=None, guard=None):
        rule = self.by_id[rule_id]
        src = self.source_of(rule)
        src.data[rule.query] = samples
        if traffic is not None:
            src.data[rule.min_traffic.query] = traffic
        if guard is not None:
            src.data[rule.guard] = guard

    def healthy(self):
        self.set("R01", by("job", {s: 1 for s in APP4}))
        self.set("R02", by("service", {s: 1 for s in ALL6}), guard=scalar(1))
        self.set("R03", by("service", {s: 15 for s in ALL6}))
        self.set("R04", by("service", {s: 25 for s in ALL6}))
        self.set("R05", by("service", {"gateway": 0.0}), traffic=by("service", {"gateway": 10.0}))
        self.set("R06", [])
        self.set("R07", by("service", {s: 0.02 for s in APP4}), traffic=by("service", {s: 5.0 for s in APP4}))
        self.set("R08", by("service", {"gateway": 19.0}))
        self.set("R09", [])

    def tick(self):
        observations = self.detector.evaluate(self.clock.utc)
        result = self.correlator.process(observations, self.clock.mono, self.clock.utc)
        self.events.extend(result.events)
        self.clock.advance(self.interval)
        return result

    def run(self, seconds):
        result = None
        for _ in range(int(seconds // self.interval)):
            result = self.tick()
        return result

    @property
    def incident(self):
        return self.correlator.incident
