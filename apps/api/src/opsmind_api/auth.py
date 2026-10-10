"""Authentication and authorization ports for the HTTP API."""

import hmac
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fastapi import Request


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated actor and tenant claims supplied by an identity provider."""

    tenant_id: UUID
    subject_id: UUID
    permissions: frozenset[str]


class AuthenticationError(Exception):
    """Raised when credentials are absent or invalid."""


class AuthorizationError(Exception):
    """Raised when an actor is not allowed to perform an operation."""


class AuthenticationBackend(Protocol):
    """Integration seam for OIDC, JWT, mTLS, or another identity provider."""

    def authenticate(self, request: Request) -> Principal: ...


class AuthorizationPolicy(Protocol):
    """Policy seam for permission and resource authorization."""

    def authorize(self, principal: Principal, action: str, resource: object | None = None) -> None:
        """Raise `AuthorizationError` when the principal cannot perform action."""


class DenyAllAuthentication:
    """Safe default until a deployment injects an authentication backend."""

    def authenticate(self, request: Request) -> Principal:
        del request
        raise AuthenticationError("Authentication is not configured")


class LocalDemoAuthentication:
    """Single-tenant bearer authentication for an explicitly configured local demo."""

    _tenant_id = UUID("a0000000-0000-4000-8000-000000000001")
    _subject_id = UUID("a0000000-0000-4000-8000-000000000002")
    _permissions = frozenset(
        {
            "incidents:create",
            "incidents:read",
            "incidents:update",
            "incidents:events:write",
            "incidents:close",
            "services:read",
            "services:write",
            "services:dependencies:write",
            "remediations:read",
            "remediations:propose",
            "remediations:approve",
            "remediations:execute",
        }
    )

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise ValueError("local demo token must not be empty")
        self._token = token

    def authenticate(self, request: Request) -> Principal:
        """Authenticate the configured local token and grant local demo permissions."""
        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(
            supplied.encode("utf-8"), self._token.encode("utf-8")
        ):
            raise AuthenticationError("Invalid local demo credential")
        return Principal(
            tenant_id=self._tenant_id,
            subject_id=self._subject_id,
            permissions=self._permissions,
        )


class PermissionAuthorization:
    """Simple permission policy that is replaceable by a richer RBAC/ABAC policy."""

    def authorize(self, principal: Principal, action: str, resource: object | None = None) -> None:
        del resource
        if action not in principal.permissions and "*" not in principal.permissions:
            raise AuthorizationError(f"Permission required: {action}")
