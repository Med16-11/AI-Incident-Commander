"""Audited, policy-gated remediation service with explicit identity and executor ports."""

# ruff: noqa: B008

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_common.logging import configure_logging
from opsmind_models.domain.remediation import RemediationActionType, RemediationRisk
from opsmind_observability.metrics import ServiceMetrics
from opsmind_storage.database import create_database_engine, create_session_factory
from opsmind_storage.models import (
    AuditLogRow,
    IncidentRow,
    RemediationActionRow,
    RemediationApprovalRequestRow,
    RemediationExecutionRow,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from starlette.responses import JSONResponse

from opsmind_remediation_engine.executors import (
    ExecutionResult,
    RemediationCommand,
    RemediationExecutor,
    UnavailableRemediationExecutor,
    VerificationResult,
)
from opsmind_remediation_engine.kubernetes_adapter import KubernetesRemediationAdapter
from opsmind_remediation_engine.policy import (
    RemediationPolicyEngine,
    RemediationPolicyError,
    ValidatedAction,
)


class RemediationPrincipal(BaseModel):
    """Trusted identity supplied by the configured authentication integration."""

    tenant_id: UUID
    subject_id: UUID
    permissions: frozenset[str]
    actor_type: Literal["user", "service", "ai"]
    model_config = ConfigDict(frozen=True)


class RemediationAuthenticationBackend(Protocol):
    def authenticate(self, request: Request) -> RemediationPrincipal: ...


class RemediationAuthorizationPolicy(Protocol):
    def authorize(self, principal: RemediationPrincipal, permission: str) -> None: ...


class DenyAllAuthentication(RemediationAuthenticationBackend):
    """Safe default when no identity provider is wired into the service."""

    def authenticate(self, request: Request) -> RemediationPrincipal:
        del request
        raise HTTPException(401, "Remediation authentication is not configured")


class LocalDemoAuthentication(RemediationAuthenticationBackend):
    """Single-tenant bearer authentication for an explicitly configured local demo."""

    _tenant_id = UUID("a0000000-0000-4000-8000-000000000001")
    _subject_id = UUID("a0000000-0000-4000-8000-000000000002")
    _permissions = frozenset(
        {
            "incidents:read",
            "remediations:read",
            "remediations:propose",
            "remediations:approve",
            "remediations:execute",
        }
    )

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise ValueError("local demo token must not be empty")
        self._token = token

    def authenticate(self, request: Request) -> RemediationPrincipal:
        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(
            supplied.encode("utf-8"), self._token.encode("utf-8")
        ):
            raise HTTPException(401, "Invalid local demo credential")
        return RemediationPrincipal(
            tenant_id=self._tenant_id,
            subject_id=self._subject_id,
            permissions=self._permissions,
            actor_type="user",
        )


class PermissionAuthorization(RemediationAuthorizationPolicy):
    def authorize(self, principal: RemediationPrincipal, permission: str) -> None:
        if permission not in principal.permissions and "*" not in principal.permissions:
            raise HTTPException(403, f"Permission required: {permission}")


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_type: RemediationActionType
    target: str = Field(min_length=1, max_length=1000)
    parameters: dict[str, object] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=2000)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(min_length=1, max_length=1000)


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dry_run: bool = False


class ActionResponse(BaseModel):
    action_id: UUID
    incident_id: UUID
    action_type: RemediationActionType
    target: str
    parameters: dict[str, Any]
    risk_level: RemediationRisk
    requested_by: UUID
    approved_by: UUID | None
    status: str
    execution_start: datetime | None
    execution_end: datetime | None
    result: dict[str, Any] | None
    compensates_action_id: UUID | None
    rationale: str
    created_at: datetime
    updated_at: datetime
    approval: ApprovalResponse | None = None


class ApprovalResponse(BaseModel):
    approval_request_id: UUID
    action_id: UUID
    action: RemediationActionType
    target: str
    risk: RemediationRisk
    reason: str
    requested_at: datetime
    expires_at: datetime
    decision: str
    approver: UUID | None
    timestamp: datetime | None
    decision_reason: str | None


class ExecutionResponse(BaseModel):
    action: ActionResponse
    execution_id: UUID
    execution_status: str
    started_at: datetime
    completed_at: datetime | None
    result: dict[str, Any] | None
    rollback_action: ActionResponse | None = None


class PageResponse(BaseModel):
    items: list[ActionResponse]
    total: int
    limit: int
    offset: int


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    authentication_backend: RemediationAuthenticationBackend | None = None,
    authorization_policy: RemediationAuthorizationPolicy | None = None,
    executor: RemediationExecutor | None = None,
    policy_engine: RemediationPolicyEngine | None = None,
) -> FastAPI:
    """Compose the service with injected persistence, identity, and infrastructure adapters."""
    configuration = settings or Settings()
    configure_logging(configuration)
    logger = structlog.get_logger(service="remediation-engine")
    engine = create_database_engine(configuration.database_url) if session_factory is None else None
    sessions = session_factory or create_session_factory(engine)  # type: ignore[arg-type]
    if executor is not None:
        action_executor = executor
    elif configuration.kubernetes_enabled:
        action_executor = KubernetesRemediationAdapter.from_cluster(
            namespace=configuration.kubernetes_namespace,
            request_timeout_seconds=configuration.kubernetes_request_timeout_seconds,
            max_retries=configuration.kubernetes_max_retries,
            retry_backoff_seconds=configuration.kubernetes_retry_backoff_seconds,
            rollout_timeout_seconds=configuration.kubernetes_rollout_timeout_seconds,
            dry_run=configuration.kubernetes_dry_run,
        )
    else:
        action_executor = UnavailableRemediationExecutor()
    policy = policy_engine or RemediationPolicyEngine()
    configured_token = (
        configuration.local_demo_token.get_secret_value().strip()
        if configuration.local_demo_token is not None
        else ""
    )
    if authentication_backend is not None:
        auth = authentication_backend
    elif (
        configuration.environment.casefold() in {"local", "dev"}
        and configuration.local_demo_auth_enabled
        and configured_token
    ):
        auth = LocalDemoAuthentication(configured_token)
    else:
        auth = DenyAllAuthentication()
    authorization = authorization_policy or PermissionAuthorization()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        title="OpsMind Remediation Engine",
        description=(
            "Policy-gated, approved-only remediation. AI callers can propose actions but have "
            "no execution authority."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.session_factory = sessions
    metrics = ServiceMetrics("remediation-engine")
    metrics.install(app)
    app.add_middleware(CorrelationIdMiddleware)

    @app.middleware("http")
    async def remediation_operation_metrics(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if "/remediations" in request.url.path and request.method in {"POST", "PATCH"}:
            action_name = request.url.path.rsplit("/", 1)[-1]
            result = "success" if response.status_code < 400 else "error"
            metrics.remediation_actions.labels(action_name, result).inc()
        return response

    @app.exception_handler(Exception)
    async def error_handler(request: Request, exc: Exception) -> JSONResponse:
        code = "storage_unavailable" if isinstance(exc, SQLAlchemyError) else "internal_error"
        status_code = 503 if code == "storage_unavailable" else 500
        logger.error("remediation_request_failed", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": code,
                    "message": "Remediation request could not be completed",
                    "correlation_id": getattr(request.state, "correlation_id", "unknown"),
                }
            },
        )

    def current_principal(request: Request) -> RemediationPrincipal:
        return auth.authenticate(request)

    def require(actor: RemediationPrincipal, permission: str) -> None:
        authorization.authorize(actor, permission)

    @app.get("/health/live", tags=["health"])
    def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "remediation-engine"}

    @app.get("/health/ready", tags=["health"], response_model=None)
    def readiness() -> Response | dict[str, str]:
        try:
            with sessions() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return Response(
                content='{"status":"not_ready","service":"remediation-engine"}',
                status_code=503,
                media_type="application/json",
            )
        return {"status": "ready", "service": "remediation-engine"}

    @app.post(
        "/incidents/{incident_id}/remediations",
        response_model=ActionResponse,
        status_code=201,
    )
    def propose(
        incident_id: UUID,
        body: ProposalRequest,
        request: Request,
        actor: RemediationPrincipal = Depends(current_principal),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ) -> ActionResponse:
        require(actor, "remediations:propose")
        _validate_idempotency_key(idempotency_key)
        validated = _validate(policy, body.action_type.value, body.target, body.parameters)
        digest = _proposal_digest(validated.digest, body.rationale, incident_id)
        try:
            with sessions.begin() as session:
                incident = session.scalar(
                    select(IncidentRow)
                    .where(
                        IncidentRow.tenant_id == actor.tenant_id,
                        IncidentRow.id == incident_id,
                    )
                    .with_for_update()
                )
                if incident is None:
                    raise HTTPException(404, "Incident was not found")
                if incident.state in {"RESOLVED", "CLOSED"}:
                    raise HTTPException(409, "Resolved incidents cannot receive new remediation")
                existing = session.scalar(
                    select(RemediationActionRow).where(
                        RemediationActionRow.tenant_id == actor.tenant_id,
                        RemediationActionRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.action_digest != digest or existing.incident_id != incident_id:
                        raise HTTPException(
                            409, "Idempotency key was used for a different proposal"
                        )
                    return _action_response(existing)
                row = RemediationActionRow(
                    id=uuid4(),
                    tenant_id=actor.tenant_id,
                    incident_id=incident_id,
                    action_type=validated.action_type.value,
                    target=body.target,
                    parameters=validated.parameters.model_dump(mode="json"),
                    risk_level=validated.risk_level.value.removesuffix("_RISK").lower(),
                    status="approval_requested",
                    idempotency_key=idempotency_key,
                    action_digest=digest,
                    requested_by=actor.subject_id,
                    rationale=body.rationale,
                    expires_at=datetime.now(UTC)
                    + timedelta(seconds=configuration.remediation_approval_ttl_seconds),
                )
                session.add(row)
                session.flush()
                approval = RemediationApprovalRequestRow(
                    id=uuid4(),
                    tenant_id=actor.tenant_id,
                    action_id=row.id,
                    action_type=row.action_type,
                    target=row.target,
                    risk_level=row.risk_level,
                    request_reason=row.rationale,
                    expires_at=row.expires_at,
                    decision="requested",
                )
                row.approval_request = approval
                session.add(approval)
                session.flush()
                _audit(
                    session,
                    actor=actor,
                    request=request,
                    row=row,
                    action="remediation.approval_requested",
                    before=None,
                    after={**_action_dict(row), "approval": _approval_dict(approval)},
                )
                response = _action_response(row)
        except IntegrityError as exc:
            with sessions() as session:
                existing = session.scalar(
                    select(RemediationActionRow).where(
                        RemediationActionRow.tenant_id == actor.tenant_id,
                        RemediationActionRow.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise HTTPException(409, "Proposal conflicts with a concurrent update") from exc
                if existing.action_digest != digest or existing.incident_id != incident_id:
                    raise HTTPException(
                        409, "Idempotency key was used for a different proposal"
                    ) from exc
                return _action_response(existing)
        return response

    @app.get("/incidents/{incident_id}/remediations", response_model=PageResponse)
    def list_actions(
        incident_id: UUID,
        actor: RemediationPrincipal = Depends(current_principal),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> PageResponse:
        require(actor, "remediations:read")
        with sessions() as session:
            base = select(RemediationActionRow).where(
                RemediationActionRow.tenant_id == actor.tenant_id,
                RemediationActionRow.incident_id == incident_id,
            )
            total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
            rows = session.scalars(
                base.order_by(RemediationActionRow.created_at.desc()).limit(limit).offset(offset)
            ).all()
            return PageResponse(
                items=[_action_response(row) for row in rows],
                total=int(total),
                limit=limit,
                offset=offset,
            )

    @app.get("/remediations/{action_id}", response_model=ActionResponse)
    def get_action(
        action_id: UUID, actor: RemediationPrincipal = Depends(current_principal)
    ) -> ActionResponse:
        require(actor, "remediations:read")
        with sessions() as session:
            return _action_response(_get_action(session, actor.tenant_id, action_id))

    @app.post("/remediations/{action_id}/approve", response_model=ActionResponse)
    def approve(
        action_id: UUID,
        body: ApprovalRequest,
        request: Request,
        actor: RemediationPrincipal = Depends(current_principal),
    ) -> ActionResponse:
        _require_human(actor)
        require(actor, "remediations:approve")
        try:
            with sessions.begin() as session:
                row = _get_action(session, actor.tenant_id, action_id, lock=True)
                approval = _get_approval(session, actor.tenant_id, row.id, lock=True)
                if approval.decision != "requested":
                    raise HTTPException(409, "Approval request has already been decided")
                if _is_expired(approval):
                    _expire_approval(session, approval, row, actor, request, _action_dict(row))
                    response = None
                elif row.status != "approval_requested":
                    raise HTTPException(409, "Action is not awaiting approval")
                elif (
                    approval.action_type != row.action_type
                    or approval.target != row.target
                    or approval.risk_level != row.risk_level
                    or approval.request_reason != row.rationale
                ):
                    raise HTTPException(409, "Approval snapshot does not match the proposed action")
                elif row.requested_by == actor.subject_id:
                    raise HTTPException(409, "The proposal requester cannot approve the action")
                else:
                    before = _action_dict(row)
                    now = datetime.now(UTC)
                    approval.decision = "approved"
                    approval.approver_id = actor.subject_id
                    approval.decision_at = now
                    approval.decision_reason = body.reason
                    row.approved_by = actor.subject_id
                    row.approved_at = now
                    row.status = "approved"
                    session.flush()
                    _audit(
                        session,
                        actor=actor,
                        request=request,
                        row=row,
                        action="remediation.approved",
                        before=before,
                        after={**_action_dict(row), "approval": _approval_dict(approval)},
                    )
                    response = _action_response(row)
            if response is None:
                raise HTTPException(410, "Approval request has expired")
            return response
        except StaleDataError as exc:
            raise HTTPException(409, "Remediation action changed concurrently") from exc

    @app.post("/remediations/{action_id}/reject", response_model=ActionResponse)
    def reject(
        action_id: UUID,
        body: ApprovalRequest,
        request: Request,
        actor: RemediationPrincipal = Depends(current_principal),
    ) -> ActionResponse:
        _require_human(actor)
        require(actor, "remediations:approve")
        try:
            with sessions.begin() as session:
                row = _get_action(session, actor.tenant_id, action_id, lock=True)
                approval = _get_approval(session, actor.tenant_id, row.id, lock=True)
                if approval.decision != "requested":
                    raise HTTPException(409, "Approval request has already been decided")
                if _is_expired(approval):
                    _expire_approval(session, approval, row, actor, request, _action_dict(row))
                    response = None
                elif row.status != "approval_requested":
                    raise HTTPException(409, "Action is not awaiting approval")
                elif row.requested_by == actor.subject_id:
                    raise HTTPException(409, "The proposal requester cannot reject their action")
                elif not _approval_matches(row, approval):
                    raise HTTPException(409, "Approval snapshot does not match the proposed action")
                else:
                    before = _action_dict(row)
                    approval.decision = "rejected"
                    approval.approver_id = actor.subject_id
                    approval.decision_at = datetime.now(UTC)
                    approval.decision_reason = body.reason
                    row.status = "rejected"
                    session.flush()
                    _audit(
                        session,
                        actor=actor,
                        request=request,
                        row=row,
                        action="remediation.rejected",
                        before=before,
                        after={**_action_dict(row), "approval": _approval_dict(approval)},
                    )
                    response = _action_response(row)
            if response is None:
                raise HTTPException(410, "Approval request has expired")
            return response
        except StaleDataError as exc:
            raise HTTPException(409, "Remediation action changed concurrently") from exc

    @app.post("/remediations/{action_id}/execute", response_model=ExecutionResponse)
    def execute(
        action_id: UUID,
        body: ExecuteRequest,
        request: Request,
        actor: RemediationPrincipal = Depends(current_principal),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ) -> ExecutionResponse:
        _require_human(actor)
        require(actor, "remediations:execute")
        _validate_idempotency_key(idempotency_key)
        payload = {"dry_run": body.dry_run}
        expired = False
        dry_response: ExecutionResponse | None = None
        with sessions.begin() as session:
            row = _get_action(session, actor.tenant_id, action_id, lock=True)
            existing = session.scalar(
                select(RemediationExecutionRow).where(
                    RemediationExecutionRow.tenant_id == actor.tenant_id,
                    RemediationExecutionRow.action_id == action_id,
                    RemediationExecutionRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_payload != payload:
                    raise HTTPException(
                        409, "Idempotency key was used with different execution options"
                    )
                return _execution_response(row, existing)
            approval = _get_approval(session, actor.tenant_id, row.id, lock=True)
            if approval.decision == "requested" and _is_expired(approval):
                _expire_approval(session, approval, row, actor, request, _action_dict(row))
                expired = True
            elif approval.decision != "approved":
                raise HTTPException(409, "Only explicitly approved actions can execute")
            elif (
                row.status != "approved"
                or row.approved_by is None
                or approval.approver_id != row.approved_by
                or approval.decision_reason is None
                or not _approval_matches(row, approval)
            ):
                raise HTTPException(409, "Approval record does not authorize this action")
            if expired:
                execution_id = uuid4()
            else:
                validated = _validate(policy, row.action_type, row.target, row.parameters)
                if (
                    row.action_digest is None
                    or not _matches_digest(row, validated)
                    or row.risk_level != validated.risk_level.value.removesuffix("_RISK").lower()
                ):
                    raise HTTPException(409, "Approved action no longer matches its policy digest")
                started_at = datetime.now(UTC)
                execution = RemediationExecutionRow(
                    id=uuid4(),
                    tenant_id=actor.tenant_id,
                    action_id=row.id,
                    idempotency_key=idempotency_key,
                    status="DRY_RUN" if body.dry_run else "RUNNING",
                    request_payload=payload,
                    result_payload=(
                        {"planned": True, "action_type": row.action_type, "target": row.target}
                        if body.dry_run
                        else None
                    ),
                    started_at=started_at,
                    completed_at=started_at if body.dry_run else None,
                )
                before = _action_dict(row)
                session.add(execution)
                if body.dry_run:
                    row.result = execution.result_payload
                else:
                    row.status = "running"
                    row.execution_start = started_at
                session.flush()
                _audit(
                    session,
                    actor=actor,
                    request=request,
                    row=row,
                    action=(
                        "remediation.dry_run" if body.dry_run else "remediation.execution_started"
                    ),
                    before=before,
                    after={**_action_dict(row), "execution_id": str(execution.id)},
                )
                if body.dry_run:
                    dry_response = _execution_response(row, execution)
                execution_id = execution.id
        if expired:
            raise HTTPException(410, "Approval request expired before action execution")
        if dry_response is not None:
            return dry_response

        try:
            outcome = _execute_adapter(action_executor, sessions, actor, action_id, policy)
        except Exception as exc:
            return _finish_execution(
                sessions,
                action_id=action_id,
                execution_id=execution_id,
                actor=actor,
                request=request,
                outcome=None,
                failure_type=type(exc).__name__,
                logger=logger,
                policy=policy,
                approval_ttl_seconds=configuration.remediation_approval_ttl_seconds,
            )
        return _finish_execution(
            sessions,
            action_id=action_id,
            execution_id=execution_id,
            actor=actor,
            request=request,
            outcome=outcome,
            failure_type=None,
            logger=logger,
            policy=policy,
            approval_ttl_seconds=configuration.remediation_approval_ttl_seconds,
        )

    @app.post(
        "/remediations/{action_id}/rollback",
        response_model=ActionResponse,
        status_code=201,
    )
    def propose_rollback(
        action_id: UUID,
        request: Request,
        actor: RemediationPrincipal = Depends(current_principal),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ) -> ActionResponse:
        _require_human(actor)
        require(actor, "remediations:propose")
        _validate_idempotency_key(idempotency_key)
        with sessions.begin() as session:
            original = _get_action(session, actor.tenant_id, action_id, lock=True)
            if (
                original.status not in {"completed", "verification_failed"}
                or original.result is None
            ):
                raise HTTPException(409, "Only an executed action with a result can be rolled back")
            existing = session.scalar(
                select(RemediationActionRow).where(
                    RemediationActionRow.tenant_id == actor.tenant_id,
                    RemediationActionRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.compensates_action_id != original.id:
                    raise HTTPException(409, "Idempotency key was used for a different rollback")
                return _action_response(existing)
            validated = _validate(
                policy, original.action_type, original.target, original.parameters
            )
            rollback_rationale = f"Compensation for remediation {original.id}"
            rollback = RemediationActionRow(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                incident_id=original.incident_id,
                action_type=original.action_type,
                target=original.target,
                parameters=original.parameters,
                risk_level=validated.risk_level.value.removesuffix("_RISK").lower(),
                status="approval_requested",
                idempotency_key=idempotency_key,
                action_digest=_proposal_digest(
                    validated.digest, rollback_rationale, original.incident_id
                ),
                requested_by=actor.subject_id,
                rationale=rollback_rationale,
                compensates_action_id=original.id,
                expires_at=datetime.now(UTC)
                + timedelta(seconds=configuration.remediation_approval_ttl_seconds),
            )
            session.add(rollback)
            session.flush()
            rollback_approval = RemediationApprovalRequestRow(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                action_id=rollback.id,
                action_type=rollback.action_type,
                target=rollback.target,
                risk_level=rollback.risk_level,
                request_reason=rollback.rationale,
                expires_at=rollback.expires_at,
                decision="requested",
            )
            rollback.approval_request = rollback_approval
            session.add(rollback_approval)
            session.flush()
            _audit(
                session,
                actor=actor,
                request=request,
                row=rollback,
                action="remediation.rollback_proposed",
                before=None,
                after={**_action_dict(rollback), "compensates_action_id": str(original.id)},
            )
            return _action_response(rollback)

    return app


def _get_action(
    session: Session, tenant_id: UUID, action_id: UUID, *, lock: bool = False
) -> RemediationActionRow:
    statement = select(RemediationActionRow).where(
        RemediationActionRow.tenant_id == tenant_id,
        RemediationActionRow.id == action_id,
    )
    if lock:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    if row is None:
        raise HTTPException(404, "Remediation action was not found")
    return row


def _get_approval(
    session: Session, tenant_id: UUID, action_id: UUID, *, lock: bool = False
) -> RemediationApprovalRequestRow:
    statement = select(RemediationApprovalRequestRow).where(
        RemediationApprovalRequestRow.tenant_id == tenant_id,
        RemediationApprovalRequestRow.action_id == action_id,
    )
    if lock:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    if row is None:
        raise HTTPException(409, "Action has no approval request")
    return row


def _approval_dict(row: RemediationApprovalRequestRow) -> dict[str, object]:
    return {
        "approval_request_id": str(row.id),
        "action_id": str(row.action_id),
        "action": row.action_type,
        "target": row.target,
        "risk": _risk_level(row.risk_level).value,
        "reason": row.request_reason,
        "requested_at": row.requested_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "decision": row.decision,
        "approver": str(row.approver_id) if row.approver_id else None,
        "timestamp": row.decision_at.isoformat() if row.decision_at else None,
        "decision_reason": row.decision_reason,
    }


def _approval_matches(
    action: RemediationActionRow, approval: RemediationApprovalRequestRow
) -> bool:
    return (
        approval.action_id == action.id
        and approval.action_type == action.action_type
        and approval.target == action.target
        and approval.risk_level == action.risk_level
    )


def _is_expired(approval: RemediationApprovalRequestRow) -> bool:
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def _expire_approval(
    session: Session,
    approval: RemediationApprovalRequestRow,
    action: RemediationActionRow,
    actor: RemediationPrincipal,
    request: Request,
    before: dict[str, object],
) -> None:
    approval.decision = "expired"
    approval.decision_at = datetime.now(UTC)
    approval.decision_reason = "Approval request expired before a decision"
    action.status = "expired"
    session.flush()
    _audit(
        session,
        actor=actor,
        request=request,
        row=action,
        action="remediation.approval_expired",
        before=before,
        after={**_action_dict(action), "approval": _approval_dict(approval)},
        outcome="expired",
    )


def _validate(
    policy: RemediationPolicyEngine,
    action_type: str,
    target: str,
    parameters: dict[str, object],
) -> ValidatedAction:
    try:
        return policy.validate(action_type, target, parameters)
    except RemediationPolicyError as exc:
        raise HTTPException(422, str(exc)) from exc


def _validate_idempotency_key(key: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,255}", key):
        raise HTTPException(422, "Idempotency-Key contains unsupported characters")


def _proposal_digest(policy_digest: str, rationale: str, incident_id: UUID) -> str:
    return hashlib.sha256(f"{incident_id}\0{policy_digest}\0{rationale}".encode()).hexdigest()


def _matches_digest(row: RemediationActionRow, validated: Any) -> bool:
    return row.action_digest == _proposal_digest(validated.digest, row.rationale, row.incident_id)


def _action_dict(row: RemediationActionRow) -> dict[str, object]:
    return {
        "action_id": str(row.id),
        "incident_id": str(row.incident_id),
        "action_type": row.action_type,
        "target": row.target,
        "parameters": row.parameters,
        "risk_level": row.risk_level,
        "requested_by": str(row.requested_by) if row.requested_by else None,
        "approved_by": str(row.approved_by) if row.approved_by else None,
        "status": row.status,
        "execution_start": row.execution_start.isoformat() if row.execution_start else None,
        "execution_end": row.execution_end.isoformat() if row.execution_end else None,
        "result": row.result,
        "rationale": row.rationale,
        "compensates_action_id": (
            str(row.compensates_action_id) if row.compensates_action_id else None
        ),
    }


def _action_response(row: RemediationActionRow) -> ActionResponse:
    if row.requested_by is None:
        raise HTTPException(409, "Legacy remediation action has no requester identity")
    return ActionResponse(
        action_id=row.id,
        incident_id=row.incident_id,
        action_type=RemediationActionType(row.action_type),
        target=row.target,
        parameters=row.parameters,
        risk_level=_risk_level(row.risk_level),
        requested_by=row.requested_by,
        approved_by=row.approved_by,
        status=row.status,
        execution_start=row.execution_start,
        execution_end=row.execution_end,
        result=row.result,
        compensates_action_id=row.compensates_action_id,
        rationale=row.rationale,
        created_at=row.created_at,
        updated_at=row.updated_at,
        approval=(
            ApprovalResponse(
                approval_request_id=row.approval_request.id,
                action_id=row.approval_request.action_id,
                action=RemediationActionType(row.approval_request.action_type),
                target=row.approval_request.target,
                risk=_risk_level(row.approval_request.risk_level),
                reason=row.approval_request.request_reason,
                requested_at=row.approval_request.requested_at,
                expires_at=row.approval_request.expires_at,
                decision=row.approval_request.decision,
                approver=row.approval_request.approver_id,
                timestamp=row.approval_request.decision_at,
                decision_reason=row.approval_request.decision_reason,
            )
            if row.approval_request is not None
            else None
        ),
    )


def _audit(
    session: Session,
    *,
    actor: RemediationPrincipal,
    request: Request,
    row: RemediationActionRow,
    action: str,
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    outcome: str = "success",
) -> None:
    session.add(
        AuditLogRow(
            tenant_id=actor.tenant_id,
            actor_type=actor.actor_type,
            actor_id=actor.subject_id,
            action=action,
            entity_type="remediation_action",
            entity_id=row.id,
            outcome=outcome,
            correlation_id=str(getattr(request.state, "correlation_id", "unknown"))[:128],
            before_state=before,
            after_state=after,
        )
    )


def _command(row: RemediationActionRow) -> RemediationCommand:
    return RemediationCommand(
        action_id=row.id,
        incident_id=row.incident_id,
        action_type=RemediationActionType(row.action_type),
        target=row.target,
        parameters=row.parameters,
        risk_level=_risk_level(row.risk_level),
    )


def _risk_level(value: str) -> RemediationRisk:
    normalized = value.casefold()
    if normalized == "critical":
        normalized = "high"
    return RemediationRisk(f"{normalized.upper()}_RISK")


def _execute_adapter(
    executor: RemediationExecutor,
    sessions: sessionmaker[Session],
    actor: RemediationPrincipal,
    action_id: UUID,
    policy: RemediationPolicyEngine,
) -> tuple[ExecutionResult, VerificationResult]:
    with sessions() as session:
        action = _get_action(session, actor.tenant_id, action_id)
        if action.compensates_action_id is None:
            command = _command(action)
            original_result = None
        else:
            original = _get_action(session, actor.tenant_id, action.compensates_action_id)
            if (
                original.status not in {"completed", "verification_failed"}
                or original.result is None
            ):
                raise RuntimeError("Original remediation is not in a rollback-eligible state")
            original_policy = _validate(
                policy,
                original.action_type,
                original.target,
                original.parameters,
            )
            if (
                original.action_digest is None
                or not _matches_digest(original, original_policy)
                or original.risk_level
                != original_policy.risk_level.value.removesuffix("_RISK").lower()
            ):
                raise RuntimeError("Original action no longer matches the remediation policy")
            command = _command(original)
            original_result = original.result
    result = (
        executor.execute(command)
        if original_result is None
        else executor.rollback(command, original_result)
    )
    verification = executor.verify(command, result)
    return result, verification


def _finish_execution(
    sessions: sessionmaker[Session],
    *,
    action_id: UUID,
    execution_id: UUID,
    actor: RemediationPrincipal,
    request: Request,
    outcome: tuple[ExecutionResult, VerificationResult] | None,
    failure_type: str | None,
    logger: Any,
    policy: RemediationPolicyEngine,
    approval_ttl_seconds: int,
) -> ExecutionResponse:
    with sessions.begin() as session:
        action = _get_action(session, actor.tenant_id, action_id, lock=True)
        execution = session.scalar(
            select(RemediationExecutionRow).where(
                RemediationExecutionRow.tenant_id == actor.tenant_id,
                RemediationExecutionRow.id == execution_id,
                RemediationExecutionRow.action_id == action_id,
            )
        )
        if execution is None:
            raise HTTPException(404, "Remediation execution was not found")
        before = _action_dict(action)
        finished = datetime.now(UTC)
        if outcome is None:
            safe_result: dict[str, object] = {
                "error": "Remediation adapter failed",
                "error_type": failure_type or "UnknownError",
            }
            execution.status = "FAILED"
            execution.result_payload = safe_result
            action.status = "failed"
            action.result = safe_result
            logger.error(
                "remediation_execution_failed",
                action_id=str(action_id),
                error_type=failure_type,
            )
            audit_action = "remediation.execution_failed"
            audit_outcome = "failure"
        else:
            execution_result, verification = outcome
            verified = verification.verified
            safe_result = {
                "execution": execution_result.result,
                "verification": {
                    "verified": verified,
                    "details": verification.details,
                },
            }
            execution.status = "COMPLETED" if verified else "VERIFICATION_FAILED"
            execution.external_operation_id = execution_result.external_operation_id
            execution.result_payload = safe_result
            action.status = "completed" if verified else "verification_failed"
            action.result = safe_result
            if verified:
                logger.info("remediation_execution_completed", action_id=str(action_id))
                audit_action = "remediation.execution_completed"
                audit_outcome = "success"
            else:
                logger.error("remediation_verification_failed", action_id=str(action_id))
                audit_action = "remediation.verification_failed"
                audit_outcome = "failure"
        execution.completed_at = finished
        action.execution_end = finished
        session.flush()
        _audit(
            session,
            actor=actor,
            request=request,
            row=action,
            action=audit_action,
            before=before,
            after={**_action_dict(action), "execution_id": str(execution.id)},
            outcome=audit_outcome,
        )
        rollback = None
        if outcome is not None and not outcome[1].verified and action.compensates_action_id is None:
            rollback = _create_rollback_approval(
                session,
                original=action,
                actor=actor,
                request=request,
                policy=policy,
                approval_ttl_seconds=approval_ttl_seconds,
                failure_details=outcome[1].details,
                execution_id=execution.id,
            )
        return _execution_response(action, execution, rollback)


def _create_rollback_approval(
    session: Session,
    *,
    original: RemediationActionRow,
    actor: RemediationPrincipal,
    request: Request,
    policy: RemediationPolicyEngine,
    approval_ttl_seconds: int,
    failure_details: dict[str, object],
    execution_id: UUID,
) -> ActionResponse:
    """Request human approval for compensation after failed post-action verification."""
    validated = _validate(policy, original.action_type, original.target, original.parameters)
    rationale = f"Verification failed for remediation {original.id}; compensation requested"
    rollback = RemediationActionRow(
        id=uuid4(),
        tenant_id=actor.tenant_id,
        incident_id=original.incident_id,
        action_type=original.action_type,
        target=original.target,
        parameters=original.parameters,
        risk_level=validated.risk_level.value.removesuffix("_RISK").lower(),
        status="approval_requested",
        idempotency_key=f"verify-rollback-{execution_id.hex}",
        action_digest=_proposal_digest(validated.digest, rationale, original.incident_id),
        requested_by=actor.subject_id,
        rationale=rationale,
        compensates_action_id=original.id,
        expires_at=datetime.now(UTC) + timedelta(seconds=approval_ttl_seconds),
    )
    session.add(rollback)
    session.flush()
    approval = RemediationApprovalRequestRow(
        id=uuid4(),
        tenant_id=actor.tenant_id,
        action_id=rollback.id,
        action_type=rollback.action_type,
        target=rollback.target,
        risk_level=rollback.risk_level,
        request_reason=rationale,
        expires_at=rollback.expires_at,
        decision="requested",
    )
    rollback.approval_request = approval
    session.add(approval)
    session.flush()
    _audit(
        session,
        actor=actor,
        request=request,
        row=rollback,
        action="remediation.rollback_approval_requested",
        before=None,
        after={
            **_action_dict(rollback),
            "approval": _approval_dict(approval),
            "triggering_execution_id": str(execution_id),
            "verification_failure": str(failure_details)[:800],
        },
    )
    return _action_response(rollback)


def _execution_response(
    action: RemediationActionRow,
    execution: RemediationExecutionRow,
    rollback_action: ActionResponse | None = None,
) -> ExecutionResponse:
    return ExecutionResponse(
        action=_action_response(action),
        execution_id=execution.id,
        execution_status=execution.status,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        result=execution.result_payload,
        rollback_action=rollback_action,
    )


app = create_app()


def _require_human(actor: RemediationPrincipal) -> None:
    if actor.actor_type != "user":
        raise HTTPException(403, "Only an authenticated human may approve or execute remediation")
