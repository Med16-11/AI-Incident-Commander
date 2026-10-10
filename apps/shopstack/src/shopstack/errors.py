"""ShopStack structured HTTP errors and downstream response translation."""

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def _correlation_id(request: Request) -> str | None:
    return (
        getattr(request.state, "correlation_id", None)
        or request.headers.get("x-correlation-id")
        or request.headers.get("x-request-id")
    )


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "error": {
                "code": "http_error",
                "message": str(exc.detail),
                "correlation_id": _correlation_id(request),
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
