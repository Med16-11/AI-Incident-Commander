"""Synthetic incident scenarios for deterministic evidence correlation."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from opsmind_common.config import Settings
from opsmind_incident_engine.correlation import (
    CorrelationWindow,
    DatabaseEvidenceSource,
    EvidenceCandidate,
    IncidentContext,
    IncidentCorrelationEngine,
    redact_evidence_summary,
)
from opsmind_incident_engine.main import create_app
from opsmind_incident_engine.sources import LokiEvidenceSource, PrometheusEvidenceSource
from opsmind_storage.models import (
    Base,
    DeploymentRow,
    IncidentEventRow,
    IncidentEvidenceRow,
    IncidentRow,
    IncidentTimelineRow,
    LogRecordRow,
    MetricSnapshotRow,
    ServiceDependencyRow,
    ServiceRow,
)
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker


class SyntheticSource:
    name = "synthetic"

    def __init__(self, events: list[EvidenceCandidate]) -> None:
        self.events = events

    def collect(
        self, _context: IncidentContext, _start: datetime, _end: datetime
    ) -> list[EvidenceCandidate]:
        return self.events


class FailingSource:
    name = "unavailable-test-source"

    def collect(
        self, _context: IncidentContext, _start: datetime, _end: datetime
    ) -> list[EvidenceCandidate]:
        raise TimeoutError("source contains private connection details")


def _candidate(
    at: datetime, kind: str, ref: str, service: str = "payment-service"
) -> EvidenceCandidate:
    return EvidenceCandidate(
        source="synthetic-monitor",
        timestamp=at,
        service=service,
        evidence_type=kind,
        raw_reference=ref,
        normalized_summary=f"Synthetic {kind} evidence from {service}",
        confidence=0.9,
        attributes={"scenario": "payment-degradation"},
    )


def _context(
    detected_at: datetime,
    incident_id: UUID | None = None,
    tenant_id: UUID | None = None,
) -> IncidentContext:
    return IncidentContext(
        incident_id=incident_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        service="payment-service",
        detected_at=detected_at,
        dependencies=("order-service",),
    )


def test_synthetic_payment_incident_collects_correlated_evidence_chronologically() -> None:
    detected_at = datetime(2026, 10, 3, 14, 5, tzinfo=UTC)
    events = list(
        reversed(
            [
                _candidate(detected_at - timedelta(minutes=2), "deployment", "deployment://42"),
                _candidate(
                    detected_at - timedelta(minutes=1), "metric_anomaly", "prometheus://latency"
                ),
                _candidate(
                    detected_at - timedelta(minutes=1), "metric_anomaly", "prometheus://errors"
                ),
                _candidate(
                    detected_at - timedelta(seconds=30),
                    "log",
                    "loki://order-errors",
                    "order-service",
                ),
                _candidate(
                    detected_at - timedelta(seconds=15),
                    "infrastructure_event",
                    "prometheus://target-unavailable",
                ),
                _candidate(
                    detected_at + timedelta(minutes=1), "pod_restart", "prometheus://restart"
                ),
                _candidate(detected_at - timedelta(minutes=20), "log", "loki://outside-window"),
            ]
        )
    )
    engine = IncidentCorrelationEngine(
        [SyntheticSource(events)], CorrelationWindow(before_seconds=600, after_seconds=120)
    )

    result = engine.collect(_context(detected_at))

    assert [item.raw_reference for item in result.evidence] == [
        "deployment://42",
        "prometheus://errors",
        "prometheus://latency",
        "loki://order-errors",
        "prometheus://target-unavailable",
        "prometheus://restart",
    ]
    assert all(item.confidence == 0.9 and item.raw_reference for item in result.evidence)
    assert result.source_failures == ()


def test_source_failure_is_reported_without_discarding_successful_evidence() -> None:
    detected_at = datetime(2026, 10, 3, 14, 5, tzinfo=UTC)
    engine = IncidentCorrelationEngine(
        [
            SyntheticSource([_candidate(detected_at, "configuration_change", "config://change")]),
            FailingSource(),
        ]
    )

    result = engine.collect(_context(detected_at))

    assert len(result.evidence) == 1
    assert result.source_failures == ("unavailable-test-source",)


def test_database_evidence_and_timeline_are_complete_and_idempotent(tmp_path: Path) -> None:
    detected_at = datetime(2026, 10, 3, 14, 5, tzinfo=UTC)
    tenant_id, incident_id, payment_id, order_id = (uuid4() for _ in range(4))
    engine_db = create_engine(f"sqlite:///{tmp_path / 'correlation.db'}")
    Base.metadata.create_all(engine_db)
    sessions = sessionmaker(bind=engine_db, expire_on_commit=False, class_=Session)
    context = _context(detected_at, incident_id, tenant_id).model_copy(
        update={"environment": "prod"}
    )
    with Session(engine_db, expire_on_commit=False) as session:
        payment = ServiceRow(
            id=payment_id, tenant_id=tenant_id, name="payment-service", environment="prod"
        )
        order = ServiceRow(
            id=order_id, tenant_id=tenant_id, name="order-service", environment="prod"
        )
        staging_payment_id = uuid4()
        staging_payment = ServiceRow(
            id=staging_payment_id,
            tenant_id=tenant_id,
            name="payment-service",
            environment="staging",
        )
        incident = IncidentRow(
            id=incident_id,
            tenant_id=tenant_id,
            service_id=payment_id,
            title="Payment errors",
            state="DETECTED",
            severity="SEV2",
            detected_at=detected_at,
        )
        session.add_all([payment, order, staging_payment, incident])
        session.flush()
        session.add_all(
            [
                ServiceDependencyRow(
                    tenant_id=tenant_id,
                    source_service_id=order_id,
                    target_service_id=payment_id,
                    dependency_type="http",
                    valid_from=detected_at - timedelta(days=1),
                ),
                DeploymentRow(
                    tenant_id=tenant_id,
                    service_id=payment_id,
                    environment="prod",
                    version="v1.2.3",
                    commit_sha="abc123",
                    status="completed",
                    started_at=detected_at - timedelta(minutes=4),
                ),
                IncidentEventRow(
                    tenant_id=tenant_id,
                    incident_id=incident_id,
                    event_type="ConfigurationChanged",
                    source="config-controller",
                    idempotency_key="change-1",
                    correlation_id="synthetic-correlation",
                    payload={"summary": "Payment timeout changed"},
                    occurred_at=detected_at - timedelta(minutes=3),
                ),
                LogRecordRow(
                    tenant_id=tenant_id,
                    service_id=payment_id,
                    incident_id=incident_id,
                    source="loki",
                    severity="error",
                    message="Payment authorization failed",
                    external_ref="loki://line-1",
                    observed_at=detected_at - timedelta(minutes=2),
                ),
                LogRecordRow(
                    tenant_id=tenant_id,
                    service_id=staging_payment_id,
                    incident_id=incident_id,
                    source="loki",
                    severity="error",
                    message="Staging-only failure",
                    external_ref="loki://staging-only",
                    observed_at=detected_at - timedelta(minutes=2),
                ),
                MetricSnapshotRow(
                    tenant_id=tenant_id,
                    service_id=payment_id,
                    incident_id=incident_id,
                    metric_name="error_rate",
                    value=0.24,
                    source="prometheus",
                    observed_at=detected_at - timedelta(minutes=1),
                ),
            ]
        )
        session.commit()

        correlation = IncidentCorrelationEngine([DatabaseEvidenceSource(sessions)])
        first = correlation.correlate(session, context)
        session.commit()
        second = correlation.correlate(session, context)
        session.commit()

        assert len(first.evidence) == 6  # detection, config, deployment, dependency, log, metric
        assert len(second.evidence) == len(first.evidence)
        evidence = session.scalars(
            select(IncidentEvidenceRow).where(IncidentEvidenceRow.incident_id == incident_id)
        ).all()
        timeline = session.scalars(
            select(IncidentTimelineRow)
            .where(IncidentTimelineRow.incident_id == incident_id)
            .order_by(IncidentTimelineRow.sequence)
        ).all()
        assert len(evidence) == len(timeline) == 6
        assert all(
            item.service and item.normalized_summary and item.confidence is not None
            for item in evidence
        )
        assert all(item.external_ref and item.content_hash for item in evidence)
        assert all(item.evidence_id is not None for item in timeline)
        assert [item.occurred_at for item in timeline] == sorted(
            item.occurred_at for item in timeline
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(IncidentEvidenceRow)
                .where(IncidentEvidenceRow.tenant_id != tenant_id)
            )
            == 0
        )
    engine_db.dispose()


def test_evidence_contract_rejects_missing_raw_reference_or_invalid_confidence() -> None:
    timestamp = datetime(2026, 10, 3, 14, 5, tzinfo=UTC)
    base = {
        "source": "prometheus",
        "timestamp": timestamp,
        "service": "payment-service",
        "evidence_type": "metric_anomaly",
        "normalized_summary": "Error rate rose",
        "confidence": 1.1,
    }
    with pytest.raises(ValueError):
        EvidenceCandidate.model_validate({**base, "raw_reference": "", "confidence": 0.9})
    with pytest.raises(ValueError):
        EvidenceCandidate.model_validate(
            {**base, "raw_reference": "prometheus://x", "confidence": 1.1}
        )


def test_evidence_cannot_use_naive_timestamps() -> None:
    with pytest.raises(ValueError):
        _candidate(datetime(2026, 10, 3, 14, 5), "log", "loki://line")


def test_evidence_summary_redacts_common_credentials() -> None:
    summary = redact_evidence_summary(
        "request failed Authorization: Bearer abc.def token=secret-value"
    )

    assert "abc.def" not in summary
    assert "secret-value" not in summary
    assert "[REDACTED]" in summary


def test_source_adapters_normalize_prometheus_and_loki_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("query_range") and "prometheus" in request.url.host:
            expression = request.url.params["query"]
            metric = {"service": "payment-service", "pod": "payment-service-abc"}
            if "restarts" in expression:
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"result": [{"metric": metric, "values": [[1791036300, "2"]]}]},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"result": [{"metric": metric, "values": [[1791036300, "0.2"]]}]},
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "order-service", "level": "error"},
                            "values": [
                                [
                                    "1791036300000000000",
                                    (
                                        '{"event_type":"ConfigurationChanged",'
                                        '"event":"payment timeout changed"}'
                                    ),
                                ]
                            ],
                        }
                    ]
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    context = _context(datetime(2026, 10, 3, 14, 5, tzinfo=UTC))
    start, end = CorrelationWindow(before_seconds=600, after_seconds=300).bounds(
        context.detected_at
    )
    prom = list(PrometheusEvidenceSource(client, "http://prometheus").collect(context, start, end))
    loki = list(LokiEvidenceSource(client, "http://loki").collect(context, start, end))

    assert {item.evidence_type for item in prom} == {"metric_anomaly", "pod_restart"}
    assert len(prom) == 4  # three threshold series plus the restart series
    assert len(loki) == 1
    assert loki[0].evidence_type == "configuration_change"
    assert loki[0].raw_reference.startswith("loki://")
    client.close()


def test_internal_correlation_endpoint_requires_bearer_and_persists_evidence(
    tmp_path: Path,
) -> None:
    tenant_id, incident_id, service_id = uuid4(), uuid4(), uuid4()
    database = create_engine(f"sqlite:///{tmp_path / 'correlation-api.db'}")
    Base.metadata.create_all(database)
    sessions = sessionmaker(bind=database, expire_on_commit=False, class_=Session)
    with sessions.begin() as session:
        session.add(
            ServiceRow(
                id=service_id,
                tenant_id=tenant_id,
                name="payment-service",
                environment="prod",
            )
        )
        session.add(
            IncidentRow(
                id=incident_id,
                tenant_id=tenant_id,
                service_id=service_id,
                title="Payment errors",
                state="DETECTED",
                severity="SEV2",
                detected_at=datetime(2026, 10, 3, 14, 5, tzinfo=UTC),
            )
        )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, request=request))
    )
    app = create_app(
        settings=Settings(
            correlation_api_token=SecretStr("test-high-entropy-token"),
            prometheus_url="http://prometheus",
            loki_url="http://loki",
        ),
        session_factory=sessions,
        http_client=client,
    )

    with TestClient(app) as api:
        unauthorized = api.post(f"/incidents/{incident_id}/correlate")
        assert unauthorized.status_code == 401
        wrong_scheme = api.post(
            f"/incidents/{incident_id}/correlate",
            headers={"Authorization": "test-high-entropy-token"},
        )
        assert wrong_scheme.status_code == 401
        response = api.post(
            f"/incidents/{incident_id}/correlate",
            headers={"Authorization": "Bearer test-high-entropy-token"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["evidence_count"] == 1
    assert set(response.json()["source_failures"]) == {"prometheus", "loki"}
    with sessions() as session:
        evidence = session.scalar(
            select(IncidentEvidenceRow).where(IncidentEvidenceRow.incident_id == incident_id)
        )
        timeline = session.scalar(
            select(IncidentTimelineRow).where(IncidentTimelineRow.incident_id == incident_id)
        )
        assert evidence is not None
        assert evidence.source == "opsmind.incidents"
        assert evidence.external_ref == f"incident://{incident_id}"
        assert timeline is not None and timeline.evidence_id == evidence.id
    client.close()
    database.dispose()
