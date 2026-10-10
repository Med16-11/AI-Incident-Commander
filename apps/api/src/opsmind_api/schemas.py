"""Validated HTTP request and response models for incident management."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from opsmind_models.domain.incidents import IncidentState
from opsmind_models.domain.remediation import RemediationRisk
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    environment: str = Field(min_length=1, max_length=100)
    owner_team: str | None = Field(default=None, max_length=200)
    repository_url: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_names(self) -> ServiceCreate:
        if not self.name.strip() or not self.environment.strip():
            raise ValueError("service name and environment cannot be blank")
        return self


class ServiceResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    environment: str
    owner_team: str | None
    repository_url: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ServiceDependencyCreate(ApiModel):
    target_service_id: UUID
    dependency_type: str = Field(default="runtime", min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_dependency_type(self) -> ServiceDependencyCreate:
        if not self.dependency_type.strip():
            raise ValueError("dependency_type cannot be blank")
        return self


class ServiceDependencyResponse(BaseModel):
    id: UUID
    source_service_id: UUID
    target_service_id: UUID
    dependency_type: str
    valid_from: datetime


class ServiceTopologyNode(BaseModel):
    id: UUID
    name: str
    environment: str


class ServiceDependenciesResponse(BaseModel):
    service: ServiceTopologyNode
    direct_dependencies: list[ServiceTopologyNode]
    upstream_services: list[ServiceTopologyNode]
    downstream_services: list[ServiceTopologyNode]
    upstream_depth: int
    downstream_depth: int
    critical_path: list[ServiceTopologyNode]


class ServiceBlastRadiusResponse(BaseModel):
    affected_service: ServiceTopologyNode
    upstream_impact: list[ServiceTopologyNode]
    downstream_impact: list[ServiceTopologyNode]
    likely_blast_radius: list[ServiceTopologyNode]
    likely_blast_radius_count: int
    upstream_depth: int
    downstream_depth: int
    critical_path: list[ServiceTopologyNode]


class IncidentCreate(ApiModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    service_id: UUID | None = None
    severity: str = Field(default="SEV3", pattern=r"^SEV[1-4]$")

    @model_validator(mode="after")
    def nonblank_title(self) -> IncidentCreate:
        if not self.title.strip():
            raise ValueError("title cannot be blank")
        return self


class IncidentPatch(ApiModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=20_000)
    service_id: UUID | None = None
    severity: str | None = Field(default=None, pattern=r"^SEV[1-4]$")
    state: IncidentState | None = None

    @model_validator(mode="after")
    def requires_change(self) -> IncidentPatch:
        if not self.model_fields_set - {"expected_version"}:
            raise ValueError("at least one incident field must be provided")
        if self.title is not None and not self.title.strip():
            raise ValueError("title cannot be blank")
        return self


class IncidentClose(ApiModel):
    expected_version: int = Field(ge=1)


class IncidentEventCreate(ApiModel):
    event_type: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)


class IncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    service_id: UUID | None
    title: str
    description: str | None
    state: IncidentState
    severity: str
    version: int
    created_at: datetime
    updated_at: datetime


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


class IncidentEventResponse(BaseModel):
    id: UUID
    incident_id: UUID
    event_type: str
    source: str
    idempotency_key: str | None
    correlation_id: str
    payload: dict[str, Any]
    occurred_at: datetime


class RecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TimelineResponse(RecordResponse):
    id: UUID
    incident_id: UUID
    event_id: UUID | None
    evidence_id: UUID | None
    sequence: int
    event_type: str
    summary: str
    actor_id: UUID | None
    occurred_at: datetime


class EvidenceResponse(RecordResponse):
    id: UUID
    incident_id: UUID
    evidence_type: str
    source: str
    service: str
    normalized_summary: str
    confidence: float
    external_ref: str | None
    content: dict[str, Any]
    content_hash: str | None
    observed_at: datetime
    collected_at: datetime


class HypothesisResponse(RecordResponse):
    id: UUID
    incident_id: UUID
    summary: str
    confidence: float
    status: str
    rationale: str | None
    created_at: datetime
    updated_at: datetime


class RemediationResponse(RecordResponse):
    id: UUID
    incident_id: UUID
    action_type: str
    target: str
    parameters: dict[str, Any]
    risk_level: RemediationRisk
    status: str
    requested_by: UUID | None = None
    approved_by: UUID | None
    approved_at: datetime | None
    execution_start: datetime | None = None
    execution_end: datetime | None = None
    result: dict[str, Any] | None = None
    rationale: str = ""
    compensates_action_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("risk_level", mode="before")
    @classmethod
    def normalize_risk_level(cls, value: object) -> object:
        if isinstance(value, str) and value.casefold() in {
            "low",
            "medium",
            "high",
            "critical",
        }:
            risk = "high" if value.casefold() == "critical" else value
            return f"{risk.upper()}_RISK"
        return value


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    correlation_id: str
    details: Any | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorDetail
