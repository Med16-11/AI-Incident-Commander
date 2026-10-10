"""Unit tests for API validation and authorization policy behavior."""

from uuid import uuid4

import pytest
from opsmind_api.auth import (
    AuthenticationError,
    AuthorizationError,
    DenyAllAuthentication,
    PermissionAuthorization,
    Principal,
)
from opsmind_api.schemas import IncidentCreate, IncidentPatch
from pydantic import ValidationError


def test_incident_create_rejects_blank_title_and_invalid_severity() -> None:
    with pytest.raises(ValidationError):
        IncidentCreate(title="   ")
    with pytest.raises(ValidationError):
        IncidentCreate(title="Database errors", severity="SEV0")


def test_incident_patch_requires_a_mutable_field() -> None:
    with pytest.raises(ValidationError):
        IncidentPatch(expected_version=1)


def test_permission_authorization_requires_explicit_grant() -> None:
    actor = Principal(uuid4(), uuid4(), frozenset({"incidents:read"}))
    policy = PermissionAuthorization()
    policy.authorize(actor, "incidents:read")
    with pytest.raises(AuthorizationError, match="incidents:update"):
        policy.authorize(actor, "incidents:update")


def test_default_authentication_backend_fails_closed() -> None:
    with pytest.raises(AuthenticationError, match="Authentication is not configured"):
        DenyAllAuthentication().authenticate(None)  # type: ignore[arg-type]
