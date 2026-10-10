"""Repository ports used by application logic without persistence dependencies."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from opsmind_models.domain.events import IncidentEventData
from opsmind_models.domain.incidents import Incident, IncidentState


class Repository[EntityT, IdentifierT](Protocol):
    """Tenant-scoped repository contract independent of persistence technology."""

    def add(self, entity: EntityT) -> None: ...

    def get(self, tenant_id: UUID, entity_id: IdentifierT) -> EntityT | None: ...


class IncidentRepository(Repository[Incident, UUID], Protocol):
    """Persistence-independent operations for incident aggregates and events."""

    def transition(
        self,
        tenant_id: UUID,
        incident_id: UUID,
        target: IncidentState,
        *,
        expected_version: int,
        at: datetime | None = None,
    ) -> Incident: ...

    def add_event_once(self, event: IncidentEventData) -> tuple[IncidentEventData, bool]: ...


class IncidentEventRepository(Protocol):
    """Port for recording incident events with external-event idempotency."""

    def add_once(self, event: IncidentEventData) -> tuple[IncidentEventData, bool]: ...
