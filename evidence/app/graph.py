"""Dependency graph (NetworkX). An edge a -> b means "a calls b" (a depends on b).

Only this module imports NetworkX; everything it returns is plain Python (lists, dicts, ints).
"""
from __future__ import annotations

from typing import Iterable

import networkx as nx

from .topology import Topology


class GraphError(ValueError):
    """The graph is invalid (cycle) or a query names an unknown service."""


class DependencyGraph:
    def __init__(self, topology: Topology):
        graph = nx.DiGraph()
        graph.add_nodes_from(topology.services)
        for caller, items in topology.deps.items():
            for dep in items:
                graph.add_edge(caller, dep)
        if not nx.is_directed_acyclic_graph(graph):
            cycle = nx.find_cycle(graph)
            path = " -> ".join([cycle[0][0]] + [edge[1] for edge in cycle])
            raise GraphError(f"dependency cycle: {path}")
        self._g = graph
        order = list(nx.topological_sort(graph))
        depth: dict[str, int] = {}
        for node in reversed(order):  # dependencies first
            succ = list(graph.successors(node))
            depth[node] = 0 if not succ else 1 + max(depth[s] for s in succ)
        self._depth = depth

    # -- helpers
    def _check(self, service: str) -> None:
        if service not in self._g:
            raise GraphError(f"unknown service: {service!r}")

    def nodes(self) -> list[str]:
        return sorted(self._g.nodes)

    def edges(self) -> list[list[str]]:
        return sorted([a, b] for a, b in self._g.edges)

    def dependencies(self, service: str) -> list[str]:
        """Direct dependencies (what `service` calls)."""
        self._check(service)
        return sorted(self._g.successors(service))

    def dependents(self, service: str) -> list[str]:
        """Direct dependents (who calls `service`)."""
        self._check(service)
        return sorted(self._g.predecessors(service))

    def descendants(self, service: str) -> list[str]:
        """Everything `service` depends on, transitively."""
        self._check(service)
        return sorted(nx.descendants(self._g, service))

    def ancestors(self, service: str) -> list[str]:
        """Everything that depends on `service`, transitively."""
        self._check(service)
        return sorted(nx.ancestors(self._g, service))

    def depth(self) -> dict[str, int]:
        """Length of the longest dependency chain below each service (a service without dependencies: 0)."""
        return {k: self._depth[k] for k in sorted(self._depth)}

    def shared_dependencies(self, anomalous: Iterable[str], min_dependents: int = 2) -> dict[str, list[str]]:
        """Services that at least `min_dependents` anomalous services call directly.

        Only direct callers count: with transitive callers a single chain (gateway -> orders -> postgres)
        would count as several independent victims of one dependency.
        """
        names = set(anomalous)
        for name in names:
            self._check(name)
        result: dict[str, list[str]] = {}
        for node in sorted(self._g.nodes):
            callers = sorted(set(self._g.predecessors(node)) & names)
            if len(callers) >= min_dependents:
                result[node] = callers
        return result

    def section(self, anomalous: Iterable[str], min_dependents: int = 2) -> dict:
        """The `graph` part of an evidence bundle."""
        return {
            "nodes": self.nodes(),
            "edges": self.edges(),
            "depth": self.depth(),
            "shared_dependencies": self.shared_dependencies(anomalous, min_dependents),
        }
