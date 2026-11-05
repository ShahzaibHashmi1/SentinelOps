"""Builders for the evidence tests (no I/O except reading the real config files)."""
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401
from _bootstrap import RANKING_PATH, SIGNALS_PATH, TOPOLOGY_PATH

from app.model import (ChangeRecord, ContainerObservation, DetectorHint, HintSymptom, LogEvidence, Point,
                       ServiceEvidence, SignalResult)
from app.ranking import load_ranking
from app.signals import load_signals
from app.topology import load_topology
from app.graph import DependencyGraph

T0 = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
SERVICES = ("gateway", "inventory", "orders", "payments", "postgres", "redis")

TOPOLOGY = load_topology(TOPOLOGY_PATH)
GRAPH = DependencyGraph(TOPOLOGY)
SIGNALS = load_signals(SIGNALS_PATH, set(TOPOLOGY.services), TOPOLOGY.tiers)
RANKING = load_ranking(RANKING_PATH)
DEPS = {name: tuple(GRAPH.dependencies(name)) for name in GRAPH.nodes()}


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def pts(values, start=0.0, step=5.0):
    """Points from a list of values; None entries are missing steps."""
    out = []
    for i, v in enumerate(values):
        if v is not None:
            out.append(Point(T0.timestamp() + start + i * step, float(v)))
    return out


def sig(base, label=None, *, anomalous=True, first=0.0, ratio=2.0, kind="high_is_bad", unit="x", window_max=1.0, baseline_max=0.0):
    return SignalResult(base, label, unit, kind, window_max, baseline_max, anomalous,
                        at(first) if anomalous else None, 1.0, None if kind == "down" else ratio)


def down(base, first=0.0):
    return sig(base, kind="down", first=first, window_max=1.0)


def svc(name, signals=(), *, logs=None, container=None, changes=(), gaps=()):
    return ServiceEvidence(name, TOPOLOGY.tier(name), tuple(signals), logs, container, tuple(changes), tuple(gaps))


def stack(**per_service):
    """All six services; per_service maps a name to a list of signals or a ready ServiceEvidence."""
    out = {}
    for name in SERVICES:
        item = per_service.get(name, ())
        out[name] = item if isinstance(item, ServiceEvidence) else svc(name, item)
    return out


def hint(primary=None, candidates=(), roles=()):
    symptoms = tuple(HintSymptom(rule, service, role, at(first)) for rule, service, role, first in roles)
    return DetectorHint(primary, tuple(candidates), symptoms)


def change(service, kind, seconds, **new):
    return ChangeRecord(service, kind, at(seconds), {}, new)


def logs(window, baseline, events=(), samples=(), ids=()):
    return LogEvidence("ok", window, baseline, tuple(events), tuple(samples), tuple(ids))


def box(service, status="running", **kw):
    return ContainerObservation(service, status=status, **kw)


def rank_scenario(services, h):
    from app.ranking import rank
    anomalous = [n for n, e in services.items() if e.is_anomalous()]
    shared = GRAPH.shared_dependencies(anomalous, RANKING.param("shared_dependency", "min_dependents"))
    return rank(services, DEPS, shared, h, RANKING)


# ---------------------------------------------------------------- scenarios (shapes of the M3 faults)

def scenario_f10():
    s = stack(
        postgres=[down("container_present", 0)],
        orders=[sig("dependency_error_rate", "postgres", first=5), sig("error_ratio_5xx", first=10), sig("latency_p95", first=15)],
        inventory=[sig("dependency_error_rate", "postgres", first=5), sig("latency_p95", first=15)],
        payments=[sig("dependency_error_rate", "postgres", first=5)],
        gateway=[sig("dependency_error_rate", "orders", first=10), sig("error_ratio_5xx", first=10), sig("latency_p95", first=20)],
    )
    s["orders"] = svc("orders", s["orders"].signals, logs=logs(200, 0))
    return s, hint("postgres", ["inventory", "orders", "postgres", "gateway"], [("R02", "postgres", "root_signal", 0)])


def scenario_f13():
    s = stack(
        redis=[down("container_present", 0)],
        inventory=[sig("dependency_error_rate", "redis", first=5)],
        payments=[sig("dependency_error_rate", "redis", first=5)],
    )
    return s, hint("redis", ["inventory", "payments", "redis"], [("R02", "redis", "root_signal", 0)])


def scenario_f06():
    s = stack(
        inventory=svc("inventory", [down("up", 3), sig("restarts", first=10, kind="high_is_bad", window_max=5.0)],
                      container=box("inventory", "restarting"), changes=[change("inventory", "recreated", -2)]),
        orders=[sig("dependency_error_rate", "inventory", first=10), sig("error_ratio_5xx", first=15)],
        gateway=[sig("error_ratio_5xx", first=20), sig("dependency_error_rate", "orders", first=20)],
    )
    return s, hint("inventory", ["inventory", "orders", "gateway"], [("R01", "inventory", "root_signal", 3)])


def scenario_f08():
    s = stack(
        orders=svc("orders", [sig("dependency_error_rate", "postgres", first=10)], changes=[change("orders", "config_changed", -3)]),
        gateway=[sig("error_ratio_5xx", first=10), sig("dependency_error_rate", "orders", first=15)],
    )
    return s, hint(None, ["orders", "postgres", "gateway"], [("R06", "orders", "dependency_effect", 10), ("R05", "gateway", "effect", 12)])


def scenario_f01():
    s = stack(orders=[sig("cpu_pct_of_limit", first=40, ratio=1.1)], gateway=[sig("latency_p95", first=60, ratio=1.4)])
    return s, hint("orders", ["orders", "gateway"], [("R03", "orders", "root_signal", 40)])


def scenario_f02():
    s = stack(payments=[sig("memory_pct_of_limit", first=30, ratio=1.05)], orders=[sig("latency_p95", first=50, ratio=1.3)])
    return s, hint("payments", ["payments", "orders"], [("R04", "payments", "root_signal", 30)])


def scenario_f11():
    s = stack(
        inventory=[sig("latency_p95", first=30, ratio=3.0), sig("dependency_latency_p95", "postgres", first=30, ratio=3.0)],
        orders=[sig("latency_p95", first=35, ratio=3.0), sig("dependency_latency_p95", "inventory", first=35, ratio=3.0)],
        gateway=[sig("error_ratio_5xx", first=40, ratio=2.4), sig("latency_p95", first=40, ratio=3.0)],
    )
    return s, hint(None, ["inventory", "orders", "gateway", "postgres"], [("R07", "inventory", "effect", 30), ("R05", "gateway", "effect", 40)])


def scenario_f18():
    s = stack(
        gateway=[sig("request_rate", first=50, ratio=1.1), sig("cpu_pct_of_limit", first=60, ratio=1.2), sig("latency_p95", first=10, ratio=3.0)],
        orders=[sig("latency_p95", first=10, ratio=2.0)],
    )
    return s, hint(None, ["gateway", "orders"], [("R07", "gateway", "effect", 10), ("R08", "gateway", "load", 50), ("R03", "gateway", "root_signal", 60)])
