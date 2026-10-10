"""Persistence adapter and incident enrichment for service topology."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from opsmind_models.domain.topology import (
    ServiceDescriptor,
    ServiceTopology,
    TopologyAnalysis,
    TopologyEdge,
)
from opsmind_storage.models import (
    IncidentEvidenceRow,
    IncidentRow,
    ServiceDependencyRow,
    ServiceRow,
)
from sqlalchemy import or_, select
from sqlalchemy.orm import Session


def load_topology(session: Session, tenant_id: UUID) -> ServiceTopology:
    """Load one tenant's current service graph using bounded, indexed queries."""
    services = session.scalars(
        select(ServiceRow)
        .where(ServiceRow.tenant_id == tenant_id)
        .order_by(ServiceRow.name, ServiceRow.id)
    ).all()
    now = datetime.now(UTC)
    dependencies = session.scalars(
        select(ServiceDependencyRow).where(
            ServiceDependencyRow.tenant_id == tenant_id,
            ServiceDependencyRow.valid_from <= now,
            or_(
                ServiceDependencyRow.valid_until.is_(None), ServiceDependencyRow.valid_until >= now
            ),
        )
    ).all()
    return ServiceTopology(
        [
            ServiceDescriptor(
                id=service.id,
                tenant_id=service.tenant_id,
                name=service.name,
                environment=service.environment,
            )
            for service in services
        ],
        [
            TopologyEdge(
                source_service_id=edge.source_service_id,
                target_service_id=edge.target_service_id,
            )
            for edge in dependencies
        ],
    )


def analysis_as_dict(analysis: TopologyAnalysis) -> dict[str, object]:
    """Serialize topology results as stable, source-backed incident evidence."""

    def service_data(service: ServiceDescriptor) -> dict[str, str]:
        return {
            "id": str(service.id),
            "name": service.name,
            "environment": service.environment,
        }

    return {
        "affected_service": service_data(analysis.service),
        "direct_dependencies": [service_data(service) for service in analysis.direct_dependencies],
        "upstream_impact": [service_data(service) for service in analysis.upstream_services],
        "downstream_impact": [service_data(service) for service in analysis.downstream_services],
        "upstream_depth": analysis.upstream_depth,
        "downstream_depth": analysis.downstream_depth,
        "critical_path": [service_data(service) for service in analysis.critical_path],
        "likely_blast_radius": [service_data(service) for service in analysis.blast_radius],
        "likely_blast_radius_count": len(analysis.blast_radius),
    }


def enrich_incident_topology(
    session: Session,
    *,
    tenant_id: UUID,
    incident_id: UUID,
    service_id: UUID | None,
    observed_at: datetime | None = None,
    topology: ServiceTopology | None = None,
) -> None:
    """Persist a deterministic topology snapshot as incident evidence."""
    if service_id is None:
        return
    topology = topology or load_topology(session, tenant_id)
    service = topology.services.get(service_id)
    if service is None:
        return
    analysis = topology.analyze(service_id)
    content = analysis_as_dict(analysis)
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    exists = session.scalar(
        select(IncidentEvidenceRow.id).where(
            IncidentEvidenceRow.tenant_id == tenant_id,
            IncidentEvidenceRow.incident_id == incident_id,
            IncidentEvidenceRow.content_hash == content_hash,
        )
    )
    if exists is not None:
        return
    when = observed_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    session.add(
        IncidentEvidenceRow(
            tenant_id=tenant_id,
            incident_id=incident_id,
            evidence_type="service_topology",
            source="opsmind-service-topology",
            service=service.name,
            normalized_summary=(
                f"{service.name} has {len(analysis.blast_radius)} service(s) in its likely "
                f"blast radius and {len(analysis.downstream_services)} downstream dependencies"
            ),
            confidence=1.0,
            external_ref=f"service-topology://{tenant_id}/{service_id}",
            content=content,
            content_hash=content_hash,
            observed_at=when,
        )
    )


def refresh_open_incident_topology(
    session: Session,
    *,
    tenant_id: UUID,
    service_ids: set[UUID],
) -> None:
    """Append refreshed topology evidence for open incidents in changed graph regions."""
    if not service_ids:
        return
    topology = load_topology(session, tenant_id)
    incidents = session.scalars(
        select(IncidentRow).where(
            IncidentRow.tenant_id == tenant_id,
            IncidentRow.service_id.in_(service_ids),
            IncidentRow.state.notin_(("RESOLVED", "CLOSED")),
        )
    ).all()
    for incident in incidents:
        enrich_incident_topology(
            session,
            tenant_id=tenant_id,
            incident_id=incident.id,
            service_id=incident.service_id,
            observed_at=datetime.now(UTC),
            topology=topology,
        )
