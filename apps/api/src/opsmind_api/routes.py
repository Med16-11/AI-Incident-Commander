"""Incident management HTTP routes."""

# FastAPI uses Depends in endpoint signatures as declarative dependency metadata.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from opsmind_models.domain.events import IncidentEventData
from opsmind_models.domain.incidents import (
    ConcurrentIncidentUpdate,
    Incident,
    IncidentState,
    InvalidIncidentTransition,
)
from opsmind_storage.models import (
    HistoricalIncidentRow,
    IncidentCreateIdempotencyRow,
    IncidentEventRow,
    IncidentEvidenceRow,
    IncidentHypothesisRow,
    IncidentRow,
    IncidentTimelineRow,
    RemediationActionRow,
    ServiceRow,
)
from opsmind_storage.repositories import (
    IdempotencyConflict,
    IncidentNotFound,
    SqlAlchemyIncidentRepository,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError
from starlette.responses import StreamingResponse

from opsmind_api.audit import add_audit, add_timeline
from opsmind_api.auth import Principal
from opsmind_api.dependencies import authorize, get_principal, get_session
from opsmind_api.errors import ApiError
from opsmind_api.historical import snapshot_resolved_incident
from opsmind_api.schemas import (
    EvidenceResponse,
    HypothesisResponse,
    IncidentClose,
    IncidentCreate,
    IncidentEventCreate,
    IncidentEventResponse,
    IncidentPatch,
    IncidentResponse,
    Page,
    RemediationResponse,
    TimelineResponse,
)
from opsmind_api.topology import enrich_incident_topology

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _correlation_id(request: Request) -> str:
    return str(request.state.correlation_id)


def _incident_or_404(session: Session, tenant_id: UUID, incident_id: UUID) -> IncidentRow:
    row = session.scalar(
        select(IncidentRow).where(
            IncidentRow.tenant_id == tenant_id,
            IncidentRow.id == incident_id,
        )
    )
    if row is None:
        raise ApiError(404, "incident_not_found", "Incident was not found")
    return row


def _domain_incident_or_404(session: Session, tenant_id: UUID, incident_id: UUID) -> Incident:
    incident = SqlAlchemyIncidentRepository(session).get(tenant_id, incident_id)
    if incident is None:
        raise ApiError(404, "incident_not_found", "Incident was not found")
    return incident


def _incident_dict(incident: Incident) -> dict[str, Any]:
    return IncidentResponse.model_validate(incident).model_dump(mode="json")


def _to_response(incident: Incident) -> IncidentResponse:
    return IncidentResponse.model_validate(incident)


def _require_service(session: Session, tenant_id: UUID, service_id: UUID | None) -> None:
    if (
        service_id is not None
        and session.scalar(
            select(ServiceRow.id).where(
                ServiceRow.tenant_id == tenant_id,
                ServiceRow.id == service_id,
            )
        )
        is None
    ):
        raise ApiError(422, "invalid_service", "Service does not exist in this tenant")


def _add_audit_and_timeline(
    session: Session,
    request: Request,
    principal: Principal,
    *,
    incident: Incident,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    add_audit(
        session,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
        action=action,
        entity_id=incident.id,
        correlation_id=_correlation_id(request),
        before=before,
        after=after,
    )
    add_timeline(
        session,
        tenant_id=principal.tenant_id,
        incident_id=incident.id,
        event_type=action,
        summary=action.replace("_", " ").title(),
        actor_id=principal.subject_id,
    )


@router.post(
    "",
    response_model=IncidentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an incident",
    description="Creates a tenant-scoped incident. `Idempotency-Key` is required.",
    responses={401: {"model": dict}, 403: {"model": dict}, 409: {"model": dict}},
)
def create_incident(
    body: IncidentCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> IncidentResponse:
    authorize(request, principal, "incidents:create")
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    key_filter = (
        IncidentCreateIdempotencyRow.tenant_id == principal.tenant_id,
        IncidentCreateIdempotencyRow.idempotency_key == idempotency_key,
    )
    incident = Incident(
        tenant_id=principal.tenant_id,
        title=body.title.strip(),
        description=body.description,
        service_id=body.service_id,
        severity=body.severity,
    )
    repository = SqlAlchemyIncidentRepository(session)
    try:
        with session.begin():
            _require_service(session, principal.tenant_id, body.service_id)
            existing = session.scalar(select(IncidentCreateIdempotencyRow).where(*key_filter))
            if existing is not None:
                authorize(request, principal, "incidents:read", existing.incident_id)
                if existing.request_hash != request_hash:
                    raise ApiError(
                        409,
                        "idempotency_conflict",
                        "Idempotency key was used for another request",
                    )
                replay = repository.get(principal.tenant_id, existing.incident_id)
                if replay is None:
                    raise ApiError(
                        409, "idempotency_conflict", "Idempotency receipt has no incident"
                    )
                response.status_code = status.HTTP_200_OK
                return _to_response(replay)
            repository.add(incident)
            enrich_incident_topology(
                session,
                tenant_id=principal.tenant_id,
                incident_id=incident.id,
                service_id=incident.service_id,
                observed_at=incident.created_at,
            )
            session.add(
                IncidentCreateIdempotencyRow(
                    tenant_id=principal.tenant_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    incident_id=incident.id,
                )
            )
            snapshot = _incident_dict(incident)
            _add_audit_and_timeline(
                session,
                request,
                principal,
                incident=incident,
                action="incident_created",
                before=None,
                after=snapshot,
            )
    except IntegrityError as exc:
        session.rollback()
        existing = session.scalar(select(IncidentCreateIdempotencyRow).where(*key_filter))
        if existing is None:
            raise exc
        authorize(request, principal, "incidents:read", existing.incident_id)
        if existing.request_hash != request_hash:
            raise ApiError(
                409, "idempotency_conflict", "Idempotency key was used for another request"
            ) from exc
        replay = repository.get(principal.tenant_id, existing.incident_id)
        if replay is None:
            raise ApiError(
                409, "idempotency_conflict", "Idempotency receipt has no incident"
            ) from exc
        response.status_code = status.HTTP_200_OK
        return _to_response(replay)
    return _to_response(incident)


@router.get("", response_model=Page[IncidentResponse], summary="List incidents")
def list_incidents(
    request: Request,
    state: IncidentState | None = None,
    severity: str | None = Query(default=None, pattern=r"^SEV[1-4]$"),
    service_id: UUID | None = None,
    q: str | None = Query(default=None, max_length=200),
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> Page[IncidentResponse]:
    authorize(request, principal, "incidents:read")
    filters: list[Any] = [IncidentRow.tenant_id == principal.tenant_id]
    if state is not None:
        filters.append(IncidentRow.state == state.value)
    if severity is not None:
        filters.append(IncidentRow.severity == severity)
    if service_id is not None:
        filters.append(IncidentRow.service_id == service_id)
    if q:
        filters.append(IncidentRow.title.ilike(f"%{q}%"))
    if created_after:
        filters.append(IncidentRow.created_at >= created_after)
    if created_before:
        filters.append(IncidentRow.created_at <= created_before)
    total = session.scalar(select(func.count()).select_from(IncidentRow).where(*filters)) or 0
    rows = session.scalars(
        select(IncidentRow)
        .where(*filters)
        .order_by(IncidentRow.created_at.desc(), IncidentRow.id)
        .limit(limit)
        .offset(offset)
    ).all()
    items = [IncidentResponse.model_validate(row) for row in rows]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{incident_id}", response_model=IncidentResponse, summary="Get an incident")
def get_incident(
    incident_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> IncidentResponse:
    authorize(request, principal, "incidents:read")
    incident = SqlAlchemyIncidentRepository(session).get(principal.tenant_id, incident_id)
    if incident is None:
        raise ApiError(404, "incident_not_found", "Incident was not found")
    return _to_response(incident)


@router.get(
    "/{incident_id}/stream",
    summary="Stream live incident timeline updates",
    response_description="Server-sent events containing newly appended incident timeline entries.",
)
async def stream_incident(
    incident_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    """Stream authorized tenant-scoped timeline events without exposing DB sessions."""
    authorize(request, principal, "incidents:read", incident_id)
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        _incident_or_404(session, principal.tenant_id, incident_id)
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError as exc:
                raise ApiError(
                    400, "invalid_event_cursor", "Last-Event-ID must be an integer"
                ) from exc
            if cursor < 0 or cursor > 2**63 - 1:
                raise ApiError(400, "invalid_event_cursor", "Last-Event-ID is out of range")
        else:
            cursor = (
                session.scalar(
                    select(func.max(IncidentTimelineRow.sequence)).where(
                        IncidentTimelineRow.tenant_id == principal.tenant_id,
                        IncidentTimelineRow.incident_id == incident_id,
                    )
                )
                or 0
            )

    async def events() -> Any:
        nonlocal cursor
        deadline = asyncio.get_running_loop().time() + 900
        yield "retry: 3000\nevent: connected\ndata: {}\n\n"
        while not await request.is_disconnected() and asyncio.get_running_loop().time() < deadline:
            with session_factory() as session:
                rows = session.scalars(
                    select(IncidentTimelineRow)
                    .where(
                        IncidentTimelineRow.tenant_id == principal.tenant_id,
                        IncidentTimelineRow.incident_id == incident_id,
                        IncidentTimelineRow.sequence > cursor,
                    )
                    .order_by(IncidentTimelineRow.sequence)
                    .limit(100)
                ).all()
                messages = [
                    (
                        row.sequence,
                        f"id: {row.sequence}\nevent: timeline\ndata: "
                        f"{TimelineResponse.model_validate(row).model_dump_json()}\n\n",
                    )
                    for row in rows
                ]
            for sequence, message in messages:
                cursor = sequence
                yield message
            if not messages:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.patch("/{incident_id}", response_model=IncidentResponse, summary="Update an incident")
def patch_incident(
    incident_id: UUID,
    body: IncidentPatch,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> IncidentResponse:
    authorize(request, principal, "incidents:update", incident_id)
    try:
        with session.begin():
            row = _incident_or_404(session, principal.tenant_id, incident_id)
            previous = _domain_incident_or_404(session, principal.tenant_id, incident_id)
            before = _incident_dict(previous)
            if row.version != body.expected_version:
                raise ApiError(409, "stale_incident", "Incident version has changed")
            if "service_id" in body.model_fields_set:
                _require_service(session, principal.tenant_id, body.service_id)
            if body.state is not None and body.state.value != row.state:
                transitioned = previous.transition_to(body.state)
                previous_state = row.state
                row.state = transitioned.state.value
                row.updated_at = transitioned.updated_at
                if body.state is IncidentState.RESOLVED:
                    row.resolved_at = transitioned.updated_at
                elif previous_state == IncidentState.RESOLVED.value:
                    row.resolved_at = None
                if body.state is IncidentState.CLOSED:
                    row.closed_at = transitioned.updated_at
                if (
                    previous_state == IncidentState.RESOLVED.value
                    and body.state is not IncidentState.CLOSED
                ):
                    session.query(HistoricalIncidentRow).filter_by(
                        tenant_id=principal.tenant_id, incident_id=incident_id
                    ).delete(synchronize_session=False)
            if body.title is not None:
                row.title = body.title.strip()
            if "description" in body.model_fields_set:
                row.description = body.description
            if "service_id" in body.model_fields_set:
                row.service_id = body.service_id
            if body.severity is not None:
                row.severity = body.severity
            session.flush()
            if row.state == IncidentState.RESOLVED.value:
                snapshot_resolved_incident(session, row)
            if "service_id" in body.model_fields_set:
                enrich_incident_topology(
                    session,
                    tenant_id=principal.tenant_id,
                    incident_id=incident_id,
                    service_id=row.service_id,
                )
            current = _domain_incident_or_404(session, principal.tenant_id, incident_id)
            _add_audit_and_timeline(
                session,
                request,
                principal,
                incident=current,
                action="incident_updated",
                before=before,
                after=_incident_dict(current),
            )
    except InvalidIncidentTransition as exc:
        raise ApiError(409, "invalid_transition", str(exc)) from exc
    except ConcurrentIncidentUpdate as exc:
        raise ApiError(409, "stale_incident", str(exc)) from exc
    except StaleDataError as exc:
        session.rollback()
        raise ApiError(409, "stale_incident", "Incident was concurrently updated") from exc
    return _to_response(current)


@router.post(
    "/{incident_id}/events",
    response_model=IncidentEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append an incident event",
)
def add_incident_event(
    incident_id: UUID,
    body: IncidentEventCreate,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> IncidentEventResponse:
    authorize(request, principal, "incidents:events:write", incident_id)
    try:
        with session.begin():
            event, created = SqlAlchemyIncidentRepository(session).add_event_once(
                IncidentEventData(
                    tenant_id=principal.tenant_id,
                    incident_id=incident_id,
                    event_type=body.event_type,
                    source=body.source,
                    idempotency_key=idempotency_key,
                    correlation_id=_correlation_id(request),
                    payload=body.payload,
                )
            )
            if created:
                row = session.get(IncidentEventRow, event.id)
                add_audit(
                    session,
                    tenant_id=principal.tenant_id,
                    actor_id=principal.subject_id,
                    action="incident_event_added",
                    entity_id=incident_id,
                    correlation_id=_correlation_id(request),
                    before=None,
                    after={"event_id": str(event.id), "event_type": event.event_type},
                )
                add_timeline(
                    session,
                    tenant_id=principal.tenant_id,
                    incident_id=incident_id,
                    event_id=event.id,
                    event_type=event.event_type,
                    summary=event.event_type,
                    actor_id=principal.subject_id,
                    occurred_at=event.occurred_at,
                )
                session.flush()
                if row is None:
                    raise RuntimeError("incident event was not persisted")
            else:
                authorize(request, principal, "incidents:read", incident_id)
    except IdempotencyConflict as exc:
        raise ApiError(409, "idempotency_conflict", str(exc)) from exc
    except IncidentNotFound as exc:
        raise ApiError(404, "incident_not_found", "Incident was not found") from exc
    return IncidentEventResponse(
        id=event.id,
        incident_id=event.incident_id,
        event_type=event.event_type,
        source=event.source,
        idempotency_key=event.idempotency_key,
        correlation_id=event.correlation_id,
        payload=event.payload,
        occurred_at=event.occurred_at,
    )


@router.post("/{incident_id}/close", response_model=IncidentResponse, summary="Close an incident")
def close_incident(
    incident_id: UUID,
    body: IncidentClose,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> IncidentResponse:
    authorize(request, principal, "incidents:close", incident_id)
    try:
        with session.begin():
            _incident_or_404(session, principal.tenant_id, incident_id)
            before = _incident_dict(
                _domain_incident_or_404(session, principal.tenant_id, incident_id)
            )
            incident = SqlAlchemyIncidentRepository(session).transition(
                principal.tenant_id,
                incident_id,
                IncidentState.CLOSED,
                expected_version=body.expected_version,
            )
            _add_audit_and_timeline(
                session,
                request,
                principal,
                incident=incident,
                action="incident_closed",
                before=before,
                after=_incident_dict(incident),
            )
    except IncidentNotFound as exc:
        raise ApiError(404, "incident_not_found", "Incident was not found") from exc
    except InvalidIncidentTransition as exc:
        raise ApiError(409, "invalid_transition", str(exc)) from exc
    except ConcurrentIncidentUpdate as exc:
        raise ApiError(409, "stale_incident", str(exc)) from exc
    return _to_response(incident)


def _list_for_incident(
    session: Session,
    principal: Principal,
    incident_id: UUID,
    model: Any,
    response_model: Any,
    *,
    limit: int,
    offset: int,
    ordering: Any,
) -> Page[Any]:
    _incident_or_404(session, principal.tenant_id, incident_id)
    filters = [model.tenant_id == principal.tenant_id, model.incident_id == incident_id]
    total = session.scalar(select(func.count()).select_from(model).where(*filters)) or 0
    rows: list[Any] = list(
        session.scalars(
            select(model).where(*filters).order_by(ordering).limit(limit).offset(offset)
        ).all()
    )
    return Page(
        items=[response_model.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{incident_id}/timeline",
    response_model=Page[TimelineResponse],
    summary="Get incident timeline",
)
def get_timeline(
    incident_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> Page[TimelineResponse]:
    authorize(request, principal, "incidents:read", incident_id)
    return _list_for_incident(
        session,
        principal,
        incident_id,
        IncidentTimelineRow,
        TimelineResponse,
        limit=limit,
        offset=offset,
        ordering=IncidentTimelineRow.sequence,
    )


@router.get(
    "/{incident_id}/evidence",
    response_model=Page[EvidenceResponse],
    summary="List incident evidence",
)
def get_evidence(
    incident_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> Page[EvidenceResponse]:
    authorize(request, principal, "incidents:read", incident_id)
    return _list_for_incident(
        session,
        principal,
        incident_id,
        IncidentEvidenceRow,
        EvidenceResponse,
        limit=limit,
        offset=offset,
        ordering=IncidentEvidenceRow.observed_at.desc(),
    )


@router.get(
    "/{incident_id}/hypotheses",
    response_model=Page[HypothesisResponse],
    summary="List incident hypotheses",
)
def get_hypotheses(
    incident_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> Page[HypothesisResponse]:
    authorize(request, principal, "incidents:read", incident_id)
    return _list_for_incident(
        session,
        principal,
        incident_id,
        IncidentHypothesisRow,
        HypothesisResponse,
        limit=limit,
        offset=offset,
        ordering=IncidentHypothesisRow.created_at.desc(),
    )


@router.get(
    "/{incident_id}/remediations",
    response_model=Page[RemediationResponse],
    summary="List incident remediations",
)
def get_remediations(
    incident_id: UUID,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> Page[RemediationResponse]:
    authorize(request, principal, "incidents:read", incident_id)
    return _list_for_incident(
        session,
        principal,
        incident_id,
        RemediationActionRow,
        RemediationResponse,
        limit=limit,
        offset=offset,
        ordering=RemediationActionRow.created_at.desc(),
    )
