"""Infrastructure adapter interface; the default executor always fails closed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from opsmind_models.domain.remediation import RemediationActionType, RemediationRisk


@dataclass(frozen=True, slots=True)
class RemediationCommand:
    action_id: UUID
    incident_id: UUID
    action_type: RemediationActionType
    target: str
    parameters: dict[str, object]
    risk_level: RemediationRisk


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    result: dict[str, object]
    external_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Observable postcondition result for the requested remediation."""

    verified: bool
    details: dict[str, object]


class RemediationExecutor(Protocol):
    """Infrastructure boundary for Kubernetes, GitLab, feature flags, and caches."""

    def execute(self, command: RemediationCommand) -> ExecutionResult: ...

    def rollback(
        self, command: RemediationCommand, previous_result: dict[str, object]
    ) -> ExecutionResult: ...

    def verify(
        self, command: RemediationCommand, result: ExecutionResult
    ) -> VerificationResult: ...


class UnavailableRemediationExecutor:
    """Safe default until a deployment injects explicitly configured adapters."""

    def execute(self, command: RemediationCommand) -> ExecutionResult:
        del command
        raise RuntimeError("No remediation infrastructure adapter is configured")

    def rollback(
        self, command: RemediationCommand, previous_result: dict[str, object]
    ) -> ExecutionResult:
        del command, previous_result
        raise RuntimeError("No remediation rollback adapter is configured")

    def verify(self, command: RemediationCommand, result: ExecutionResult) -> VerificationResult:
        del command, result
        raise RuntimeError("No remediation verification adapter is configured")
