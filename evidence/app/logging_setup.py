"""JSON log lines in the format of the other services: ts, level, service, version, request_id (null), event, plus
the fields of the event. Free text in field values is redacted and cut (log lines go to Loki)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .model import fmt_ts
from .redact import redact

SERVICE_NAME = "evidence"
RESERVED = {"ts", "level", "service", "version", "request_id", "event"}
MAX_FIELD_CHARS = 500


def _clean(value):
    if isinstance(value, str):
        return redact(value)[:MAX_FIELD_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    return redact(str(value))[:MAX_FIELD_CHARS]


class JsonFormatter(logging.Formatter):
    def __init__(self, version: str):
        super().__init__()
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": fmt_ts(datetime.fromtimestamp(record.created, tz=timezone.utc)),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "version": self.version,
            "request_id": None,
            "event": record.getMessage(),
        }
        for key, value in getattr(record, "fields", {}).items():
            line[key if key not in RESERVED else f"field_{key}"] = _clean(value)
        return json.dumps(line, ensure_ascii=False)


def setup_logging(level: str, version: str, stream=None) -> logging.Logger:
    logger = logging.getLogger(SERVICE_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(version))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: int, event: str, /, **fields) -> None:
    logger.log(level, event, extra={"fields": fields})
