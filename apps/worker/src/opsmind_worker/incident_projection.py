"""Project detection events into the durable incident read model."""

from __future__ import annotations

from typing import cast

import structlog
from opsmind_events.contracts import EventUnitOfWork
from opsmind_models.domain.incidents import IncidentState
from opsmind_models.events import (
    CorrelatedIncidentPayload,
    EventEnvelope,
    EventPayload,
    OpsMindEventType,
)
from opsmind_storage.models import (
    AuditLogRow,
    IncidentEventRow,
    IncidentRow,
    IncidentTimelineRow,
    ServiceRow,
)
from prometheus_client import Counter
from sqlalchemy import func, select
from sqlalchemy.orm import Session


class IncidentProjectionHandler:
    """Idempotently persist correlated incident events and their audit timeline."""

    def __init__(self, incidents_created: Counter | None = None) -> None:
        self._incidents_created = incidents_created

    def __call__(self, event: EventEnvelope[EventPayload], uow: EventUnitOfWork) -> None:
        event_type = str(event.event_type)
        if event_type not in {
            OpsMindEventType.INCIDENT_CREATED.value,
            OpsMindEventType.INCIDENT_UPDATED.value,
        }:
            return

        logger = structlog.get_logger(service="worker")
        payload = CorrelatedIncidentPayload.model_validate(event.payload.model_dump())
        session = cast(Session, uow.session)
        services = session.scalars(
            select(ServiceRow)
            .where(ServiceRow.name == payload.service)
            .order_by(ServiceRow.environment, ServiceRow.id)
        ).all()
        if event.tenant_id is not None:
            services = [service for service in services if service.tenant_id == event.tenant_id]
        tenant_ids = {service.tenant_id for service in services}
        if not tenant_ids:
            logger.warning(
                "incident_event_ignored_unregistered_service",
                event_id=str(event.event_id),
                service=payload.service,
            )
            return
        if len(tenant_ids) != 1:
            raise ValueError(
                "incident event service must resolve to exactly one registered tenant; "
                "include tenant_id on the event when service names are shared"
            )
        tenant_id = next(iter(tenant_ids))
        service_matches = [service for service in services if service.tenant_id == tenant_id]
        if len(service_matches) != 1:
            raise ValueError(
                "incident event service must resolve to exactly one registered environment"
            )
        service = service_matches[0]

        existing_event = session.get(IncidentEventRow, event.event_id)
        if existing_event is not None:
            if (
                existing_event.tenant_id != tenant_id
                or existing_event.incident_id != payload.incident_id
                or existing_event.source != event.source
            ):
                raise ValueError("event ID conflicts with a different persisted incident event")
            return

        active_duplicate = session.scalar(
            select(IncidentRow)
            .join(IncidentEventRow, IncidentEventRow.incident_id == IncidentRow.id)
            .where(
                IncidentRow.tenant_id == tenant_id,
                IncidentRow.service_id == service.id,
                IncidentRow.state.notin_(
                    [IncidentState.RESOLVED.value, IncidentState.CLOSED.value]
                ),
                IncidentEventRow.tenant_id == tenant_id,
                IncidentEventRow.source == event.source,
                IncidentEventRow.event_type.in_(
                    [
                        OpsMindEventType.INCIDENT_CREATED.value,
                        OpsMindEventType.INCIDENT_UPDATED.value,
                    ]
                ),
                IncidentEventRow.payload["signature"].as_string() == payload.signature,
            )
            .order_by(IncidentRow.created_at, IncidentRow.id)
            .limit(1)
            .with_for_update(of=IncidentRow)
        )
        incident = active_duplicate or session.scalar(
            select(IncidentRow)
            .where(IncidentRow.tenant_id == tenant_id, IncidentRow.id == payload.incident_id)
            .with_for_update()
        )
        created = incident is None
        severity = "SEV2" if payload.score >= 85 else "SEV3"
        if incident is None:
            incident = IncidentRow(
                id=payload.incident_id,
                tenant_id=tenant_id,
                service_id=service.id,
                title=f"{payload.service} anomaly detected",
                description=payload.summary,
                state="DETECTED",
                severity=severity,
                detected_at=payload.first_seen_at,
            )
            session.add(incident)
            session.flush()
        else:
            before = {"state": incident.state, "severity": incident.severity}
            incident.description = payload.summary
            if severity == "SEV2":
                incident.severity = severity
            session.flush()
            session.add(
                AuditLogRow(
                    tenant_id=tenant_id,
                    actor_type="service",
                    actor_id=None,
                    action="incident.updated_from_detection",
                    entity_type="incident",
                    entity_id=incident.id,
                    outcome="success",
                    correlation_id=event.correlation_id,
                    before_state=before,
                    after_state={"state": incident.state, "severity": incident.severity},
                )
            )

        incident_event = IncidentEventRow(
            id=event.event_id,
            tenant_id=tenant_id,
            incident_id=incident.id,
            event_type=event_type,
            source=event.source,
            idempotency_key=str(event.event_id),
            correlation_id=event.correlation_id,
            payload=payload.model_dump(mode="json"),
            occurred_at=event.timestamp,
        )
        session.add(incident_event)
        session.flush()
        sequence = int(
            session.scalar(
                select(func.coalesce(func.max(IncidentTimelineRow.sequence), 0)).where(
                    IncidentTimelineRow.tenant_id == tenant_id,
                    IncidentTimelineRow.incident_id == incident.id,
                )
            )
            or 0
        )
        session.add(
            IncidentTimelineRow(
                tenant_id=tenant_id,
                incident_id=incident.id,
                event_id=event.event_id,
                sequence=sequence + 1,
                event_type="incident.detected" if created else "incident.updated",
                summary=payload.summary,
                occurred_at=event.timestamp,
            )
        )
        if created:
            session.add(
                AuditLogRow(
                    tenant_id=tenant_id,
                    actor_type="service",
                    actor_id=None,
                    action="incident.created_from_detection",
                    entity_type="incident",
                    entity_id=incident.id,
                    outcome="success",
                    correlation_id=event.correlation_id,
                    before_state=None,
                    after_state={
                        "state": incident.state,
                        "severity": incident.severity,
                        "source_event": str(event.event_id),
                    },
                )
            )
            if self._incidents_created is not None:
                self._incidents_created.inc()
