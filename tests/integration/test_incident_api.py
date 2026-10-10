"""HTTP integration tests for tenant scoping, mutations, and audit behavior."""

import sqlite3
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse
from opsmind_api.auth import DenyAllAuthentication, Principal
from opsmind_api.main import create_app
from opsmind_storage.models import AuditLogRow, Base, IncidentEventRow, IncidentRow
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


class FakeAuthenticationBackend:
    def __init__(self, tenant_id: UUID, permissions: frozenset[str] = frozenset({"*"})) -> None:
        self.tenant_id = tenant_id
        self.permissions = permissions

    def authenticate(self, _: object) -> Principal:
        return Principal(
            tenant_id=self.tenant_id,
            subject_id=UUID("f02f8cab-4ec2-44c7-8ec5-7b8dbf725843"),
            permissions=self.permissions,
        )


@pytest.fixture
def api_context() -> Iterator[tuple[TestClient, sessionmaker[Session], UUID]]:
    tenant_id = uuid4()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: sqlite3.Connection, _: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    app = create_app(
        session_factory=sessions, authentication_backend=FakeAuthenticationBackend(tenant_id)
    )
    with TestClient(app) as client:
        yield client, sessions, tenant_id
    engine.dispose()


def _create(client: TestClient, key: str = "create-1", **payload: object) -> HttpxResponse:
    return client.post(
        "/incidents",
        headers={"Idempotency-Key": key, "X-Correlation-ID": "test-correlation"},
        json={"title": "Checkout latency", **payload},
    )


def test_create_is_idempotent_and_audited(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, tenant_id = api_context
    first = _create(client)
    replay = _create(client)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["id"] == replay.json()["id"]
    assert first.headers["x-correlation-id"] == "test-correlation"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(IncidentRow)) == 1
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 1
        audit = session.scalar(select(AuditLogRow))
        assert audit is not None
        assert audit.tenant_id == tenant_id
        assert audit.correlation_id == "test-correlation"


def test_idempotency_key_cannot_be_reused_for_different_create(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, _ = api_context
    assert _create(client).status_code == 201
    response = client.post(
        "/incidents",
        headers={"Idempotency-Key": "create-1"},
        json={"title": "Different incident"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(IncidentRow)) == 1


def test_idempotency_replay_requires_read_permission(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, tenant_id = api_context
    app = client.app
    assert isinstance(app, FastAPI)
    app.state.authentication_backend = FakeAuthenticationBackend(
        tenant_id, permissions=frozenset({"incidents:create"})
    )
    assert _create(client, "create-only").status_code == 201
    assert _create(client, "create-only").status_code == 403
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(IncidentRow)) == 1
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 1


def test_patch_event_deduplication_and_timeline_are_audited(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, _ = api_context
    incident_id = _create(client).json()["id"]
    patch = client.patch(
        f"/incidents/{incident_id}",
        json={"expected_version": 1, "state": "TRIAGING", "severity": "SEV2"},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["version"] == 2

    event_body = {
        "event_type": "InvestigationStarted",
        "source": "integration-test",
        "payload": {"investigator": "test"},
    }
    headers = {"Idempotency-Key": "event-1"}
    first_event = client.post(f"/incidents/{incident_id}/events", headers=headers, json=event_body)
    second_event = client.post(f"/incidents/{incident_id}/events", headers=headers, json=event_body)
    assert first_event.status_code == second_event.status_code == 201
    assert first_event.json()["id"] == second_event.json()["id"]
    conflict = client.post(
        f"/incidents/{incident_id}/events",
        headers=headers,
        json={**event_body, "payload": {"investigator": "changed"}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    timeline = client.get(f"/incidents/{incident_id}/timeline?limit=10")
    assert timeline.status_code == 200
    assert timeline.json()["total"] == 3
    for collection in ("evidence", "hypotheses", "remediations"):
        listed = client.get(f"/incidents/{incident_id}/{collection}?limit=10")
        assert listed.status_code == 200
        assert listed.json()["total"] == 0
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 3
        assert session.scalar(select(func.count()).select_from(IncidentEventRow)) == 1


def test_invalid_transition_and_stale_version_return_structured_errors(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, _ = api_context
    incident_id = _create(client).json()["id"]
    invalid = client.patch(
        f"/incidents/{incident_id}", json={"expected_version": 1, "state": "RESOLVED"}
    )
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "invalid_transition"
    assert invalid.json()["error"]["correlation_id"]

    stale = client.patch(
        f"/incidents/{incident_id}", json={"expected_version": 99, "title": "stale"}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_incident"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 1


def test_filtering_pagination_and_tenant_isolation(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, _, tenant_id = api_context
    first_id = _create(client, "first", severity="SEV1").json()["id"]
    _create(client, "second", severity="SEV2")
    filtered = client.get("/incidents?severity=SEV1&limit=1&offset=0")
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert len(filtered.json()["items"]) == 1

    app = client.app
    assert isinstance(app, FastAPI)
    app.state.authentication_backend = FakeAuthenticationBackend(uuid4())
    hidden = client.get(f"/incidents/{first_id}")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "incident_not_found"
    assert tenant_id != app.state.authentication_backend.tenant_id


def test_authentication_fails_closed_and_validation_is_structured(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, _ = api_context
    app = client.app
    assert isinstance(app, FastAPI)
    app.state.authentication_backend = DenyAllAuthentication()
    unauthenticated = client.get("/incidents")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "unauthenticated"

    app.state.authentication_backend = FakeAuthenticationBackend(uuid4())
    invalid = client.post(
        "/incidents",
        headers={"Idempotency-Key": "bad"},
        json={"title": "   ", "severity": "secret-input-value"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert invalid.headers.get("x-correlation-id")
    assert "secret-input-value" not in invalid.text
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(IncidentRow)) == 0


def test_authorization_policy_rejects_missing_permission(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, tenant_id = api_context
    app = client.app
    assert isinstance(app, FastAPI)
    app.state.authentication_backend = FakeAuthenticationBackend(
        tenant_id, permissions=frozenset({"incidents:read"})
    )
    response = _create(client)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(IncidentRow)) == 0


def test_openapi_documents_incident_management_routes(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, _, _ = api_context
    schema = client.get("/openapi.json").json()
    expected = {
        "/incidents",
        "/incidents/{incident_id}",
        "/incidents/{incident_id}/events",
        "/incidents/{incident_id}/close",
        "/incidents/{incident_id}/timeline",
        "/incidents/{incident_id}/evidence",
        "/incidents/{incident_id}/hypotheses",
        "/incidents/{incident_id}/remediations",
    }
    assert expected <= schema["paths"].keys()


def test_close_requires_valid_lifecycle_transition(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, _ = api_context
    incident_id = _create(client).json()["id"]
    response = client.post(f"/incidents/{incident_id}/close", json={"expected_version": 1})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_transition"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 1


def test_close_resolved_incident_creates_audit_and_timeline(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> None:
    client, sessions, _ = api_context
    incident_id = _create(client).json()["id"]
    for expected_version, state in enumerate(
        ("TRIAGING", "INVESTIGATING", "IDENTIFIED", "MITIGATING", "RESOLVED"), start=1
    ):
        transition = client.patch(
            f"/incidents/{incident_id}",
            json={"expected_version": expected_version, "state": state},
        )
        assert transition.status_code == 200, transition.text
    response = client.post(f"/incidents/{incident_id}/close", json={"expected_version": 6})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "CLOSED"
    assert response.json()["version"] == 7
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuditLogRow)) == 7
