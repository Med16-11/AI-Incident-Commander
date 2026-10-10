"""Resolution snapshot capture tests."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from opsmind_api.historical import snapshot_resolved_incident
from opsmind_storage.models import (
    Base,
    HistoricalIncidentRow,
    IncidentEvidenceRow,
    IncidentRow,
    InvestigationRunRow,
    ServiceRow,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


def test_resolved_incident_snapshot_stores_searchable_context(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    tenant_id = uuid4()
    service_id = uuid4()
    incident_id = uuid4()
    now = datetime(2026, 10, 6, 10, tzinfo=UTC)

    with sessions.begin() as session:
        session.add(
            ServiceRow(
                id=service_id,
                tenant_id=tenant_id,
                name="payment",
                environment="production",
            )
        )
        incident = IncidentRow(
            id=incident_id,
            tenant_id=tenant_id,
            service_id=service_id,
            title="Payment requests timing out",
            description="Order completion degraded",
            state="RESOLVED",
            severity="SEV2",
            detected_at=datetime(2026, 10, 6, 9, 55, tzinfo=UTC),
            resolved_at=now,
        )
        session.add(incident)
        session.flush()
        session.add(
            IncidentEvidenceRow(
                tenant_id=tenant_id,
                incident_id=incident_id,
                source="prometheus",
                service="payment",
                evidence_type="metric_anomaly",
                external_ref="prometheus://payment-latency",
                normalized_summary="Payment p95 latency exceeded threshold",
                confidence=0.97,
                observed_at=datetime(2026, 10, 6, 9, 56, tzinfo=UTC),
            )
        )
        session.add(
            InvestigationRunRow(
                tenant_id=tenant_id,
                incident_id=incident_id,
                correlation_id="corr-1",
                state="REMEDIATION_PROPOSED",
                status="COMPLETED",
                state_data={
                    "analysis_summary": {"text": "Payment gateway timed out."},
                    "symptoms": ["Payment requests timed out."],
                    "root_cause": {
                        "text": "Provider gateway connection pool was exhausted.",
                        "evidence_status": "supported",
                    },
                    "affected_services": ["payment", "orders"],
                    "remediation_recommendations": [
                        {"text": "Route payment requests through standby gateway."}
                    ],
                },
                started_at=datetime(2026, 10, 6, 9, 57, tzinfo=UTC),
            )
        )
        stored = snapshot_resolved_incident(session, incident)
        assert stored.resolution_seconds == 300

    try:
        with sessions() as session:
            stored_row = session.scalar(
                select(HistoricalIncidentRow).where(
                    HistoricalIncidentRow.tenant_id == tenant_id,
                    HistoricalIncidentRow.incident_id == incident_id,
                )
            )
            assert stored_row is not None
            assert stored_row.summary == "Payment gateway timed out."
            assert stored_row.root_cause == "Provider gateway connection pool was exhausted."
            assert stored_row.affected_services == ["payment", "orders"]
            assert stored_row.evidence[0]["source"] == "prometheus"
            assert stored_row.remediation[0]["summary"].startswith("Route payment")
            assert "payment" in stored_row.search_text.casefold()
            assert "sev2" in stored_row.tags
    finally:
        engine.dispose()
