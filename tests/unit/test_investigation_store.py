"""Durable state and append-only trace persistence tests."""

from __future__ import annotations

from uuid import uuid4

from opsmind_incident_engine.investigation import InvestigationTraceStep
from opsmind_incident_engine.investigation_store import InvestigationRunStore
from opsmind_storage.models import IncidentRow, InvestigationRunRow, InvestigationRunStepRow
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def test_run_state_and_each_trace_event_are_persisted(db_session: Session) -> None:
    tenant_id = uuid4()
    incident_id = uuid4()
    db_session.add(
        IncidentRow(
            id=incident_id,
            tenant_id=tenant_id,
            title="Payment latency elevated",
            state="INVESTIGATING",
            severity="SEV2",
        )
    )
    db_session.commit()
    sessions = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, class_=Session)
    run_id = uuid4()
    store = InvestigationRunStore(
        sessions,
        run_id=run_id,
        tenant_id=tenant_id,
        incident_id=incident_id,
        correlation_id="correlation-test",
    )
    store.create()
    store(
        InvestigationTraceStep(
            event_type="state_transition",
            state="HYPOTHESIS_FORMED",
            name="INITIAL_TRIAGE->HYPOTHESIS_FORMED",
            outcome="succeeded",
            duration_ms=0,
            details={"from": "INITIAL_TRIAGE", "to": "HYPOTHESIS_FORMED"},
        ),
        {
            "hypotheses": ["A recent deployment could explain the latency."],
            "missing_information": ["deployment records"],
            "evidence_ids": [],
            "tool_calls": 0,
        },
    )
    store.finish("COMPLETED")

    run = db_session.get(InvestigationRunRow, run_id)
    rows = db_session.scalars(
        select(InvestigationRunStepRow)
        .where(InvestigationRunStepRow.run_id == run_id)
        .order_by(InvestigationRunStepRow.sequence)
    ).all()
    assert run is not None
    assert run.state == "HYPOTHESIS_FORMED"
    assert run.status == "COMPLETED"
    assert run.correlation_id == "correlation-test"
    assert run.state_data["hypotheses"] == ["A recent deployment could explain the latency."]
    assert [row.sequence for row in rows] == [1, 2, 3]
    assert [row.name for row in rows] == [
        "run_created",
        "INITIAL_TRIAGE->HYPOTHESIS_FORMED",
        "run_finished",
    ]


def test_timeout_status_cannot_be_overwritten_by_success_finish(db_session: Session) -> None:
    tenant_id = uuid4()
    incident_id = uuid4()
    db_session.add(
        IncidentRow(
            id=incident_id,
            tenant_id=tenant_id,
            title="Payment errors",
            state="INVESTIGATING",
            severity="SEV2",
        )
    )
    db_session.commit()
    sessions = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False, class_=Session)
    run_id = uuid4()
    store = InvestigationRunStore(
        sessions,
        run_id=run_id,
        tenant_id=tenant_id,
        incident_id=incident_id,
        correlation_id="correlation-timeout",
    )
    store.create()
    store(
        InvestigationTraceStep(
            event_type="run_finished",
            state="HYPOTHESIS_FORMED",
            name="execution_deadline",
            outcome="timed_out",
            duration_ms=1000,
        ),
        {"tool_calls": 0},
    )
    store.finish("COMPLETED")

    run = db_session.get(InvestigationRunRow, run_id)
    assert run is not None
    assert run.status == "TIMED_OUT"
