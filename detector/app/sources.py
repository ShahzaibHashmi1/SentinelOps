"""The interface of a data source. The detector reads ONLY Prometheus and Loki (decision D58).

CP1 contains only this interface (the threshold detector and the unit tests use it, with fake
sources). The HTTP clients with the readiness check, the warm-up and the no-data handling are
added to this file in CP2.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

SOURCE_OK = "ok"
SOURCE_NO_DATA = "no_data"
SOURCE_WARMING_UP = "warming_up"


@dataclass(frozen=True)
class Sample:
    """One element of an instant-query vector: its labels and its numeric value."""
    labels: dict = field(default_factory=dict)
    value: float = 0.0


@dataclass(frozen=True)
class SourceStatus:
    state: str                              # "ok", "no_data" or "warming_up"
    since: Optional[datetime] = None
    detail: str = ""


class QueryFailed(Exception):
    """A query could not be answered (timeout, HTTP error, bad response).

    An EMPTY result is not a failure: it is a normal answer and the rule's on_missing decides.
    """


class Source(ABC):
    name: str = ""                          # "prometheus" or "loki"

    @abstractmethod
    def status(self) -> SourceStatus:
        """Cheap: answered from the state of the current cycle, no network call per rule."""

    @abstractmethod
    def query(self, expr: str) -> list:
        """Instant query -> list of Sample. Raises QueryFailed."""
