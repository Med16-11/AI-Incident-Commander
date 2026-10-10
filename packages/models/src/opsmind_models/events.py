"""Versioned event envelope shared by producers and consumers."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class OpsMindEventType(StrEnum):
    """Stable event names exchanged between OpsMind services."""

    METRIC_ANOMALY_DETECTED = "MetricAnomalyDetected"
    LOG_ANOMALY_DETECTED = "LogAnomalyDetected"
    INCIDENT_CREATED = "IncidentCreated"
    INCIDENT_UPDATED = "IncidentUpdated"
    INVESTIGATION_STARTED = "InvestigationStarted"
    EVIDENCE_COLLECTED = "EvidenceCollected"
    HYPOTHESIS_GENERATED = "HypothesisGenerated"
    REMEDIATION_PROPOSED = "RemediationProposed"
    REMEDIATION_APPROVED = "RemediationApproved"
    REMEDIATION_STARTED = "RemediationStarted"
    REMEDIATION_COMPLETED = "RemediationCompleted"
    REMEDIATION_FAILED = "RemediationFailed"
    INCIDENT_RESOLVED = "IncidentResolved"
    DEPLOYMENT_STARTED = "DeploymentStarted"
    CONFIGURATION_CHANGED = "ConfigurationChanged"
    POD_RESTARTED = "PodRestarted"
    INFRASTRUCTURE_EVENT = "InfrastructureEvent"


class EventPayload(BaseModel):
    """Forward-compatible decoded payload; producers should use specific models."""

    model_config = ConfigDict(extra="allow")


class CorrelatedIncidentPayload(BaseModel):
    """Detection-originated correlated incident projection contract."""

    incident_id: UUID
    signature: str
    service: str
    score: int = Field(ge=0, le=100)
    anomaly_ids: list[UUID]
    first_seen_at: datetime
    last_seen_at: datetime
    summary: str


class EventEnvelope[PayloadT: BaseModel](BaseModel):
    """Common event metadata; event producers must propagate correlation IDs."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    event_type: OpsMindEventType | str
    schema_version: int = Field(default=1, ge=1)
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        validation_alias=AliasChoices("timestamp", "occurred_at"),
    )
    correlation_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    causation_id: UUID | None = None
    source: str = Field(min_length=1, max_length=160)
    tenant_id: UUID | None = None
    aggregate_type: str | None = None
    aggregate_id: UUID | None = None
    aggregate_version: int | None = Field(default=None, ge=1)
    payload: PayloadT

    @property
    def occurred_at(self) -> datetime:
        """Compatibility alias for domain code using the earlier field name."""

        return self.timestamp
