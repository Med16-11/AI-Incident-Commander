"""SQLAlchemy table mappings for the OpsMind incident domain."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Metadata root for OpsMind persistence models."""


class TimestampMixin:
    """UTC creation and update timestamps for mutable rows."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


incident_hypothesis_evidence = Table(
    "incident_hypothesis_evidence",
    Base.metadata,
    Column(
        "hypothesis_id",
        Uuid(as_uuid=True),
        ForeignKey("incident_hypotheses.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "evidence_id",
        Uuid(as_uuid=True),
        ForeignKey("incident_evidence.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ServiceRow(TimestampMixin, Base):
    """A monitored service within a tenant and environment."""

    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("tenant_id", "environment", "name", name="uq_services_scope_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    environment: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_team: Mapped[str | None] = mapped_column(String(200))
    repository_url: Mapped[str | None] = mapped_column(String(1000))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_DOCUMENT, nullable=False, default=dict
    )


class IncidentRow(TimestampMixin, Base):
    """Incident aggregate row with SQLAlchemy optimistic version checking."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(
            "state IN ('DETECTED','TRIAGING','INVESTIGATING','IDENTIFIED',"
            "'MITIGATING','RESOLVED','CLOSED')",
            name="ck_incidents_state",
        ),
        CheckConstraint("severity IN ('SEV1','SEV2','SEV3','SEV4')", name="ck_incidents_severity"),
        CheckConstraint("version >= 1", name="ck_incidents_version_positive"),
        Index("ix_incidents_tenant_state_updated", "tenant_id", "state", "updated_at"),
        Index("ix_incidents_tenant_created", "tenant_id", "created_at"),
        Index("ix_incidents_service_state", "service_id", "state"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    service_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="DETECTED")
    severity: Mapped[str] = mapped_column(String(10), nullable=False, default="SEV3")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    __mapper_args__ = {"version_id_col": version}
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_to: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class ServiceDependencyRow(TimestampMixin, Base):
    """Directed dependency edge between services."""

    __tablename__ = "service_dependencies"
    __table_args__ = (
        CheckConstraint("source_service_id <> target_service_id", name="ck_dependency_not_self"),
        UniqueConstraint(
            "tenant_id", "source_service_id", "target_service_id", name="uq_service_dependency_edge"
        ),
        Index("ix_dependencies_target", "tenant_id", "target_service_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_service_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    target_service_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    dependency_type: Mapped[str] = mapped_column(String(100), nullable=False, default="runtime")
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IncidentEventRow(Base):
    """Append-only incident event record with scoped external idempotency."""

    __tablename__ = "incident_events"
    __table_args__ = (
        CheckConstraint(
            "source = 'internal' OR idempotency_key IS NOT NULL",
            name="ck_external_event_has_idempotency_key",
        ),
        CheckConstraint(
            "length(correlation_id) BETWEEN 1 AND 128", name="ck_incident_event_correlation_length"
        ),
        UniqueConstraint(
            "tenant_id", "source", "idempotency_key", name="uq_incident_event_idempotency"
        ),
        Index("ix_incident_events_incident_time", "incident_id", "occurred_at"),
        Index("ix_incident_events_tenant_time", "tenant_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    source: Mapped[str] = mapped_column(String(160), nullable=False, default="internal")
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IncidentEvidenceRow(Base):
    """Source evidence captured during an incident investigation."""

    __tablename__ = "incident_evidence"
    __table_args__ = (
        Index("ix_evidence_incident_observed", "incident_id", "observed_at"),
        Index("ix_evidence_tenant_collected", "tenant_id", "collected_at"),
        UniqueConstraint(
            "tenant_id", "incident_id", "content_hash", name="uq_evidence_incident_content_hash"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    evidence_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(160), nullable=False)
    service: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(2000))
    content: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IncidentHypothesisRow(TimestampMixin, Base):
    """Evidence-linked root-cause hypothesis."""

    __tablename__ = "incident_hypotheses"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_hypothesis_confidence"),
        Index("ix_hypotheses_incident_created", "incident_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="proposed")
    rationale: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[IncidentEvidenceRow]] = relationship(
        secondary=incident_hypothesis_evidence, lazy="selectin"
    )


class InvestigationRunRow(Base):
    """Durable state for one bounded agent investigation execution."""

    __tablename__ = "investigation_runs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('INITIAL_TRIAGE','HYPOTHESIS_FORMED','EVIDENCE_COLLECTION',"
            "'HYPOTHESIS_UPDATED','ROOT_CAUSE_IDENTIFIED','REMEDIATION_PROPOSED')",
            name="ck_investigation_run_state",
        ),
        CheckConstraint(
            "status IN ('RUNNING','COMPLETED','FAILED','TIMED_OUT')",
            name="ck_investigation_run_status",
        ),
        CheckConstraint("version >= 1", name="ck_investigation_run_version_positive"),
        CheckConstraint("tool_calls >= 0", name="ck_investigation_run_tool_calls_nonnegative"),
        Index("ix_investigation_runs_incident_created", "tenant_id", "incident_id", "started_at"),
        Index("ix_investigation_runs_status_updated", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="INITIAL_TRIAGE")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RUNNING")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    __mapper_args__ = {"version_id_col": version}
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    state_data: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InvestigationRunStepRow(Base):
    """Append-only trace record for a provider, tool, or state-machine step."""

    __tablename__ = "investigation_run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_investigation_run_step_sequence"),
        Index("ix_investigation_steps_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HistoricalIncidentRow(TimestampMixin, Base):
    """Resolved incident snapshot indexed for tenant-scoped similarity retrieval."""

    __tablename__ = "historical_incidents"
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "incident_id", name="uq_historical_incident_scope"),
        CheckConstraint("resolution_seconds >= 0", name="ck_historical_resolution_nonnegative"),
        Index("ix_historical_tenant_resolved", "tenant_id", "resolved_at"),
        Index("ix_historical_tenant_service", "tenant_id", "service_id"),
        Index(
            "ix_historical_incident_search",
            func.to_tsvector("english", search_text),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL")
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    symptoms: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    affected_services: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    remediation: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)


class DeploymentRow(Base):
    """Deployment/change record correlated with a service incident."""

    __tablename__ = "deployments"
    __table_args__ = (
        Index("ix_deployments_service_started", "service_id", "started_at"),
        Index(
            "ix_deployments_tenant_environment_started", "tenant_id", "environment", "started_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    service_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    environment: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(128))
    repository_url: Mapped[str | None] = mapped_column(String(1000))
    deployed_by: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RemediationActionRow(TimestampMixin, Base):
    """Proposed remediation operation, separate from any execution attempt."""

    __tablename__ = "remediation_actions"
    __table_args__ = (
        CheckConstraint("risk_level IN ('low','medium','high','critical')", name="ck_action_risk"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_action_idempotency"),
        Index("ix_actions_incident_status", "incident_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    __mapper_args__ = {"version_id_col": version}
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(160), nullable=False)
    target: Mapped[str] = mapped_column(String(1000), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="proposed")
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    action_digest: Mapped[str | None] = mapped_column(String(128))
    approved_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    execution_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    compensates_action_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("remediation_actions.id", ondelete="SET NULL")
    )
    approval_request: Mapped[RemediationApprovalRequestRow | None] = relationship(
        back_populates="action", uselist=False
    )


class RemediationApprovalRequestRow(Base):
    """Approval request plus its single immutable decision snapshot."""

    __tablename__ = "remediation_approval_requests"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_remediation_approval_action"),
        CheckConstraint(
            "decision IN ('requested','approved','rejected','expired')",
            name="ck_remediation_approval_decision",
        ),
        Index("ix_approval_requests_tenant_expiry", "tenant_id", "decision", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("remediation_actions.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(160), nullable=False)
    target: Mapped[str] = mapped_column(String(1000), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    request_reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False, default="requested")
    approver_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    action: Mapped[RemediationActionRow] = relationship(back_populates="approval_request")


class RemediationExecutionRow(Base):
    """Attempt and outcome for a remediation action."""

    __tablename__ = "remediation_executions"
    __table_args__ = (
        UniqueConstraint("action_id", "idempotency_key", name="uq_execution_idempotency"),
        Index("ix_executions_tenant_started", "tenant_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("remediation_actions.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    external_operation_id: Mapped[str | None] = mapped_column(String(500))
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationRow(Base):
    """Outbound notification request and delivery status."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_notification_idempotency"),
        Index("ix_notifications_incident_created", "incident_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(100), nullable=False)
    recipient: Mapped[str] = mapped_column(String(500), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    external_message_id: Mapped[str | None] = mapped_column(String(500))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLogRow(Base):
    """Append-only audit record; mutation is excluded from repository interfaces."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "length(correlation_id) BETWEEN 1 AND 128", name="ck_audit_correlation_length"
        ),
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_entity", "tenant_id", "entity_type", "entity_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MetricSnapshotRow(Base):
    """Time-indexed metric sample associated with a service or incident."""

    __tablename__ = "metric_snapshots"
    __table_args__ = (
        Index("ix_metrics_tenant_observed", "tenant_id", "observed_at"),
        Index("ix_metrics_service_name_observed", "service_id", "metric_name", "observed_at"),
        Index("ix_metrics_incident_observed", "incident_id", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    service_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL")
    )
    incident_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="SET NULL")
    )
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(160), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LogRecordRow(Base):
    """Normalized, time-indexed log excerpt associated with a service or incident."""

    __tablename__ = "log_records"
    __table_args__ = (
        Index("ix_logs_tenant_observed", "tenant_id", "observed_at"),
        Index("ix_logs_service_observed", "service_id", "observed_at"),
        Index("ix_logs_incident_observed", "incident_id", "observed_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    service_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL")
    )
    incident_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(160), nullable=False)
    severity: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    external_ref: Mapped[str | None] = mapped_column(String(2000))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IncidentTimelineRow(Base):
    """Ordered incident activity projection for responder timelines."""

    __tablename__ = "incident_timeline"
    __table_args__ = (
        UniqueConstraint("incident_id", "sequence", name="uq_timeline_incident_sequence"),
        Index("ix_timeline_incident_time", "incident_id", "occurred_at"),
        Index("ix_timeline_tenant_time", "tenant_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident_events.id", ondelete="SET NULL"), unique=True
    )
    evidence_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incident_evidence.id", ondelete="SET NULL"), unique=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProcessedEventRow(Base):
    """Transactional inbox key, scoped per consumer group for at-least-once delivery."""

    __tablename__ = "processed_events"
    __table_args__ = (Index("ix_processed_events_created", "processed_at"),)

    consumer_group: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IncidentCreateIdempotencyRow(Base):
    """Idempotency receipt for tenant-scoped incident creation requests."""

    __tablename__ = "incident_create_idempotency"
    __table_args__ = (Index("ix_incident_create_idempotency_incident", "incident_id"),)

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
