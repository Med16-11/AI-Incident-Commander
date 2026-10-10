"""FastAPI dependencies for database, authentication, and authorization."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from opsmind_api.auth import (
    AuthenticationBackend,
    AuthenticationError,
    AuthorizationError,
    Principal,
)
from opsmind_api.errors import ApiError


def get_session(request: Request) -> Iterator[Session]:
    """Yield a request-scoped database session from the injected factory."""
    with request.app.state.session_factory() as session:
        yield session


def get_principal(request: Request) -> Principal:
    """Authenticate a request using the configured backend, failing closed."""
    backend: AuthenticationBackend = request.app.state.authentication_backend
    try:
        return backend.authenticate(request)
    except AuthenticationError as exc:
        raise ApiError(401, "unauthenticated", str(exc)) from exc


def authorize(
    request: Request, principal: Principal, action: str, resource: object | None = None
) -> None:
    """Apply the configured authorization policy to an operation."""
    try:
        request.app.state.authorization_policy.authorize(principal, action, resource)
    except AuthorizationError as exc:
        raise ApiError(403, "forbidden", str(exc)) from exc
