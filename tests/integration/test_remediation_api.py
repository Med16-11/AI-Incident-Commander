"""Remediation approval, execution, idempotency, rollback, and audit integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from opsmind_remediation_engine.executors import (
    ExecutionResult,
    RemediationCommand,
    VerificationResult,
)
from opsmind_remediation_engine.main import (
    RemediationPrincipal,
    create_app,
)
from opsmind_storage.models import (
    AuditLogRow,
    Base,
    IncidentRow,
    RemediationApprovalRequestRow,
    RemediationExecutionRow,
)
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request


class FakeAuthentication:
    def __init__(self, principals: dict[str, RemediationPrincipal]) -> None:
        self.principals = principals

    def authenticate(self, request: Request) -> RemediationPrincipal:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.principals:
            raise ValueError("unknown test principal")
        return self.principals[token]


class FakeExecutor:
    def __init__(self) -> None:
        self.executed: list[UUID] = []
        self.rolled_back: list[UUID] = []
        self.verification_succeeds = True

    def execute(self, command: RemediationCommand) -> ExecutionResult:
        self.executed.append(command.action_id)
        return ExecutionResult({"operation": "restarted", "target": command.target})

    def rollback(
        self, command: RemediationCommand, previous_result: dict[str, object]
    ) -> ExecutionResult:
        assert previous_result["execution"]["operation"] == "restarted"  # type: ignore[index]
        self.rolled_back.append(command.action_id)
        return ExecutionResult({"operation": "restored", "target": command.target})

    def verify(self, command: RemediationCommand, result: ExecutionResult) -> VerificationResult:
        del result
        return VerificationResult(
            self.verification_succeeds,
            {"target": command.target, "verified": self.verification_succeeds},
        )


def _setup(
    tmp_path: Path,
) -> tuple[
    Engine,
    sessionmaker[Session],
    TestClient,
    UUID,
    UUID,
    RemediationPrincipal,
    RemediationPrincipal,
    RemediationPrincipal,
    RemediationPrincipal,
    FakeExecutor,
]:
    engine = create_engine(f"sqlite:///{tmp_path / 'remediation.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    tenant_id, incident_id = uuid4(), uuid4()
    ai = RemediationPrincipal(
        tenant_id=tenant_id,
        subject_id=uuid4(),
        permissions=frozenset({"remediations:propose", "remediations:execute"}),
        actor_type="ai",
    )
    requester = RemediationPrincipal(
        tenant_id=tenant_id,
        subject_id=uuid4(),
        permissions=frozenset(
            {"remediations:propose", "remediations:approve", "remediations:read"}
        ),
        actor_type="user",
    )
    approver = RemediationPrincipal(
        tenant_id=tenant_id,
        subject_id=uuid4(),
        permissions=frozenset({"remediations:approve", "remediations:read"}),
        actor_type="user",
    )
    operator = RemediationPrincipal(
        tenant_id=tenant_id,
        subject_id=uuid4(),
        permissions=frozenset(
            {"remediations:execute", "remediations:propose", "remediations:read"}
        ),
        actor_type="user",
    )
    reader = RemediationPrincipal(
        tenant_id=tenant_id,
        subject_id=uuid4(),
        permissions=frozenset({"remediations:read"}),
        actor_type="user",
    )
    with sessions.begin() as session:
        session.add(
            IncidentRow(
                id=incident_id,
                tenant_id=tenant_id,
                title="Payment service errors",
                state="INVESTIGATING",
                severity="SEV2",
            )
        )
    executor = FakeExecutor()
    app = create_app(
        session_factory=sessions,
        authentication_backend=FakeAuthentication(
            {
                "ai": ai,
                "requester": requester,
                "approver": approver,
                "operator": operator,
                "reader": reader,
            }
        ),
        executor=executor,
    )
    return (
        engine,
        sessions,
        TestClient(app),
        tenant_id,
        incident_id,
        ai,
        requester,
        approver,
        operator,
        executor,
    )


def _auth(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {name}"}


def test_ai_can_propose_but_human_must_approve_and_execute(tmp_path: Path) -> None:
    engine, sessions, client, _, incident_id, ai, _, approver, operator, executor = _setup(tmp_path)
    try:
        response = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("ai"), "Idempotency-Key": "restart-1"},
            json={
                "action_type": "restart_service",
                "target": "payment",
                "parameters": {},
                "rationale": "Restart after confirmed worker deadlock",
            },
        )
        assert response.status_code == 201, response.text
        action = response.json()
        assert action["requested_by"] == str(ai.subject_id)
        assert action["risk_level"] == "LOW_RISK"
        action_id = action["action_id"]
        duplicate_proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("ai"), "Idempotency-Key": "restart-1"},
            json={
                "action_type": "restart_service",
                "target": "payment",
                "parameters": {"grace_period_seconds": 30},
                "rationale": "Restart after confirmed worker deadlock",
            },
        )
        assert duplicate_proposal.status_code == 201
        assert duplicate_proposal.json()["action_id"] == action_id
        collision = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("ai"), "Idempotency-Key": "restart-1"},
            json={
                "action_type": "restart_service",
                "target": "orders",
                "parameters": {},
                "rationale": "Different target",
            },
        )
        assert collision.status_code == 409

        denied = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("ai"), "Idempotency-Key": "ai-execute"},
            json={"dry_run": False},
        )
        assert denied.status_code == 403

        approved = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Approved after checking service health"},
        )
        assert approved.status_code == 200
        assert approved.json()["approved_by"] == str(approver.subject_id)
        assert approved.json()["approval"]["approver"] == str(approver.subject_id)
        assert (
            approved.json()["approval"]["decision_reason"]
            == "Approved after checking service health"
        )
        executed = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "execute-1"},
            json={"dry_run": False},
        )
        assert executed.status_code == 200
        assert executed.json()["execution_status"] == "COMPLETED"
        duplicate = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "execute-1"},
            json={"dry_run": False},
        )
        assert duplicate.json()["execution_id"] == executed.json()["execution_id"]
        assert executor.executed == [UUID(action_id)]

        with sessions() as session:
            audit = session.scalars(
                select(AuditLogRow).where(AuditLogRow.entity_id == UUID(action_id))
            ).all()
            assert [entry.action for entry in audit] == [
                "remediation.approval_requested",
                "remediation.approved",
                "remediation.execution_started",
                "remediation.execution_completed",
            ]
            assert audit[0].actor_type == "ai"
            assert session.scalar(select(func.count()).select_from(RemediationExecutionRow)) == 1
    finally:
        client.close()
        engine.dispose()


def test_self_approval_is_rejected_and_dry_run_does_not_execute(tmp_path: Path) -> None:
    engine, _, client, _, incident_id, _, _, _, _, executor = _setup(tmp_path)
    try:
        proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("requester"), "Idempotency-Key": "cache-1"},
            json={
                "action_type": "clear_cache",
                "target": "orders",
                "parameters": {"cache_name": "order-cache"},
                "rationale": "Stale cache values are affecting order reads",
            },
        )
        action_id = proposal.json()["action_id"]
        self_approval = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("requester"),
            json={"reason": "Self approval is not allowed"},
        )
        assert self_approval.status_code == 409

        assert (
            client.post(
                f"/remediations/{action_id}/approve",
                headers=_auth("approver"),
                json={"reason": "Reviewed impact and rollback plan"},
            ).status_code
            == 200
        )
        dry_run = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "dry-run-1"},
            json={"dry_run": True},
        )
        assert dry_run.status_code == 200
        assert dry_run.json()["execution_status"] == "DRY_RUN"
        assert dry_run.json()["result"]["planned"] is True
        assert executor.executed == []
    finally:
        client.close()
        engine.dispose()


def test_approval_security_boundaries_and_rejection_are_enforced(tmp_path: Path) -> None:
    engine, _, client, _, incident_id, _, _, approver, operator, executor = _setup(tmp_path)
    try:
        proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("requester"), "Idempotency-Key": "approval-boundaries"},
            json={
                "action_type": "restart_service",
                "target": "payment",
                "parameters": {},
                "rationale": "Restart after confirmed deadlock",
            },
        )
        action_id = proposal.json()["action_id"]
        assert proposal.json()["status"] == "approval_requested"
        assert proposal.json()["approval"]["action"] == "restart_service"
        assert proposal.json()["approval"]["target"] == "payment"
        assert proposal.json()["approval"]["risk"] == "LOW_RISK"
        assert proposal.json()["approval"]["expires_at"]

        unauthorized_user = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("reader"),
            json={"reason": "Read-only users cannot approve"},
        )
        assert unauthorized_user.status_code == 403

        before_approval = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "before-approval"},
            json={},
        )
        assert before_approval.status_code == 409
        assert executor.executed == []

        unauthorized = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("ai"),
            json={"reason": "AI cannot approve"},
        )
        assert unauthorized.status_code == 403

        first = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Reviewed service health and impact"},
        )
        assert first.status_code == 200
        assert first.json()["approval"]["approver"] == str(approver.subject_id)
        assert first.json()["approval"]["timestamp"]

        second = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Second approval attempt"},
        )
        assert second.status_code == 409
        assert (
            client.get(f"/remediations/{action_id}", headers=_auth("approver")).json()["approval"][
                "decision_reason"
            ]
            == "Reviewed service health and impact"
        )
    finally:
        client.close()
        engine.dispose()


def test_rejection_is_terminal_and_cannot_execute(tmp_path: Path) -> None:
    engine, sessions, client, _, incident_id, _, _, _, operator, executor = _setup(tmp_path)
    try:
        proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("requester"), "Idempotency-Key": "reject-action"},
            json={
                "action_type": "clear_cache",
                "target": "orders",
                "parameters": {"cache_name": "order-cache"},
                "rationale": "Clear stale order cache",
            },
        )
        action_id = proposal.json()["action_id"]
        rejected = client.post(
            f"/remediations/{action_id}/reject",
            headers=_auth("approver"),
            json={"reason": "Evidence does not justify cache invalidation"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["approval"]["decision"] == "rejected"
        assert rejected.json()["approval"]["approver"]
        assert rejected.json()["approval"]["timestamp"]

        duplicate_decision = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Change my mind"},
        )
        assert duplicate_decision.status_code == 409
        execution = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "execute-rejected"},
            json={},
        )
        assert execution.status_code == 409
        assert executor.executed == []
        with sessions() as session:
            approval = session.scalar(
                select(RemediationApprovalRequestRow).where(
                    RemediationApprovalRequestRow.action_id == UUID(action_id)
                )
            )
            assert approval is not None
            assert approval.decision_reason == "Evidence does not justify cache invalidation"
    finally:
        client.close()
        engine.dispose()


def test_expired_approval_is_persisted_and_never_executed(tmp_path: Path) -> None:
    engine, sessions, client, _, incident_id, _, _, _, operator, executor = _setup(tmp_path)
    try:
        proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("requester"), "Idempotency-Key": "expire-action"},
            json={
                "action_type": "restart_service",
                "target": "payment",
                "parameters": {},
                "rationale": "Recover payment worker",
            },
        )
        action_id = UUID(proposal.json()["action_id"])
        with sessions.begin() as session:
            approval = session.scalar(
                select(RemediationApprovalRequestRow).where(
                    RemediationApprovalRequestRow.action_id == action_id
                )
            )
            assert approval is not None
            approval.expires_at = datetime(2000, 1, 1, tzinfo=UTC)

        expired = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Too late to approve"},
        )
        assert expired.status_code == 410
        details = client.get(f"/remediations/{action_id}", headers=_auth("approver"))
        assert details.json()["status"] == "expired"
        assert details.json()["approval"]["decision"] == "expired"

        execution = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "execute-expired"},
            json={},
        )
        assert execution.status_code == 409
        assert executor.executed == []
    finally:
        client.close()
        engine.dispose()


def test_remediation_timeout_is_audited_and_marked_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, sessions, client, _, incident_id, _, _, _, operator, executor = _setup(tmp_path)

    def timeout_execute(_executor: FakeExecutor, _command: RemediationCommand) -> ExecutionResult:
        raise TimeoutError("adapter timeout")

    monkeypatch.setattr(FakeExecutor, "execute", timeout_execute)
    try:
        proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("requester"), "Idempotency-Key": "timeout-action"},
            json={
                "action_type": "restart_service",
                "target": "payment",
                "parameters": {},
                "rationale": "Test adapter timeout handling",
            },
        )
        action_id = UUID(proposal.json()["action_id"])
        approval = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Approved for timeout regression"},
        )
        assert approval.status_code == 200

        execution = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "timeout-execution"},
            json={},
        )

        assert execution.status_code == 200
        assert execution.json()["execution_status"] == "FAILED"
        assert execution.json()["result"]["error"] == "Remediation adapter failed"
        details = client.get(f"/remediations/{action_id}", headers=_auth("operator"))
        assert details.json()["status"] == "failed"
        with sessions() as session:
            actions = session.scalars(
                select(AuditLogRow).where(AuditLogRow.action == "remediation.execution_failed")
            ).all()
            assert any(action.entity_id == action_id for action in actions)
    finally:
        client.close()
        engine.dispose()


def test_rollback_is_a_separately_approved_compensating_action(tmp_path: Path) -> None:
    engine, _, client, _, incident_id, _, _, _, _, executor = _setup(tmp_path)
    try:
        proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("requester"), "Idempotency-Key": "scale-1"},
            json={
                "action_type": "scale_service",
                "target": "payment",
                "parameters": {"replicas": 4},
                "rationale": "Increase capacity for traffic spike",
            },
        )
        action_id = proposal.json()["action_id"]
        client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Reviewed capacity need"},
        )
        client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "scale-exec"},
            json={},
        )
        rollback = client.post(
            f"/remediations/{action_id}/rollback",
            headers={**_auth("operator"), "Idempotency-Key": "scale-rollback"},
        )
        assert rollback.status_code == 201
        rollback_id = rollback.json()["action_id"]
        assert rollback.json()["compensates_action_id"] == action_id
        assert rollback.json()["status"] == "approval_requested"
        client.post(
            f"/remediations/{rollback_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Approve compensating action"},
        )
        result = client.post(
            f"/remediations/{rollback_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "scale-rollback-exec"},
            json={},
        )
        assert result.status_code == 200
        assert result.json()["result"]["execution"]["operation"] == "restored"
        assert executor.rolled_back == [UUID(action_id)]
    finally:
        client.close()
        engine.dispose()


def test_verification_failure_requests_rollback_approval_without_executing_it(
    tmp_path: Path,
) -> None:
    engine, _, client, _, incident_id, _, _, _, _, executor = _setup(tmp_path)
    executor.verification_succeeds = False
    try:
        proposal = client.post(
            f"/incidents/{incident_id}/remediations",
            headers={**_auth("requester"), "Idempotency-Key": "verification-failure"},
            json={
                "action_type": "restart_service",
                "target": "payment",
                "parameters": {},
                "rationale": "Recover the unhealthy payment process",
            },
        )
        action_id = proposal.json()["action_id"]
        approved = client.post(
            f"/remediations/{action_id}/approve",
            headers=_auth("approver"),
            json={"reason": "Approved after checking impact"},
        )
        assert approved.status_code == 200
        result = client.post(
            f"/remediations/{action_id}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "verification-failure-exec"},
            json={},
        )
        assert result.status_code == 200
        assert result.json()["execution_status"] == "VERIFICATION_FAILED"
        rollback = result.json()["rollback_action"]
        assert rollback["status"] == "approval_requested"
        assert rollback["compensates_action_id"] == action_id
        assert rollback["approval"]["decision"] == "requested"
        assert executor.executed == [UUID(action_id)]
        assert executor.rolled_back == []

        unapproved_execution = client.post(
            f"/remediations/{rollback['action_id']}/execute",
            headers={**_auth("operator"), "Idempotency-Key": "rollback-before-approval"},
            json={},
        )
        assert unapproved_execution.status_code == 409
        assert executor.rolled_back == []
    finally:
        client.close()
        engine.dispose()
