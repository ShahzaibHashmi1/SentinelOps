"""Entry point of the container: python -m app.main

Settings come from environment variables (defaults below). The start order matters:
rules (an invalid file stops the detector with a clear error) -> store -> correlator restored from the
store -> API (so /health answers while the first cycle runs) -> evaluation thread.
Stop: SIGTERM or SIGINT end the loop, the API and close the store.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import logging_setup
from .api import ApiContext, make_server
from .correlator import Correlator
from .loop import EvaluationLoop, LoopState
from .logging_setup import log_event
from .metrics import Metrics
from .rules import RulesError, load_rules
from .sources import HTTP_TIMEOUT_S, loki_source, prometheus_source, urllib_transport
from .store import Store
from .thresholds import ThresholdDetector

API_HOST = "0.0.0.0"
API_PORT = 9100
RULES_PATH = Path(__file__).resolve().parent.parent / "rules" / "rules.json"
SERVER_POLL_S = 0.05          # how fast serve_forever notices a stop request (default 0.5 s)
EXIT_SETTINGS = 2
EXIT_RULES = 3
EXIT_STORE = 4


class SettingsError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    eval_interval_s: float = 5.0
    warmup_s: float = 30.0
    resolve_after_s: float = 60.0
    reopen_within_s: float = 120.0
    db_path: str = "/data/detector.db"
    log_level: str = "INFO"
    version: str = "dev"

    @classmethod
    def from_environ(cls, env) -> "Settings":
        problems: list = []

        def number(name: str, default: float, minimum: float, exclusive: bool = False) -> float:
            raw = env.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                value = float(raw)
            except ValueError:
                problems.append(f"{name}={raw!r} is not a number")
                return default
            if not (value == value) or value in (float("inf"), float("-inf")):
                problems.append(f"{name}={raw!r} is not a finite number")
                return default
            if value < minimum or (exclusive and value == minimum):
                problems.append(f"{name}={raw!r} must be {'above' if exclusive else 'at least'} {minimum:g}")
                return default
            return value

        def text(name: str, default: str) -> str:
            raw = env.get(name)
            return default if raw is None or raw.strip() == "" else raw.strip()

        prometheus_url = text("PROMETHEUS_URL", cls.prometheus_url)
        loki_url = text("LOKI_URL", cls.loki_url)
        for name, value in (("PROMETHEUS_URL", prometheus_url), ("LOKI_URL", loki_url)):
            if not value.startswith(("http://", "https://")):
                problems.append(f"{name}={value!r} must start with http:// or https://")
        level = text("LOG_LEVEL", cls.log_level).upper()
        if not logging_setup.valid_level(level):
            problems.append(f"LOG_LEVEL={level!r} must be DEBUG, INFO, WARNING or ERROR")
            level = cls.log_level
        settings = cls(prometheus_url=prometheus_url, loki_url=loki_url,
                       eval_interval_s=number("EVAL_INTERVAL_S", cls.eval_interval_s, 0.0, exclusive=True),
                       warmup_s=number("WARMUP_S", cls.warmup_s, 0.0),
                       resolve_after_s=number("RESOLVE_AFTER_S", cls.resolve_after_s, 0.0, exclusive=True),
                       reopen_within_s=number("REOPEN_WITHIN_S", cls.reopen_within_s, 0.0),
                       db_path=text("DB_PATH", cls.db_path), log_level=level,
                       version=text("APP_VERSION", cls.version))
        if problems:
            raise SettingsError(problems)
        return settings

    def public(self) -> dict:
        """Shown by /internal/v1/status. There are no secrets in the detector."""
        return {"prometheus_url": self.prometheus_url, "loki_url": self.loki_url,
                "eval_interval_s": self.eval_interval_s, "warmup_s": self.warmup_s,
                "resolve_after_s": self.resolve_after_s, "reopen_within_s": self.reopen_within_s,
                "db_path": self.db_path, "log_level": self.log_level, "http_timeout_s": HTTP_TIMEOUT_S}


class App:
    """All parts, wired. start() runs the API and the evaluation thread, stop() ends both."""

    def __init__(self, settings: Settings, rules: list, store: Store, logger: logging.Logger,
                 transport: Callable = urllib_transport, port: int = API_PORT, host: str = API_HOST):
        self.settings = settings
        self.rules = rules
        self.store = store
        self.log = logger
        state = store.load_state()
        self.restored = state.latest
        self.correlator = Correlator([r.meta() for r in rules], resolve_after_s=settings.resolve_after_s,
                                     reopen_within_s=settings.reopen_within_s,
                                     next_incident_seq=state.next_incident_seq,
                                     next_symptom_seq=state.next_symptom_seq)
        self.correlator.restore(state.latest)
        self.sources = [prometheus_source(settings.prometheus_url, settings.warmup_s, transport=transport),
                        loki_source(settings.loki_url, settings.warmup_s, transport=transport)]
        detector = ThresholdDetector(rules, {s.name: s for s in self.sources})
        self.state = LoopState()
        self.metrics = Metrics()
        self.loop = EvaluationLoop([detector], self.sources, self.correlator, store, settings.eval_interval_s,
                                   self.state, self.metrics, logger)
        ctx = ApiContext(db_path=settings.db_path, rules=rules, state=self.state, metrics=self.metrics,
                         settings=settings.public(), version=settings.version,
                         interval_s=settings.eval_interval_s, logger=logger)
        self.server = make_server(ctx, host, port)
        self._stop = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._server_thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        """Start the API and the loop in background threads (used by tests)."""
        self._server_thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": SERVER_POLL_S}, name="api",
                                              daemon=True)
        self._server_thread.start()
        self._loop_thread = threading.Thread(target=self.loop.run_forever, args=(self._stop,), name="evaluation",
                                             daemon=True)
        self._loop_thread.start()

    def serve_until_stopped(self) -> None:
        """Run the loop in a thread and the API in the calling thread until stop() is called."""
        self._loop_thread = threading.Thread(target=self.loop.run_forever, args=(self._stop,), name="evaluation",
                                             daemon=True)
        self._loop_thread.start()
        self.server.serve_forever(poll_interval=SERVER_POLL_S)

    def request_stop(self) -> None:
        """Ask the loop and the API to end (safe to call from a signal handler)."""
        self._stop.set()
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def stop(self) -> None:
        """Stop both threads and close the API socket and the store."""
        self.request_stop()
        for thread in (self._loop_thread, self._server_thread):
            if thread is not None:
                thread.join(timeout=10)
        self.server.server_close()
        self.store.close()


def main(environ=None) -> int:
    env = os.environ if environ is None else environ
    try:
        settings = Settings.from_environ(env)
    except SettingsError as exc:
        logger = logging_setup.configure("INFO", env.get("APP_VERSION", "dev"))
        log_event(logger, logging.ERROR, "settings_invalid", problems=exc.problems)
        return EXIT_SETTINGS
    logger = logging_setup.configure(settings.log_level, settings.version)

    try:
        rules = load_rules(RULES_PATH)
    except RulesError as exc:
        log_event(logger, logging.ERROR, "rules_invalid", path=str(RULES_PATH), problems=exc.problems)
        return EXIT_RULES
    try:
        store = Store(settings.db_path)
    except Exception as exc:  # StoreError, sqlite3.Error or a path problem: all stop the start
        log_event(logger, logging.ERROR, "store_unavailable", path=settings.db_path,
                  error=f"{exc.__class__.__name__}: {exc}")
        return EXIT_STORE

    app = App(settings, rules, store, logger)

    def on_signal(signum, frame):
        log_event(logger, logging.INFO, "stopping", signal=signal.Signals(signum).name)
        app.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log_event(logger, logging.INFO, "detector_started", port=API_PORT, rules_loaded=len(rules),
              settings=settings.public(),
              restored_incident=(app.restored.incident_id if app.restored else None),
              restored_status=(app.restored.status if app.restored else None))
    try:
        app.serve_until_stopped()
    finally:
        app.stop()
        log_event(logger, logging.INFO, "detector_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
