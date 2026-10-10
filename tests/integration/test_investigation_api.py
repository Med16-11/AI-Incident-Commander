"""Investigation endpoint wiring and durable run inspection tests."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from opsmind_common.config import Settings
from opsmind_incident_engine.investigation import ProviderFailure, ProviderRequest, ProviderTurn
from opsmind_incident_engine.main import create_app
from opsmind_storage.models import Base, IncidentRow, InvestigationRunRow, InvestigationRunStepRow
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


class FinalAnalysisProvider:
    def complete(self, _request: ProviderRequest) -> ProviderTurn:
        unsupported = {
            "text": "Insufficient evidence to establish a root cause.",
            "evidence_status": "insufficient_evidence",
            "evidence_ids": [],
            "historical_incident_ids": [],
        }
        return ProviderTurn(
            content=json.dumps(
                {
                    "summary": unsupported,
                    "severity": {"level": "SEV2", "rationale": unsupported},
                    "affected_services": [],
                    "symptoms": [],
                    "probable_root_cause": unsupported,
                    "confidence": 0.1,
                    "supporting_evidence": [],
                    "historical_context_ids": [],
                    "alternative_hypotheses": [],
                    "recommended_actions": [],
                    "missing_information": [],
                    "investigation_steps": [],
                }
            ),
            tool_calls=[],
        )


def test_investigation_endpoint_persists_run_state_and_returns_run_id(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'investigation-api.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    tenant_id = uuid4()
    incident_id = uuid4()
    with sessions.begin() as session:
        session.add(
            IncidentRow(
                id=incident_id,
                tenant_id=tenant_id,
                title="Payment errors increased",
                state="INVESTIGATING",
                severity="SEV2",
            )
        )

    app = create_app(
        settings=Settings(investigation_api_token=SecretStr("local-test-token")),
        session_factory=sessions,
        http_client=httpx.Client(),
        llm_provider=FinalAnalysisProvider(),
    )
    try:
        with TestClient(app) as client:
            denied = client.post(f"/incidents/{incident_id}/investigate")
            assert denied.status_code == 401
            response = client.post(
                f"/incidents/{incident_id}/investigate",
                headers={"Authorization": "Bearer local-test-token"},
            )
    finally:
        engine.dispose()

    assert response.status_code == 200
    run_id = response.headers["x-investigation-run-id"]
    with sessions() as session:
        run = session.get(InvestigationRunRow, UUID(run_id))
        steps = session.scalars(
            select(InvestigationRunStepRow)
            .where(InvestigationRunStepRow.run_id == UUID(run_id))
            .order_by(InvestigationRunStepRow.sequence)
        ).all()
    assert run is not None
    assert run.tenant_id == tenant_id
    assert run.incident_id == incident_id
    assert run.status == "COMPLETED"
    assert run.state == "HYPOTHESIS_FORMED"
    assert run.state_data["hypotheses"] == ["Insufficient evidence to establish a root cause."]
    assert any(step.name == "llm.complete" for step in steps)


def test_llm_outage_returns_upstream_failure_and_marks_run_failed(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'investigation-provider-outage.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    tenant_id, incident_id = uuid4(), uuid4()
    with sessions.begin() as session:
        session.add(
            IncidentRow(
                id=incident_id,
                tenant_id=tenant_id,
                title="Payment errors increased",
                state="INVESTIGATING",
                severity="SEV2",
            )
        )

    class UnavailableProvider:
        def complete(self, _request: ProviderRequest) -> ProviderTurn:
            raise ProviderFailure("LLM provider request failed")

    app = create_app(
        settings=Settings(investigation_api_token=SecretStr("local-test-token")),
        session_factory=sessions,
        http_client=httpx.Client(),
        llm_provider=UnavailableProvider(),
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/incidents/{incident_id}/investigate",
                headers={"Authorization": "Bearer local-test-token"},
            )
    finally:
        engine.dispose()

    assert response.status_code == 502
    assert response.json()["detail"] == "Evidence-grounded investigation failed"
    with sessions() as session:
        run = session.scalar(
            select(InvestigationRunRow).where(InvestigationRunRow.incident_id == incident_id)
        )
    assert run is not None
    assert run.status == "FAILED"
