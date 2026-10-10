"""Service registry, dependency mutation, traversal, and incident enrichment APIs."""

import sqlite3
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from opsmind_api.auth import Principal
from opsmind_api.main import create_app
from opsmind_storage.models import Base, IncidentEvidenceRow, ServiceDependencyRow
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ServiceApiContext = tuple[TestClient, sessionmaker[Session], UUID, dict[str, str]]


class _TopologyAuthentication:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    def authenticate(self, _: object) -> Principal:
        return Principal(
            tenant_id=self.tenant_id,
            subject_id=UUID("f02f8cab-4ec2-44c7-8ec5-7b8dbf725843"),
            permissions=frozenset({"*"}),
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
        session_factory=sessions, authentication_backend=_TopologyAuthentication(tenant_id)
    )
    with TestClient(app) as client:
        yield client, sessions, tenant_id
    engine.dispose()


@pytest.fixture
def services(
    api_context: tuple[TestClient, sessionmaker[Session], UUID],
) -> Iterator[ServiceApiContext]:
    client, sessions, tenant_id = api_context
    created: dict[str, str] = {}
    for name in ("api", "order", "payment", "postgres"):
        response = client.post(
            "/services",
            json={"name": name, "environment": "production", "metadata": {"tier": "demo"}},
        )
        assert response.status_code == 201, response.text
        created[name] = response.json()["id"]
    yield client, sessions, tenant_id, created


def _add_dependency(client: TestClient, source: str, target: str) -> None:
    response = client.post(
        f"/services/{source}/dependencies",
        json={"target_service_id": target, "dependency_type": "http"},
    )
    assert response.status_code == 201, response.text


def test_service_list_and_detail_are_tenant_scoped(services: ServiceApiContext) -> None:
    client, _, _, created = services
    listing = client.get("/services?environment=production&limit=2")
    assert listing.status_code == 200
    assert listing.json()["total"] == 4
    assert len(listing.json()["items"]) == 2

    detail = client.get(f"/services/{created['api']}")
    assert detail.status_code == 200
    assert detail.json()["metadata"] == {"tier": "demo"}
    assert client.get(f"/services/{uuid4()}").status_code == 404


def test_dependency_graph_cycle_rejection_removal_and_queries(services: ServiceApiContext) -> None:
    client, _, _, ids = services
    first = client.post(
        f"/services/{ids['api']}/dependencies", json={"target_service_id": ids["order"]}
    )
    second = client.post(
        f"/services/{ids['order']}/dependencies", json={"target_service_id": ids["payment"]}
    )
    third = client.post(
        f"/services/{ids['payment']}/dependencies", json={"target_service_id": ids["postgres"]}
    )
    assert first.status_code == second.status_code == third.status_code == 201
    duplicate = client.post(
        f"/services/{ids['api']}/dependencies", json={"target_service_id": ids["order"]}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_dependency"

    cycle = client.post(
        f"/services/{ids['postgres']}/dependencies", json={"target_service_id": ids["api"]}
    )
    assert cycle.status_code == 409
    assert cycle.json()["error"]["code"] == "dependency_cycle"

    dependencies = client.get(f"/services/{ids['payment']}/dependencies")
    assert dependencies.status_code == 200
    assert [node["name"] for node in dependencies.json()["upstream_services"]] == [
        "order",
        "api",
    ]
    assert [node["name"] for node in dependencies.json()["downstream_services"]] == ["postgres"]
    assert [node["name"] for node in dependencies.json()["critical_path"]] == [
        "payment",
        "postgres",
    ]

    blast = client.get(f"/services/{ids['payment']}/blast-radius")
    assert blast.status_code == 200
    assert blast.json()["likely_blast_radius_count"] == 3
    assert {item["name"] for item in blast.json()["likely_blast_radius"]} == {
        "payment",
        "order",
        "api",
    }

    edge_id = second.json()["id"]
    removed = client.delete(f"/services/{ids['order']}/dependencies/{edge_id}")
    assert removed.status_code == 204
    assert client.delete(f"/services/{ids['order']}/dependencies/{edge_id}").status_code == 404
    readded = client.post(
        f"/services/{ids['order']}/dependencies", json={"target_service_id": ids["payment"]}
    )
    assert readded.status_code == 201


def test_missing_and_cross_environment_dependencies_are_rejected(
    services: ServiceApiContext,
) -> None:
    client, _, _, ids = services
    missing = client.post(
        f"/services/{ids['api']}/dependencies", json={"target_service_id": str(uuid4())}
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "service_not_found"

    staging = client.post("/services", json={"name": "worker", "environment": "staging"})
    assert staging.status_code == 201
    mismatch = client.post(
        f"/services/{ids['api']}/dependencies",
        json={"target_service_id": staging.json()["id"]},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "service_environment_mismatch"


def test_incident_creation_stores_topology_evidence(services: ServiceApiContext) -> None:
    client, sessions, tenant_id, ids = services
    _add_dependency(client, ids["api"], ids["order"])
    _add_dependency(client, ids["order"], ids["payment"])
    incident = client.post(
        "/incidents",
        headers={"Idempotency-Key": "topology-incident"},
        json={"title": "Payment degraded", "service_id": ids["payment"]},
    )
    assert incident.status_code == 201, incident.text

    with sessions() as session:
        evidence = session.scalar(
            select(IncidentEvidenceRow).where(
                IncidentEvidenceRow.tenant_id == tenant_id,
                IncidentEvidenceRow.incident_id == UUID(incident.json()["id"]),
                IncidentEvidenceRow.evidence_type == "service_topology",
            )
        )
        assert evidence is not None
        assert evidence.source == "opsmind-service-topology"
        assert evidence.content["affected_service"]["name"] == "payment"
        assert {item["name"] for item in evidence.content["upstream_impact"]} == {"api", "order"}
        assert evidence.content["likely_blast_radius_count"] == 3
        assert session.scalar(select(ServiceDependencyRow.id)) is not None


def test_dependency_change_refreshes_open_incident_topology(services: ServiceApiContext) -> None:
    client, sessions, tenant_id, ids = services
    incident = client.post(
        "/incidents",
        headers={"Idempotency-Key": "topology-refresh"},
        json={"title": "Payment degraded", "service_id": ids["payment"]},
    )
    assert incident.status_code == 201, incident.text
    incident_id = UUID(incident.json()["id"])

    changed = client.post(
        f"/services/{ids['order']}/dependencies",
        json={"target_service_id": ids["payment"]},
    )
    assert changed.status_code == 201, changed.text

    with sessions() as session:
        snapshots = session.scalars(
            select(IncidentEvidenceRow)
            .where(
                IncidentEvidenceRow.tenant_id == tenant_id,
                IncidentEvidenceRow.incident_id == incident_id,
                IncidentEvidenceRow.evidence_type == "service_topology",
            )
            .order_by(IncidentEvidenceRow.collected_at)
        ).all()
        assert len(snapshots) == 2
        assert snapshots[0].content["likely_blast_radius_count"] == 1
        assert snapshots[1].content["likely_blast_radius_count"] == 2
