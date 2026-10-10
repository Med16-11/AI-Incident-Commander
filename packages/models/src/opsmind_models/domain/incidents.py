"""Incident state machine and domain value objects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID, uuid4


class IncidentState(StrEnum):
    """Lifecycle states for an incident."""

    DETECTED = "DETECTED"
    TRIAGING = "TRIAGING"
    INVESTIGATING = "INVESTIGATING"
    IDENTIFIED = "IDENTIFIED"
    MITIGATING = "MITIGATING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


VALID_INCIDENT_TRANSITIONS: Final = MappingProxyType(
    {
        IncidentState.DETECTED: frozenset({IncidentState.TRIAGING}),
        IncidentState.TRIAGING: frozenset({IncidentState.INVESTIGATING}),
        IncidentState.INVESTIGATING: frozenset({IncidentState.IDENTIFIED}),
        IncidentState.IDENTIFIED: frozenset(
            {IncidentState.INVESTIGATING, IncidentState.MITIGATING}
        ),
        IncidentState.MITIGATING: frozenset({IncidentState.INVESTIGATING, IncidentState.RESOLVED}),
        IncidentState.RESOLVED: frozenset({IncidentState.INVESTIGATING, IncidentState.CLOSED}),
        IncidentState.CLOSED: frozenset(),
    }
)


class InvalidIncidentTransition(ValueError):
    """Raised when a requested incident state transition is not allowed."""

    def __init__(self, current: IncidentState, requested: IncidentState) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"Incident cannot transition from {current.value} to {requested.value}")


class ConcurrentIncidentUpdate(RuntimeError):
    """Raised when an incident changed since the caller read its version."""


@dataclass(frozen=True, slots=True)
class Incident:
    """Persistence-independent incident aggregate snapshot."""

    tenant_id: UUID
    title: str
    state: IncidentState = IncidentState.DETECTED
    id: UUID = field(default_factory=uuid4)
    service_id: UUID | None = None
    description: str | None = None
    severity: str = "SEV3"
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def transition_to(
        self,
        target: IncidentState,
        *,
        at: datetime | None = None,
    ) -> Incident:
        """Return a new incident snapshot after validating and applying a transition."""
        if target not in VALID_INCIDENT_TRANSITIONS[self.state]:
            raise InvalidIncidentTransition(self.state, target)
        return replace(
            self, state=target, version=self.version + 1, updated_at=at or datetime.now(UTC)
        )
