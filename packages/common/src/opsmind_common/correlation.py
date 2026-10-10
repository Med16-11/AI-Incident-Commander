"""HTTP correlation ID middleware and event correlation helpers."""

import re
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "X-Correlation-ID"
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def new_correlation_id() -> str:
    """Return a new opaque correlation identifier."""
    return str(uuid4())


class CorrelationIdMiddleware:
    """Bind correlation IDs at the ASGI boundary, including generated error responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        structlog.contextvars.clear_contextvars()
        correlation_header = CORRELATION_HEADER.lower().encode()
        values = [
            value for name, value in scope.get("headers", []) if name.lower() == correlation_header
        ]
        try:
            candidate = values[0].decode("ascii") if len(values) == 1 else ""
        except UnicodeDecodeError:
            candidate = ""
        correlation_id = (
            candidate if _VALID_CORRELATION_ID.fullmatch(candidate) else new_correlation_id()
        )
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != CORRELATION_HEADER.lower().encode()
                ]
                response_headers.append(
                    (CORRELATION_HEADER.lower().encode(), correlation_id.encode())
                )
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        finally:
            structlog.contextvars.clear_contextvars()
