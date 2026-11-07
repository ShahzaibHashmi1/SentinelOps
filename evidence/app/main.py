"""Entry point: settings, wiring, threads. Exit codes: 2 invalid settings, 3 invalid configuration or topology,
4 unusable database path."""
from __future__ import annotations

import importlib
import os
import signal
import sys
import threading
from dataclasses import dataclass

from . import api
from .changes import ChangePoller
from .collector import Collector
from .graph import DependencyGraph, GraphError
from .logging_setup import log_event, setup_logging
from .loop import BuildLoop, LoopState
from .metrics import Metrics
from .ranking import RankingConfigError, load_ranking
from .signals import SignalsConfigError, load_signals
from .sources import DetectorSource, LokiSource, PrometheusSource, urllib_transport
from .store import Store, StoreError, StoreReader
from .topology import TopologyError, load_topology

import logging

EXIT_SETTINGS, EXIT_CONFIG, EXIT_STORE = 2, 3, 4


class SettingsError(ValueError):
    """One or more settings are invalid."""


@dataclass(frozen=True)
class Settings:
    prometheus_url: str
    loki_url: str
    detector_url: str
    interval_s: float
    warmup_s: float
    settle_s: float
    refresh_s: float
    change_poll_s: float
    db_path: str
    topology_path: str
    config_dir: str
    log_level: str
    version: str
    port: int

    def public(self) -> dict:
        """The settings reported by /internal/v1/status (none of them is secret)."""
        return {
            "prometheus_url": self.prometheus_url, "loki_url": self.loki_url, "detector_url": self.detector_url,
            "interval_s": self.interval_s, "warmup_s": self.warmup_s, "settle_s": self.settle_s, "refresh_s": self.refresh_s,
            "change_poll_s": self.change_poll_s, "db_path": self.db_path, "log_level": self.log_level, "port": self.port,
        }


_NUMBERS = (
    ("EVIDENCE_INTERVAL_S", "interval_s", 5, 1, 60), ("WARMUP_S", "warmup_s", 30, 0, 600), ("SETTLE_S", "settle_s", 15, 0, 300),
    ("REFRESH_S", "refresh_s", 60, 5, 3600), ("CHANGE_POLL_S", "change_poll_s", 10, 1, 60),
)
_URLS = (("PROMETHEUS_URL", "prometheus_url", "http://prometheus:9090"), ("LOKI_URL", "loki_url", "http://loki:3100"),
         ("DETECTOR_URL", "detector_url", "http://detector:9100"))


def parse_settings(environ) -> Settings:
    problems, values = [], {}
    for env, key, default in _URLS:
        raw = (environ.get(env) or default).strip()
        if not raw.startswith(("http://", "https://")) or len(raw) < 9:
            problems.append(f"{env} must be an http(s) URL")
        values[key] = raw
    for env, key, default, low, high in _NUMBERS:
        raw = environ.get(env)
        if raw is None or raw.strip() == "":
            values[key] = float(default)
            continue
        try:
            number = float(raw)
        except ValueError:
            problems.append(f"{env} must be a number")
            continue
        if not low <= number <= high:
            problems.append(f"{env} must be from {low} to {high}")
        values[key] = number
    level = (environ.get("LOG_LEVEL") or "INFO").strip().upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        problems.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
    port_raw = environ.get("EVIDENCE_PORT") or "9200"
    try:
        port = int(port_raw)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        problems.append("EVIDENCE_PORT must be a port number")
        port = 9200
    if problems:
        raise SettingsError("; ".join(problems))
    return Settings(
        prometheus_url=values["prometheus_url"], loki_url=values["loki_url"], detector_url=values["detector_url"],
        interval_s=values["interval_s"], warmup_s=values["warmup_s"], settle_s=values["settle_s"], refresh_s=values["refresh_s"],
        change_poll_s=values["change_poll_s"], db_path=(environ.get("DB_PATH") or "/data/evidence.db"),
        topology_path=(environ.get("TOPOLOGY_PATH") or "/app/docs/topology.yaml"), config_dir=(environ.get("CONFIG_DIR") or "/app/config"),
        log_level=level, version=(environ.get("APP_VERSION") or "dev"), port=port)


@dataclass
class Runtime:
    settings: Settings
    loop: BuildLoop
    poller: ChangePoller
    runtime_source: object
    collector: Collector
    ctx: api.ApiContext
    store: Store


def build_runtime(settings: Settings, environ, logger: logging.Logger, transport=urllib_transport, runtime_source=None) -> Runtime:
    """Wire everything. Raises TopologyError, GraphError, SignalsConfigError, RankingConfigError, StoreError, ValueError."""
    topology = load_topology(settings.topology_path)
    graph = DependencyGraph(topology)
    signals_cfg = load_signals(os.path.join(settings.config_dir, "signals.json"), set(topology.services), topology.tiers)
    ranking_cfg = load_ranking(os.path.join(settings.config_dir, "ranking.json"))
    if runtime_source is None:
        # the module of the container-runtime source is named by the configuration: <runtime_source>_source
        module = importlib.import_module(f".{signals_cfg.runtime_source}_source", package=__package__)
        runtime_source = module.create(environ, topology.services)  # a bad switch raises here, before any file is opened
    try:
        store = Store(settings.db_path)
    except (OSError, Exception) as exc:  # sqlite3.Error and OS errors both mean an unusable path
        raise StoreError(f"cannot open {settings.db_path}: {exc}") from exc
    reader = StoreReader(settings.db_path)
    metrics = Metrics(signals_cfg.sources)
    detector = DetectorSource(settings.detector_url, transport=transport)
    prom = PrometheusSource(settings.prometheus_url, settings.warmup_s, transport=transport)
    loki = LokiSource(settings.loki_url, settings.warmup_s, transport=transport)
    poller = ChangePoller(runtime_source, metrics, logger)
    collector = Collector(prom, loki, signals_cfg, topology)
    state = LoopState()
    loop = BuildLoop(detector=detector, prom=prom, loki=loki, runtime=runtime_source, poller=poller, collector=collector, store=store,
                     reader=reader, topology=topology, graph=graph, signals_cfg=signals_cfg, ranking_cfg=ranking_cfg, metrics=metrics,
                     state=state, logger=logger, interval_s=settings.interval_s, settle_s=settings.settle_s, refresh_s=settings.refresh_s)
    ctx = api.ApiContext(reader=reader, graph=graph, topology=topology, state=state, metrics=metrics, settings=settings.public(),
                         version=settings.version, interval_s=settings.interval_s, logger=logger)
    return Runtime(settings, loop, poller, runtime_source, collector, ctx, store)


def main() -> int:
    environ = os.environ
    try:
        settings = parse_settings(environ)
    except SettingsError as exc:
        logger = setup_logging("INFO", environ.get("APP_VERSION") or "dev")
        log_event(logger, logging.ERROR, "settings_invalid", error=str(exc))
        return EXIT_SETTINGS
    logger = setup_logging(settings.log_level, settings.version)
    try:
        runtime = build_runtime(settings, environ, logger)
    except (TopologyError, GraphError, SignalsConfigError, RankingConfigError) as exc:
        log_event(logger, logging.ERROR, "config_invalid", error=str(exc))
        return EXIT_CONFIG
    except StoreError as exc:
        log_event(logger, logging.ERROR, "store_unavailable", error=str(exc))
        return EXIT_STORE
    except ValueError as exc:  # an invalid setting of the container-runtime source
        log_event(logger, logging.ERROR, "settings_invalid", error=str(exc))
        return EXIT_SETTINGS
    stop = threading.Event()
    server = api.make_server(runtime.ctx, "0.0.0.0", settings.port)
    threads = [threading.Thread(target=runtime.loop.run, args=(stop,), name="build", daemon=True)]
    if runtime.runtime_source.status().state != "off":
        threads.append(threading.Thread(target=runtime.poller.run, args=(stop, settings.change_poll_s), name="changes", daemon=True))

    def shutdown(_signum=None, _frame=None):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log_event(logger, logging.INFO, "evidence_started", **runtime.ctx.settings, runtime_source=runtime.runtime_source.status().state)
    for thread in threads:
        thread.start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        runtime.collector.close()
        runtime.store.close()
        log_event(logger, logging.INFO, "evidence_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
