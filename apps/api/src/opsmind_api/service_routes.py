"""Tenant-scoped service registry and dependency graph API."""

# FastAPI uses Depends in endpoint signatures as declarative dependency metadata.
# ruff: noqa: B008

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from opsmind_models.domain.topology import (
    MissingTopologyService,
    ServiceDescriptor,
    TopologyCycle,
)
from opsmind_storage.models import AuditLogRow, ServiceDependencyRow, ServiceRow
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opsmind_api.auth import Principal
from opsmind_api.dependencies import authorize, get_principal, get_session
from opsmind_api.errors import ApiError
from opsmind_api.schemas import (
    Page,
    ServiceBlastRadiusResponse,
    ServiceCreate,
    ServiceDependenciesResponse,
    ServiceDependencyCreate,
    ServiceDependencyResponse,
    ServiceResponse,
    ServiceTopologyNode,
)
from opsmind_api.topology import load_topology, refresh_open_incident_topology

router = APIRouter(prefix="/services", tags=["services"])


def _correlation_id(request: Request) -> str:
    return str(request.state.correlation_id)


def _node(service: ServiceDescriptor) -> ServiceTopologyNode:
    return ServiceTopologyNode(
        id=service.id,
        name=service.name,
        environment=service.environment,
    )


def _service_response(service: ServiceRow) -> ServiceResponse:
    return ServiceResponse(
        id=service.id,
        tenant_id=service.tenant_id,
        name=service.name,
        environment=service.environment,
        owner_team=service.owner_team,
        repository_url=service.repository_url,
        metadata=service.metadata_json,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _service_or_404(session: Session, tenant_id: UUID, service_id: UUID) -> ServiceRow:
    service = session.scalar(
        select(ServiceRow).where(
            ServiceRow.tenant_id == tenant_id,
            ServiceRow.id == service_id,
        )
    )
    if service is None:
        raise ApiError(404, "service_not_found", "Service was not found")
    return service


def _audit_service_change(
    session: Session,
    request: Request,
    principal: Principal,
    *,
    entity_type: str,
    entity_id: UUID,
    action: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    session.add(
        AuditLogRow(
            tenant_id=principal.tenant_id,
            actor_type="user",
            actor_id=principal.subject_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome="success",
            correlation_id=_correlation_id(request),
            before_state=before,
            after_state=after,
        )
    )


def _dependency_response(edge: ServiceDependencyRow) -> ServiceDependencyResponse:
    return ServiceDependencyResponse(
        id=edge.id,
        source_service_id=edge.source_service_id,
        target_service_id=edge.target_service_id,
        dependency_type=edge.dependency_type,
        valid_from=edge.valid_from,
    )


@router.post("", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
def create_service(
    body: ServiceCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> ServiceResponse:
    """Register a service node so dependencies can be managed through the API."""
    authorize(request, principal, "services:write")
    service = ServiceRow(
        tenant_id=principal.tenant_id,
        name=body.name.strip(),
        environment=body.environment.strip(),
        owner_team=body.owner_team,
        repository_url=body.repository_url,
        metadata_json=body.metadata,
    )
    try:
        with session.begin():
            session.add(service)
            session.flush()
            _audit_service_change(
                session,
                request,
                principal,
                entity_type="service",
                entity_id=service.id,
                action="service_created",
                before=None,
                after={"name": service.name, "environment": service.environment},
            )
    except IntegrityError as exc:
        session.rollback()
        raise ApiError(
            409, "duplicate_service", "Service already exists in this environment"
        ) from exc
    return _service_response(service)


@router.get("", response_model=Page[ServiceResponse], summary="List services")
def list_services(
    request: Request,
    environment: str | None = Query(default=None, min_length=1, max_length=100),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> Page[ServiceResponse]:
    authorize(request, principal, "services:read")
    filters: list[Any] = [ServiceRow.tenant_id == principal.tenant_id]
    if environment:
        filters.append(ServiceRow.environment == environment)
    if q:
        filters.append(ServiceRow.name.ilike(f"%{q}%"))
    total = session.scalar(select(func.count()).select_from(ServiceRow).where(*filters)) or 0
    rows = session.scalars(
        select(ServiceRow)
        .where(*filters)
        .order_by(ServiceRow.environment, ServiceRow.name, ServiceRow.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return Page(
        items=[_service_response(service) for service in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{service_id}", response_model=ServiceResponse, summary="Get a service")
def get_service(
    service_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> ServiceResponse:
    authorize(request, principal, "services:read", service_id)
    return _service_response(_service_or_404(session, principal.tenant_id, service_id))


@router.get(
    "/{service_id}/dependencies",
    response_model=ServiceDependenciesResponse,
    summary="Get service dependency graph",
)
def get_dependencies(
    service_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> ServiceDependenciesResponse:
    authorize(request, principal, "services:read", service_id)
    topology = load_topology(session, principal.tenant_id)
    try:
        result = topology.analyze(service_id)
    except MissingTopologyService as exc:
        raise ApiError(404, "service_not_found", "Service was not found") from exc
    return ServiceDependenciesResponse(
        service=_node(result.service),
        direct_dependencies=[_node(service) for service in result.direct_dependencies],
        upstream_services=[_node(service) for service in result.upstream_services],
        downstream_services=[_node(service) for service in result.downstream_services],
        upstream_depth=result.upstream_depth,
        downstream_depth=result.downstream_depth,
        critical_path=[_node(service) for service in result.critical_path],
    )


@router.get(
    "/{service_id}/blast-radius",
    response_model=ServiceBlastRadiusResponse,
    summary="Calculate service blast radius",
)
def get_blast_radius(
    service_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> ServiceBlastRadiusResponse:
    authorize(request, principal, "services:read", service_id)
    topology = load_topology(session, principal.tenant_id)
    try:
        result = topology.analyze(service_id)
    except MissingTopologyService as exc:
        raise ApiError(404, "service_not_found", "Service was not found") from exc
    blast_radius = [_node(service) for service in result.blast_radius]
    return ServiceBlastRadiusResponse(
        affected_service=_node(result.service),
        upstream_impact=[_node(service) for service in result.upstream_services],
        downstream_impact=[_node(service) for service in result.downstream_services],
        likely_blast_radius=blast_radius,
        likely_blast_radius_count=len(blast_radius),
        upstream_depth=result.upstream_depth,
        downstream_depth=result.downstream_depth,
        critical_path=[_node(service) for service in result.critical_path],
    )


@router.post(
    "/{service_id}/dependencies",
    response_model=ServiceDependencyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a service dependency",
)
def create_dependency(
    service_id: UUID,
    body: ServiceDependencyCreate,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> ServiceDependencyResponse:
    authorize(request, principal, "services:dependencies:write", service_id)
    try:
        with session.begin():
            # Serialize topology mutations within the tenant to prevent concurrent cycle races.
            session.scalars(
                select(ServiceRow.id)
                .where(ServiceRow.tenant_id == principal.tenant_id)
                .order_by(ServiceRow.id)
                .with_for_update()
            ).all()
            topology = load_topology(session, principal.tenant_id)
            try:
                source = topology.services[service_id]
                target = topology.services[body.target_service_id]
            except KeyError as exc:
                raise ApiError(
                    404, "service_not_found", "Dependency service was not found"
                ) from exc
            if source.environment != target.environment:
                raise ApiError(
                    422,
                    "service_environment_mismatch",
                    "Service dependencies must be within the same environment",
                )
            try:
                topology.assert_edge_allowed(service_id, body.target_service_id)
            except TopologyCycle as exc:
                raise ApiError(409, "dependency_cycle", str(exc)) from exc
            if body.target_service_id in topology.downstream[service_id]:
                raise ApiError(409, "duplicate_dependency", "Dependency already exists")

            changed_region = (
                set(topology.upstream[service_id])
                | {service_id}
                | set(topology.downstream[body.target_service_id])
                | {body.target_service_id}
            )

            edge = ServiceDependencyRow(
                tenant_id=principal.tenant_id,
                source_service_id=service_id,
                target_service_id=body.target_service_id,
                dependency_type=body.dependency_type.strip(),
            )
            session.add(edge)
            session.flush()
            refresh_open_incident_topology(
                session,
                tenant_id=principal.tenant_id,
                service_ids=changed_region,
            )
            _audit_service_change(
                session,
                request,
                principal,
                entity_type="service_dependency",
                entity_id=edge.id,
                action="service_dependency_created",
                before=None,
                after={
                    "source_service_id": str(service_id),
                    "target_service_id": str(body.target_service_id),
                    "dependency_type": edge.dependency_type,
                },
            )
    except IntegrityError as exc:
        session.rollback()
        raise ApiError(409, "duplicate_dependency", "Dependency already exists") from exc
    return _dependency_response(edge)


@router.delete(
    "/{service_id}/dependencies/{dependency_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a service dependency",
)
def remove_dependency(
    service_id: UUID,
    dependency_id: UUID,
    request: Request,
    principal: Principal = Depends(get_principal),
    session: Session = Depends(get_session),
) -> Response:
    authorize(request, principal, "services:dependencies:write", service_id)
    with session.begin():
        edge = session.scalar(
            select(ServiceDependencyRow).where(
                ServiceDependencyRow.tenant_id == principal.tenant_id,
                ServiceDependencyRow.source_service_id == service_id,
                ServiceDependencyRow.id == dependency_id,
            )
        )
        if edge is None:
            raise ApiError(404, "dependency_not_found", "Service dependency was not found")
        topology = load_topology(session, principal.tenant_id)
        changed_region = (
            set(topology.upstream[edge.source_service_id])
            | {edge.source_service_id}
            | set(topology.downstream[edge.target_service_id])
            | {edge.target_service_id}
        )
        before = {
            "source_service_id": str(edge.source_service_id),
            "target_service_id": str(edge.target_service_id),
            "dependency_type": edge.dependency_type,
        }
        _audit_service_change(
            session,
            request,
            principal,
            entity_type="service_dependency",
            entity_id=edge.id,
            action="service_dependency_removed",
            before=before,
            after=None,
        )
        session.delete(edge)
        session.flush()
        refresh_open_incident_topology(
            session,
            tenant_id=principal.tenant_id,
            service_ids=changed_region,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
