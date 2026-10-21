"""JSON logs, one line per event, to stdout. Same field names as the M1 services:
ts, level, service, version, request_id (always null here), event, then the extra fields of the event."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .model import iso_z

SERVICE_NAME = "detector"
LOGGER_NAME = "detector"
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str):
        super().__init__()
        self._service = service
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": iso_z(datetime.fromtimestamp(record.created, timezone.utc)),
            "level": record.levelname,
            "service": self._service,
            "version": self._version,
            "request_id": None,
            "event": record.getMessage(),
        }
        data.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            data["error"] = f"{record.exc_info[0].__name__}: {record.exc_info[1]}"
        return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


def valid_level(name: str) -> bool:
    return name.upper() in _LEVELS


def configure(level: str, version: str, stream=None) -> logging.Logger:
    """The detector's logger. Calling it again replaces the handler (no duplicate lines)."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(SERVICE_NAME, version))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """log_event(logger, logging.INFO, "incident_opened", incident_id="INC-0001")"""
    logger.log(level, event, extra={"fields": fields})
