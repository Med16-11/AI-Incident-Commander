"""Typed remediation action contracts and policy classifications."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RemediationActionType(StrEnum):
    RESTART_SERVICE = "restart_service"
    ROLLBACK_DEPLOYMENT = "rollback_deployment"
    SCALE_SERVICE = "scale_service"
    DISABLE_FEATURE_FLAG = "disable_feature_flag"
    CLEAR_CACHE = "clear_cache"


class RemediationRisk(StrEnum):
    LOW_RISK = "LOW_RISK"
    MEDIUM_RISK = "MEDIUM_RISK"
    HIGH_RISK = "HIGH_RISK"


class ActionParameters(BaseModel):
    """Strict allowlisted parameters; action-specific rules are applied by policy."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RestartServiceParameters(ActionParameters):
    grace_period_seconds: Annotated[int, Field(ge=0, le=300)] = 30


class RollbackDeploymentParameters(ActionParameters):
    deployment_id: UUID
    target_version: Annotated[str, Field(min_length=1, max_length=255)]


class ScaleServiceParameters(ActionParameters):
    replicas: Annotated[int, Field(ge=1, le=100)]


class DisableFeatureFlagParameters(ActionParameters):
    flag_key: Annotated[str, Field(min_length=1, max_length=200)]
    environment: Annotated[str, Field(min_length=1, max_length=100)]


class ClearCacheParameters(ActionParameters):
    cache_name: Annotated[str, Field(min_length=1, max_length=100)]
    key_prefix: Annotated[str | None, Field(max_length=200)] = None


RemediationParameters = (
    RestartServiceParameters
    | RollbackDeploymentParameters
    | ScaleServiceParameters
    | DisableFeatureFlagParameters
    | ClearCacheParameters
)


class RemediationProposal(BaseModel):
    """AI or operator proposal; never includes approval or execution authority."""

    model_config = ConfigDict(extra="forbid", strict=True)

    incident_id: UUID
    action_type: RemediationActionType
    target: Annotated[str, Field(min_length=1, max_length=1000)]
    parameters: dict[str, object]
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    compensates_action_id: UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> RemediationProposal:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}", self.target):
            raise ValueError("target must be a simple resource identifier")
        return self


class RemediationAction(BaseModel):
    """Action state visible in APIs and audit records."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    action_id: UUID
    incident_id: UUID
    action_type: RemediationActionType
    target: str
    parameters: dict[str, object]
    risk_level: RemediationRisk
    requested_by: UUID
    approved_by: UUID | None
    status: str
    execution_start: datetime | None
    execution_end: datetime | None
    result: dict[str, object] | None
    compensates_action_id: UUID | None
    rationale: str
    created_at: datetime
    updated_at: datetime
