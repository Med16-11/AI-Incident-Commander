"""Create searchable snapshots when an incident is resolved."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from opsmind_storage.models import (
    HistoricalIncidentRow,
    IncidentEvidenceRow,
    IncidentHypothesisRow,
    IncidentRow,
    InvestigationRunRow,
    RemediationActionRow,
    RemediationExecutionRow,
    ServiceRow,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


def snapshot_resolved_incident(session: Session, incident: IncidentRow) -> HistoricalIncidentRow:
    """Upsert a tenant-scoped summary from resolved incident and investigation records."""
    resolved_at = incident.resolved_at or incident.updated_at or datetime.now(UTC)
    service = session.get(ServiceRow, incident.service_id) if incident.service_id else None
    run = session.scalar(
        select(InvestigationRunRow)
        .where(
            InvestigationRunRow.tenant_id == incident.tenant_id,
            InvestigationRunRow.incident_id == incident.id,
            InvestigationRunRow.status == "COMPLETED",
        )
        .order_by(InvestigationRunRow.started_at.desc())
        .limit(1)
    )
    run_data = run.state_data if run is not None else {}
    summary = _plain_text(run_data.get("analysis_summary")) or " ".join(
        item for item in (incident.title, incident.description or "") if item
    )

    evidence_rows = session.scalars(
        select(IncidentEvidenceRow)
        .where(
            IncidentEvidenceRow.tenant_id == incident.tenant_id,
            IncidentEvidenceRow.incident_id == incident.id,
        )
        .order_by(IncidentEvidenceRow.observed_at.asc())
        .limit(200)
    ).all()
    historical_evidence = [
        {
            "evidence_id": str(row.id),
            "source": row.source[:160],
            "timestamp": row.observed_at.isoformat(),
            "service": row.service[:200],
            "evidence_type": row.evidence_type[:100],
            "summary": row.normalized_summary[:2000],
        }
        for row in evidence_rows
    ]
    symptoms = _strings(run_data.get("symptoms"))
    if not symptoms:
        symptoms = list(
            dict.fromkeys(
                row.normalized_summary
                for row in evidence_rows
                if row.evidence_type
                in {"log", "metric_anomaly", "metric_observation", "service_health"}
            )
        )[:50]

    root_cause_data = run_data.get("root_cause")
    root_cause = _text(root_cause_data)
    if not root_cause:
        hypothesis = session.scalar(
            select(IncidentHypothesisRow)
            .where(
                IncidentHypothesisRow.tenant_id == incident.tenant_id,
                IncidentHypothesisRow.incident_id == incident.id,
                IncidentHypothesisRow.status.in_(("identified", "confirmed", "probable")),
            )
            .order_by(IncidentHypothesisRow.created_at.desc())
            .limit(1)
        )
        root_cause = (
            hypothesis.summary
            if hypothesis is not None
            else "No supported root cause was documented for this incident."
        )

    affected_services = _strings(run_data.get("affected_services")) or (
        [service.name] if service is not None else []
    )
    remediation = _remediation_snapshot(session, incident, run_data)
    normalized_resolved = _aware_utc(resolved_at)
    normalized_detected = _aware_utc(incident.detected_at)
    resolution_seconds = max(0.0, (normalized_resolved - normalized_detected).total_seconds())
    tags = list(
        dict.fromkeys(
            [
                incident.severity.casefold(),
                *(name.casefold() for name in affected_services),
                *(
                    f"evidence:{kind}"
                    for kind in sorted({row.evidence_type for row in evidence_rows})
                ),
            ]
        )
    )[:50]
    search_text = " ".join(
        [
            summary,
            *symptoms,
            root_cause,
            *affected_services,
            *[str(item.get("summary", "")) for item in historical_evidence],
            *[str(item.get("summary", item.get("action_type", ""))) for item in remediation],
            *tags,
        ]
    )

    snapshot = session.scalar(
        select(HistoricalIncidentRow).where(
            HistoricalIncidentRow.tenant_id == incident.tenant_id,
            HistoricalIncidentRow.incident_id == incident.id,
        )
    )
    if snapshot is None:
        snapshot = HistoricalIncidentRow(
            tenant_id=incident.tenant_id,
            incident_id=incident.id,
            service_id=incident.service_id,
            summary=summary,
            symptoms=symptoms,
            root_cause=root_cause,
            affected_services=affected_services,
            evidence=historical_evidence,
            remediation=remediation,
            resolved_at=resolved_at,
            resolution_seconds=resolution_seconds,
            tags=tags,
            search_text=search_text,
        )
        session.add(snapshot)
    else:
        snapshot.service_id = incident.service_id
        snapshot.summary = summary
        snapshot.symptoms = symptoms
        snapshot.root_cause = root_cause
        snapshot.affected_services = affected_services
        snapshot.evidence = historical_evidence
        snapshot.remediation = remediation
        snapshot.resolved_at = resolved_at
        snapshot.resolution_seconds = resolution_seconds
        snapshot.tags = tags
        snapshot.search_text = search_text
    session.flush()
    return snapshot


def _remediation_snapshot(
    session: Session, incident: IncidentRow, run_data: dict[str, Any]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in run_data.get("remediation_recommendations", []):
        if isinstance(item, str) and item.strip():
            results.append({"kind": "recommended", "summary": item[:2000]})
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            results.append({"kind": "recommended", "summary": item["text"][:2000]})
    actions = session.scalars(
        select(RemediationActionRow)
        .where(
            RemediationActionRow.tenant_id == incident.tenant_id,
            RemediationActionRow.incident_id == incident.id,
        )
        .order_by(RemediationActionRow.created_at.asc())
        .limit(100)
    ).all()
    for action in actions:
        executions = session.scalars(
            select(RemediationExecutionRow)
            .where(
                RemediationExecutionRow.tenant_id == incident.tenant_id,
                RemediationExecutionRow.action_id == action.id,
            )
            .order_by(RemediationExecutionRow.started_at.asc())
            .limit(20)
        ).all()
        results.append(
            {
                "kind": "action",
                "action_type": action.action_type,
                "status": action.status,
                "risk_level": action.risk_level,
                "execution_outcomes": [item.status for item in executions],
            }
        )
    return results[:120]


def _text(value: object) -> str:
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        text = str(value["text"])[:4000]
        if value.get("evidence_status") != "supported":
            return f"Insufficient current evidence to establish root cause: {text}"
        return text
    return ""


def _plain_text(value: object) -> str:
    """Read a model summary while preserving its stated certainty wording."""
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return str(value["text"])[:4000]
    return ""


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item.strip()[:2000] for item in value if isinstance(item, str) and item.strip()
        )
    )[:50]


def _aware_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamps and PostgreSQL timestamps for duration math."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
