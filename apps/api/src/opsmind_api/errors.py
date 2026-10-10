"""Stable structured error responses for the public API."""

from typing import Any

import structlog
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    """Expected domain or access error suitable for an HTTP response."""

    def __init__(
        self, status_code: int, code: str, message: str, details: Any | None = None
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


def _correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unknown"))


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "correlation_id": _correlation_id(request),
                "details": exc.details,
            }
        },
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = [
        {key: error[key] for key in ("loc", "msg", "type") if key in error}
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "correlation_id": _correlation_id(request),
                "details": jsonable_encoder(safe_errors),
            }
        },
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    messages = {404: "Resource not found", 405: "Method not allowed"}
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": "http_error",
                "message": messages.get(exc.status_code, str(exc.detail)),
                "correlation_id": _correlation_id(request),
                "details": None,
            }
        },
        headers=exc.headers,
    )


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return safe structured failures without exposing internal exception details."""
    # Avoid serializing exception text/tracebacks that may contain SQL parameters,
    # request values, or credential-bearing connection URLs.
    structlog.get_logger().error("unhandled_api_error", error_type=type(exc).__name__)
    if isinstance(exc, SQLAlchemyError):
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "storage_unavailable",
                    "message": "Incident data is temporarily unavailable",
                    "correlation_id": _correlation_id(request),
                    "details": None,
                }
            },
        )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "An internal error occurred",
                "correlation_id": _correlation_id(request),
                "details": None,
            }
        },
    )
