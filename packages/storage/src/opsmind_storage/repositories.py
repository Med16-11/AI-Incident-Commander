"""SQLAlchemy implementations of persistence-independent repository ports."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from opsmind_models.domain.events import IncidentEventData
from opsmind_models.domain.incidents import (
    ConcurrentIncidentUpdate,
    Incident,
    IncidentState,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from opsmind_storage.models import IncidentEventRow, IncidentRow


class IncidentNotFound(LookupError):
    """Raised when an incident does not exist in the requested tenant scope."""


class IdempotencyConflict(RuntimeError):
    """Raised when a previously seen external key is reused for different event data."""


class SqlAlchemyIncidentRepository:
    """Persistence adapter for incident transitions and idempotent event ingestion."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, incident: Incident) -> None:
        """Stage a new incident in the caller-owned transaction."""
        self._session.add(
            IncidentRow(
                id=incident.id,
                tenant_id=incident.tenant_id,
                service_id=incident.service_id,
                title=incident.title,
                description=incident.description,
                state=incident.state.value,
                severity=incident.severity,
                version=incident.version,
                created_at=incident.created_at,
                updated_at=incident.updated_at,
            )
        )
        self._session.flush()

    def get(self, tenant_id: UUID, incident_id: UUID) -> Incident | None:
        """Load an incident only when it belongs to the supplied tenant."""
        row = self._session.scalar(
            select(IncidentRow).where(
                IncidentRow.tenant_id == tenant_id,
                IncidentRow.id == incident_id,
            )
        )
        return self._to_domain(row) if row is not None else None

    def transition(
        self,
        tenant_id: UUID,
        incident_id: UUID,
        target: IncidentState,
        *,
        expected_version: int,
        at: datetime | None = None,
    ) -> Incident:
        """Validate and persist a state change using optimistic concurrency."""
        row = self._session.scalar(
            select(IncidentRow).where(
                IncidentRow.tenant_id == tenant_id,
                IncidentRow.id == incident_id,
            )
        )
        if row is None:
            raise IncidentNotFound(f"Incident {incident_id} was not found in tenant {tenant_id}")
        if row.version != expected_version:
            raise ConcurrentIncidentUpdate(
                f"Incident {incident_id} version is {row.version}, expected {expected_version}"
            )

        updated = self._to_domain(row).transition_to(target, at=at)
        previous_state = row.state
        row.state = updated.state.value
        row.updated_at = updated.updated_at
        if target is IncidentState.RESOLVED:
            row.resolved_at = updated.updated_at
        elif (
            target is IncidentState.INVESTIGATING and previous_state == IncidentState.RESOLVED.value
        ):
            row.resolved_at = None
        elif target is IncidentState.CLOSED:
            row.closed_at = updated.updated_at

        try:
            self._session.flush()
        except StaleDataError as exc:
            self._session.rollback()
            raise ConcurrentIncidentUpdate(
                f"Incident {incident_id} was concurrently updated"
            ) from exc
        return self._to_domain(row)

    def add_event_once(self, event: IncidentEventData) -> tuple[IncidentEventData, bool]:
        """Insert an event once, deduplicating external deliveries in tenant/source scope."""
        parent = self._session.scalar(
            select(IncidentRow.id).where(
                IncidentRow.tenant_id == event.tenant_id,
                IncidentRow.id == event.incident_id,
            )
        )
        if parent is None:
            raise IncidentNotFound(
                f"Incident {event.incident_id} was not found in tenant {event.tenant_id}"
            )

        if event.idempotency_key is not None:
            existing = self._find_event(event.tenant_id, event.source, event.idempotency_key)
            if existing is not None:
                return self._existing_event(existing, event), False

        row = IncidentEventRow(
            id=event.id,
            tenant_id=event.tenant_id,
            incident_id=event.incident_id,
            event_type=event.event_type,
            source=event.source,
            idempotency_key=event.idempotency_key,
            correlation_id=event.correlation_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        try:
            if event.idempotency_key is None:
                self._session.add(row)
                self._session.flush()
            else:
                with self._session.begin_nested():
                    self._session.add(row)
                    self._session.flush()
        except IntegrityError:
            if event.idempotency_key is None:
                raise
            existing = self._find_event(event.tenant_id, event.source, event.idempotency_key)
            if existing is None:
                raise
            return self._existing_event(existing, event), False
        return event, True

    def add_once(self, event: IncidentEventData) -> tuple[IncidentEventData, bool]:
        """Satisfy the dedicated incident-event repository port."""
        return self.add_event_once(event)

    def _find_event(
        self,
        tenant_id: UUID,
        source: str,
        idempotency_key: str,
    ) -> IncidentEventRow | None:
        return self._session.scalar(
            select(IncidentEventRow).where(
                IncidentEventRow.tenant_id == tenant_id,
                IncidentEventRow.source == source,
                IncidentEventRow.idempotency_key == idempotency_key,
            )
        )

    @classmethod
    def _existing_event(
        cls,
        existing: IncidentEventRow,
        requested: IncidentEventData,
    ) -> IncidentEventData:
        """Return a replayed event only when its idempotency identity is unchanged."""
        if (
            existing.incident_id != requested.incident_id
            or existing.event_type != requested.event_type
            or existing.payload != requested.payload
        ):
            raise IdempotencyConflict(
                "An idempotency key was reused with different incident event data"
            )
        return cls._event_to_domain(existing)

    @staticmethod
    def _to_domain(row: IncidentRow) -> Incident:
        return Incident(
            id=row.id,
            tenant_id=row.tenant_id,
            service_id=row.service_id,
            title=row.title,
            description=row.description,
            state=IncidentState(row.state),
            severity=row.severity,
            version=row.version,
            created_at=SqlAlchemyIncidentRepository._as_utc(row.created_at),
            updated_at=SqlAlchemyIncidentRepository._as_utc(row.updated_at),
        )

    @staticmethod
    def _event_to_domain(row: IncidentEventRow) -> IncidentEventData:
        return IncidentEventData(
            id=row.id,
            tenant_id=row.tenant_id,
            incident_id=row.incident_id,
            event_type=row.event_type,
            source=row.source,
            idempotency_key=row.idempotency_key,
            correlation_id=row.correlation_id,
            payload=cast(dict[str, object], row.payload),
            occurred_at=SqlAlchemyIncidentRepository._as_utc(row.occurred_at),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
