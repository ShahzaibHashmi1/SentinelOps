"""Per-request context shared by logging, metrics and outbound HTTP calls.

A ContextVar is used so that every log line written while handling a request
automatically carries that request's id, without passing it through every function.
"""
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
