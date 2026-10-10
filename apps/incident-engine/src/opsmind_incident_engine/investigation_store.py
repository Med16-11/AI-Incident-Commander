"""SQL persistence adapter for investigation state and append-only trace steps."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from opsmind_storage.models import InvestigationRunRow, InvestigationRunStepRow
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from opsmind_incident_engine.investigation import (
    InvestigationTraceStep,
)


class InvestigationRunStore:
    """Persist each phase change and execution trace in a short database transaction."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        run_id: UUID,
        tenant_id: UUID,
        incident_id: UUID,
        correlation_id: str,
    ) -> None:
        self._sessions = sessions
        self.run_id = run_id
        self._tenant_id = tenant_id
        self._incident_id = incident_id
        self._correlation_id = correlation_id[:128] or str(uuid4())
        self._sequence = 0

    def create(self) -> None:
        """Create the durable run before any provider or evidence source is called."""
        with self._sessions() as session, session.begin():
            session.add(
                InvestigationRunRow(
                    id=self.run_id,
                    tenant_id=self._tenant_id,
                    incident_id=self._incident_id,
                    correlation_id=self._correlation_id,
                    state="INITIAL_TRIAGE",
                    status="RUNNING",
                    state_data={"hypotheses": [], "missing_information": [], "evidence_ids": []},
                )
            )
            session.add(
                InvestigationRunStepRow(
                    run_id=self.run_id,
                    sequence=1,
                    event_type="state_transition",
                    state="INITIAL_TRIAGE",
                    name="run_created",
                    outcome="succeeded",
                    duration_ms=0,
                    details={"incident_id": str(self._incident_id)},
                )
            )
        self._sequence = 1

    def __call__(self, step: InvestigationTraceStep, state_data: dict[str, object]) -> None:
        """Store sanitized trace details and the current agent memory atomically."""
        with self._sessions() as session, session.begin():
            run = session.scalar(
                select(InvestigationRunRow)
                .where(
                    InvestigationRunRow.id == self.run_id,
                    InvestigationRunRow.tenant_id == self._tenant_id,
                    InvestigationRunRow.incident_id == self._incident_id,
                )
                .with_for_update()
            )
            if run is None:
                raise LookupError("Investigation run is unavailable")
            self._sequence += 1
            run.state = step.state
            run.state_data = state_data
            tool_calls = state_data.get("tool_calls", 0)
            run.tool_calls = tool_calls if isinstance(tool_calls, int) else 0
            if step.event_type == "run_finished" and step.outcome == "timed_out":
                run.status = "TIMED_OUT"
                run.finished_at = datetime.now(UTC)
            session.add(
                InvestigationRunStepRow(
                    run_id=self.run_id,
                    sequence=self._sequence,
                    event_type=step.event_type,
                    state=step.state,
                    name=step.name[:160],
                    outcome=step.outcome,
                    duration_ms=step.duration_ms,
                    details=step.details,
                )
            )

    def finish(self, status: str) -> None:
        """Mark a run terminal after completion, error, or deadline exhaustion."""
        if status not in {"COMPLETED", "FAILED", "TIMED_OUT"}:
            raise ValueError("invalid terminal investigation status")
        with self._sessions() as session, session.begin():
            run = session.scalar(
                select(InvestigationRunRow)
                .where(InvestigationRunRow.id == self.run_id)
                .with_for_update()
            )
            if run is None:
                raise LookupError("Investigation run is unavailable")
            if run.status != "RUNNING":
                return
            run.status = status
            run.finished_at = datetime.now(UTC)
            self._sequence += 1
            session.add(
                InvestigationRunStepRow(
                    run_id=self.run_id,
                    sequence=self._sequence,
                    event_type="run_finished",
                    state=run.state,
                    name="run_finished",
                    outcome="succeeded" if status == "COMPLETED" else "failed",
                    duration_ms=0,
                    details={"status": status},
                )
            )
