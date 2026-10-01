"""JSON logging: one line per event, to stdout.

Always present: ts, level, service, version, request_id, event
Present when relevant: method, path, status, duration_ms, error (+ any extra fields)
"""
import json
import logging
import sys
from datetime import datetime, timezone

from common.context import request_id_var
from common.settings import Settings

# Attributes every LogRecord has; anything else on a record came from `extra=` and is logged.
# "color_message" is added by uvicorn for its console colours and is just noise here.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", "color_message",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, version: str):
        super().__init__()
        self.service = service
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        data = {
            "ts": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "version": self.version,
            "request_id": request_id_var.get(),
            "event": getattr(record, "event", None) or "log",
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key != "event" and value is not None:
                data[key] = value
        if not hasattr(record, "event"):
            # Record from a library (uvicorn, psycopg_pool, ...): keep its text and origin.
            data["message"] = record.getMessage()
            data["logger"] = record.name
        if record.exc_info and "error" not in data:
            exc = record.exc_info[1]
            data["error"] = f"{type(exc).__name__}: {exc}"
        return json.dumps(data, default=str)  # json.dumps escapes newlines -> always one line


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.service_name, settings.app_version))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # uvicorn installs its own plain-text handlers; route its server logs through ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # We write our own access-log line per request (middleware.py); silence uvicorn's duplicate.
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    # httpx logs every request at INFO; our own access/dependency logs already cover that.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """Write one structured log line: log_event(logger, logging.INFO, "stock_reserved", sku=...)."""
    logger.log(level, event, extra={"event": event, **fields})
