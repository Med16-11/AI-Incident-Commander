"""Transactional audit and incident timeline helpers."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opsmind_storage.models import AuditLogRow, IncidentRow, IncidentTimelineRow
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def add_audit(
    session: Session,
    *,
    tenant_id: UUID,
    actor_id: UUID,
    action: str,
    entity_id: UUID,
    correlation_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    entity_type: str = "incident",
) -> None:
    """Stage an append-only audit row in the mutation transaction."""
    session.add(
        AuditLogRow(
            tenant_id=tenant_id,
            actor_type="user",
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome="success",
            correlation_id=correlation_id,
            before_state=before,
            after_state=after,
        )
    )


def add_timeline(
    session: Session,
    *,
    tenant_id: UUID,
    incident_id: UUID,
    event_type: str,
    summary: str,
    actor_id: UUID,
    event_id: UUID | None = None,
    occurred_at: datetime | None = None,
) -> None:
    """Append a monotonically sequenced timeline entry."""
    # Serialize sequence allocation on PostgreSQL; uniqueness remains a final guard.
    session.scalar(
        select(IncidentRow.id)
        .where(IncidentRow.tenant_id == tenant_id, IncidentRow.id == incident_id)
        .with_for_update()
    )
    sequence = session.scalar(
        select(func.coalesce(func.max(IncidentTimelineRow.sequence), 0)).where(
            IncidentTimelineRow.incident_id == incident_id
        )
    )
    session.add(
        IncidentTimelineRow(
            tenant_id=tenant_id,
            incident_id=incident_id,
            event_id=event_id,
            sequence=int(sequence or 0) + 1,
            event_type=event_type,
            summary=summary,
            actor_id=actor_id,
            occurred_at=occurred_at or datetime.now(UTC),
        )
    )
