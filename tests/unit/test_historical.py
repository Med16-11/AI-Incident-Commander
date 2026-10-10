"""Historical incident retrieval relevance and tenant isolation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from opsmind_incident_engine.historical import (
    PostgreSQLFullTextHistoricalRetriever,
    VectorHistoricalIncidentRetriever,
    VectorSearchHit,
)
from opsmind_storage.models import Base, HistoricalIncidentRow, IncidentRow
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def _database(tmp_path: Path) -> tuple[sessionmaker[Session], Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'historical.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session), engine


def _add_history(
    session: Session, tenant_id: UUID, *, summary: str, search_text: str
) -> HistoricalIncidentRow:
    incident = IncidentRow(
        tenant_id=tenant_id,
        title=summary,
        state="RESOLVED",
        severity="SEV2",
        detected_at=datetime(2026, 10, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 10, 1, 0, 5, tzinfo=UTC),
    )
    session.add(incident)
    session.flush()
    row = HistoricalIncidentRow(
        tenant_id=tenant_id,
        incident_id=incident.id,
        summary=summary,
        symptoms=["elevated payment errors"],
        root_cause="payment provider timeout",
        affected_services=["payment", "orders"],
        evidence=[],
        remediation=[{"kind": "action", "summary": "switched to standby gateway"}],
        resolved_at=datetime(2026, 10, 1, 0, 5, tzinfo=UTC),
        resolution_seconds=300,
        tags=["payment", "timeout"],
        search_text=search_text,
    )
    session.add(row)
    session.flush()
    return row


def test_full_text_fallback_ranks_relevant_history_and_omits_irrelevant(
    tmp_path: Path,
) -> None:
    sessions, engine = _database(tmp_path)
    tenant_id = uuid4()
    with sessions.begin() as session:
        relevant = _add_history(
            session,
            tenant_id,
            summary="Payment provider timeout caused order errors",
            search_text=(
                "payment provider timeout elevated errors orders gateway latency "
                "standby gateway payment timeout"
            ),
        )
        _add_history(
            session,
            tenant_id,
            summary="Inventory stock synchronization delay",
            search_text="inventory stock synchronization delayed warehouse reconciliation",
        )

    try:
        with sessions() as session:
            results = PostgreSQLFullTextHistoricalRetriever().search(
                session,
                tenant_id,
                "payment provider timeout order errors",
                limit=10,
            )
    finally:
        engine.dispose()

    assert [item.incident_id for item in results] == [relevant.incident_id]
    assert results[0].similarity > 0
    assert results[0].evidence == []
    assert results[0].remediation == ["switched to standby gateway"]


def test_full_text_retriever_is_tenant_scoped(tmp_path: Path) -> None:
    sessions, engine = _database(tmp_path)
    matching_tenant = uuid4()
    other_tenant = uuid4()
    with sessions.begin() as session:
        matching = _add_history(
            session,
            matching_tenant,
            summary="Matching payment timeout",
            search_text="payment timeout gateway",
        )
        _add_history(
            session,
            other_tenant,
            summary="Other tenant payment timeout",
            search_text="payment timeout gateway",
        )
    try:
        with sessions() as session:
            results = PostgreSQLFullTextHistoricalRetriever().search(
                session, matching_tenant, "payment timeout", limit=10
            )
    finally:
        engine.dispose()
    assert [item.incident_id for item in results] == [matching.incident_id]


class _VectorBackend:
    def __init__(self, hit_id: UUID) -> None:
        self.hit_id = hit_id

    def search(self, _tenant_id: UUID, _query: str, *, limit: int) -> list[VectorSearchHit]:
        assert limit == 5
        return [VectorSearchHit(incident_id=self.hit_id, similarity=0.93)]


def test_vector_adapter_hydrates_only_tenant_visible_snapshots(tmp_path: Path) -> None:
    sessions, engine = _database(tmp_path)
    tenant_id = uuid4()
    other_tenant = uuid4()
    with sessions.begin() as session:
        hidden = _add_history(
            session,
            other_tenant,
            summary="Cross-tenant history",
            search_text="payment timeout gateway",
        )
    try:
        with sessions() as session:
            results = VectorHistoricalIncidentRetriever(_VectorBackend(hidden.incident_id)).search(
                session, tenant_id, "payment timeout", limit=5
            )
    finally:
        engine.dispose()
    assert results == []
