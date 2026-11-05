"""Loader for docs/topology.yaml (service -> list of dependencies).

The file format is tiny, so a strict parser is used instead of a YAML library:

    caller:
      - dependency

Anything else is rejected with the line number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_CALLER = re.compile(r"^([a-z][a-z0-9_-]*):\s*$")
_DEP = re.compile(r"^\s+-\s+([a-z][a-z0-9_-]*)\s*$")


class TopologyError(ValueError):
    """The topology file is malformed."""


@dataclass(frozen=True)
class Topology:
    deps: dict  # caller -> tuple of direct dependencies, in file order
    services: tuple  # every service, sorted by name
    tiers: dict  # service -> "app" (calls others) | "infra" (only ever called)

    def tier(self, service: str) -> str:
        try:
            return self.tiers[service]
        except KeyError:
            raise TopologyError(f"unknown service: {service!r}") from None


def parse_topology(text: str) -> Topology:
    deps: dict[str, list[str]] = {}
    current: str | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        caller = _CALLER.match(line)
        if caller and not line[0].isspace():
            current = caller.group(1)
            if current in deps:
                raise TopologyError(f"line {number}: caller {current!r} appears twice")
            deps[current] = []
            continue
        dep = _DEP.match(line)
        if dep:
            if current is None:
                raise TopologyError(f"line {number}: dependency before any caller")
            name = dep.group(1)
            if name == current:
                raise TopologyError(f"line {number}: {current!r} depends on itself")
            if name in deps[current]:
                raise TopologyError(f"line {number}: duplicate dependency {name!r} of {current!r}")
            deps[current].append(name)
            continue
        raise TopologyError(f"line {number}: cannot parse {raw!r}")
    if not deps:
        raise TopologyError("topology is empty")
    for caller, items in deps.items():
        if not items:
            raise TopologyError(f"caller {caller!r} has no dependency")
    names = set(deps)
    for items in deps.values():
        names.update(items)
    for name in names:
        if not _NAME.match(name):
            raise TopologyError(f"bad service name {name!r}")
    tiers = {name: ("app" if name in deps else "infra") for name in names}
    return Topology(
        deps={k: tuple(v) for k, v in deps.items()},
        services=tuple(sorted(names)),
        tiers=tiers,
    )


def load_topology(path: str) -> Topology:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return parse_topology(handle.read())
    except OSError as exc:
        raise TopologyError(f"cannot read {path}: {exc}") from exc
