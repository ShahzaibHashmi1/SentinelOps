"""Error helpers shared by all services."""
from fastapi import Request
from fastapi.responses import JSONResponse

from common.context import request_id_var


def describe_exception(exc: BaseException) -> str:
    """'ExceptionType: message' (or just the type when the message is empty)."""
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class AppError(Exception):
    """A deliberate error response: HTTP status + machine-readable error code.

    Body returned to the client: {"error": <code>, "request_id": ..., **extra}
    `detail` is only written to the log (field "error"), never sent to the client.
    """

    def __init__(self, status_code: int, error: str, detail: str | None = None, **extra):
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.detail = detail
        self.extra = extra


def error_body(error: str, **extra) -> dict:
    return {"error": error, "request_id": request_id_var.get(), **extra}


def remember_error(request: Request, text: str) -> None:
    """Store error text so the access-log middleware can add it to the request's log line."""
    request.scope["sentinelops_error"] = text


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    remember_error(request, f"{exc.error}: {exc.detail}" if exc.detail else exc.error)
    return JSONResponse(error_body(exc.error, **exc.extra), status_code=exc.status_code)
