"""Allowlisted remediation policy and typed parameter validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import cast

from opsmind_models.domain.remediation import (
    ActionParameters,
    ClearCacheParameters,
    DisableFeatureFlagParameters,
    RemediationActionType,
    RemediationParameters,
    RemediationRisk,
    RestartServiceParameters,
    RollbackDeploymentParameters,
    ScaleServiceParameters,
)
from pydantic import ValidationError

_RISK_BY_ACTION: dict[str, RemediationRisk] = {
    RemediationActionType.RESTART_SERVICE: RemediationRisk.LOW_RISK,
    RemediationActionType.CLEAR_CACHE: RemediationRisk.LOW_RISK,
    RemediationActionType.SCALE_SERVICE: RemediationRisk.MEDIUM_RISK,
    RemediationActionType.ROLLBACK_DEPLOYMENT: RemediationRisk.MEDIUM_RISK,
    RemediationActionType.DISABLE_FEATURE_FLAG: RemediationRisk.HIGH_RISK,
    # Classified for policy reporting only. It is not in the executable allowlist.
    "database_modification": RemediationRisk.HIGH_RISK,
}
_PARAMETER_MODEL: dict[RemediationActionType, type[ActionParameters]] = {
    RemediationActionType.RESTART_SERVICE: RestartServiceParameters,
    RemediationActionType.ROLLBACK_DEPLOYMENT: RollbackDeploymentParameters,
    RemediationActionType.SCALE_SERVICE: ScaleServiceParameters,
    RemediationActionType.DISABLE_FEATURE_FLAG: DisableFeatureFlagParameters,
    RemediationActionType.CLEAR_CACHE: ClearCacheParameters,
}


class RemediationPolicyError(ValueError):
    """Action violates the executable catalog or policy rules."""


@dataclass(frozen=True, slots=True)
class ValidatedAction:
    action_type: RemediationActionType
    risk_level: RemediationRisk
    parameters: RemediationParameters
    digest: str


class RemediationPolicyEngine:
    """Derive risk, validate parameters, and gate execution without model authority."""

    def classify(self, action_type: str) -> RemediationRisk:
        """Return the policy risk class, including non-executable high-risk actions."""
        try:
            return _RISK_BY_ACTION[action_type]
        except KeyError as exc:
            raise RemediationPolicyError(
                "Action type is not classified by remediation policy"
            ) from exc

    def validate(
        self,
        action_type: str,
        target: str,
        parameters: dict[str, object],
    ) -> ValidatedAction:
        """Validate one allowlisted action and calculate its immutable proposal digest."""
        try:
            typed_action = RemediationActionType(action_type)
            model = _PARAMETER_MODEL[typed_action]
        except (ValueError, KeyError) as exc:
            raise RemediationPolicyError("Action type is not executable") from exc
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}", target):
            raise RemediationPolicyError("Action target must be a simple resource identifier")
        try:
            typed_parameters = model.model_validate_json(json.dumps(parameters, default=str))
        except ValidationError as exc:
            raise RemediationPolicyError("Action parameters failed policy validation") from exc
        canonical = {
            "action_type": typed_action.value,
            "target": target,
            "parameters": typed_parameters.model_dump(mode="json"),
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ValidatedAction(
            action_type=typed_action,
            risk_level=self.classify(typed_action.value),
            parameters=cast(RemediationParameters, typed_parameters),
            digest=digest,
        )
