"""Deterministic incident evidence collection and chronological timeline storage."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import structlog
from opsmind_storage.models import (
    DeploymentRow,
    IncidentEventRow,
    IncidentEvidenceRow,
    IncidentRow,
    IncidentTimelineRow,
    LogRecordRow,
    MetricSnapshotRow,
    ServiceDependencyRow,
    ServiceRow,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


class IncidentContext(BaseModel):
    """Minimum persisted incident metadata required for bounded correlation."""

    model_config = ConfigDict(frozen=True)

    incident_id: UUID
    tenant_id: UUID
    service: str = Field(min_length=1, max_length=200)
    environment: str | None = Field(default=None, min_length=1, max_length=100)
    detected_at: datetime
    dependencies: tuple[str, ...] = ()

    @field_validator("detected_at")
    @classmethod
    def detection_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("detected_at must include a timezone")
        return value.astimezone(UTC)


class CorrelationWindow(BaseModel):
    """Configured look-behind and look-ahead durations."""

    before_seconds: int = Field(default=600, ge=0, le=7 * 24 * 60 * 60)
    after_seconds: int = Field(default=300, ge=0, le=24 * 60 * 60)

    def bounds(self, detected_at: datetime) -> tuple[datetime, datetime]:
        return (
            detected_at - timedelta(seconds=self.before_seconds),
            detected_at + timedelta(seconds=self.after_seconds),
        )


class EvidenceCandidate(BaseModel):
    """Source-backed evidence; no generated claims are accepted without a reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=160)
    timestamp: datetime
    service: str = Field(min_length=1, max_length=200)
    evidence_type: str = Field(min_length=1, max_length=100)
    raw_reference: str = Field(min_length=1, max_length=2000)
    normalized_summary: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0, le=1)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("raw_reference")
    @classmethod
    def redact_reference_credentials(cls, value: str) -> str:
        return redact_evidence_reference(value)

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence timestamp must include a timezone")
        return value.astimezone(UTC)

    @property
    def content_hash(self) -> str:
        """Stable idempotency digest over source identity and normalized content."""
        material = {
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
            "service": self.service,
            "type": self.evidence_type,
            "reference": self.raw_reference,
            "summary": self.normalized_summary,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class EvidenceSource(Protocol):
    """Adapter that returns source-backed evidence within the supplied time bounds."""

    name: str

    def collect(
        self, context: IncidentContext, start: datetime, end: datetime
    ) -> Iterable[EvidenceCandidate]: ...


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    incident_id: UUID
    evidence: tuple[EvidenceCandidate, ...]
    source_failures: tuple[str, ...]


class IncidentCorrelationEngine:
    """Collect independently sourced evidence and persist an ordered evidence timeline."""

    def __init__(
        self,
        sources: Sequence[EvidenceSource],
        window: CorrelationWindow | None = None,
    ) -> None:
        self._sources = tuple(sources)
        self._window = window or CorrelationWindow()
        self._logger = structlog.get_logger(service="incident-engine")

    @property
    def window(self) -> CorrelationWindow:
        return self._window

    def collect(self, context: IncidentContext) -> CorrelationResult:
        """Gather within the incident window; one source outage does not hide others."""
        start, end = self._window.bounds(context.detected_at)
        collected: dict[str, EvidenceCandidate] = {}
        failures: list[str] = []
        for source in self._sources:
            try:
                for item in source.collect(context, start, end):
                    if start <= item.timestamp <= end:
                        collected[item.content_hash] = item
            except Exception as exc:
                failures.append(source.name)
                self._logger.error(
                    "incident_evidence_source_failed",
                    source=source.name,
                    incident_id=str(context.incident_id),
                    error_type=type(exc).__name__,
                )
        return CorrelationResult(
            incident_id=context.incident_id,
            evidence=tuple(
                sorted(
                    collected.values(),
                    key=lambda item: (item.timestamp, item.source, item.raw_reference),
                )
            ),
            source_failures=tuple(failures),
        )

    def correlate(self, session: Session, context: IncidentContext) -> CorrelationResult:
        """Collect source evidence, then persist it in the caller's transaction."""
        result = self.collect(context)
        self.persist(session, context, result)
        return result

    def persist(
        self, session: Session, context: IncidentContext, result: CorrelationResult
    ) -> None:
        """Persist a completed collection under an incident row lock."""
        if result.incident_id != context.incident_id:
            raise ValueError("correlation result does not match incident context")
        incident_row = session.scalar(
            select(IncidentRow)
            .where(
                IncidentRow.id == context.incident_id,
                IncidentRow.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if incident_row is None:
            raise LookupError("incident not found in tenant scope")

        existing_hashes = set(
            session.scalars(
                select(IncidentEvidenceRow.content_hash).where(
                    IncidentEvidenceRow.incident_id == context.incident_id,
                    IncidentEvidenceRow.tenant_id == context.tenant_id,
                    IncidentEvidenceRow.content_hash.is_not(None),
                )
            ).all()
        )
        sequence = (
            session.scalar(
                select(func.max(IncidentTimelineRow.sequence)).where(
                    IncidentTimelineRow.incident_id == context.incident_id,
                    IncidentTimelineRow.tenant_id == context.tenant_id,
                )
            )
            or 0
        )

        for item in result.evidence:
            if item.content_hash in existing_hashes:
                continue
            evidence = IncidentEvidenceRow(
                tenant_id=context.tenant_id,
                incident_id=context.incident_id,
                evidence_type=item.evidence_type,
                source=item.source,
                service=item.service,
                external_ref=item.raw_reference,
                normalized_summary=item.normalized_summary,
                confidence=item.confidence,
                content={"attributes": item.attributes},
                content_hash=item.content_hash,
                observed_at=item.timestamp,
            )
            session.add(evidence)
            session.flush()
            sequence += 1
            session.add(
                IncidentTimelineRow(
                    tenant_id=context.tenant_id,
                    incident_id=context.incident_id,
                    evidence_id=evidence.id,
                    sequence=sequence,
                    event_type=item.evidence_type,
                    summary=item.normalized_summary,
                    occurred_at=item.timestamp,
                )
            )
            existing_hashes.add(item.content_hash)
        session.flush()


class DatabaseEvidenceSource:
    """Correlate persisted incident events, logs, metrics, deployments, and dependencies."""

    name = "postgresql"

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def collect(
        self, context: IncidentContext, start: datetime, end: datetime
    ) -> Iterable[EvidenceCandidate]:
        with self._session_factory() as session:
            return self._collect(session, context, start, end)

    def _collect(
        self, session: Session, context: IncidentContext, start: datetime, end: datetime
    ) -> Iterable[EvidenceCandidate]:
        services_query = select(ServiceRow.id).where(
            ServiceRow.tenant_id == context.tenant_id,
            ServiceRow.name.in_((context.service, *context.dependencies)),
        )
        if context.environment is not None:
            services_query = services_query.where(ServiceRow.environment == context.environment)
        service_ids = set(session.scalars(services_query).all())
        names = {
            row.id: row.name
            for row in session.scalars(
                select(ServiceRow).where(
                    ServiceRow.tenant_id == context.tenant_id,
                    ServiceRow.id.in_(service_ids),
                )
            ).all()
        }
        candidates: list[EvidenceCandidate] = [
            EvidenceCandidate(
                source="opsmind.incidents",
                timestamp=context.detected_at,
                service=context.service,
                evidence_type="incident_detected",
                raw_reference=f"incident://{context.incident_id}",
                normalized_summary="Incident detection timestamp",
                confidence=1.0,
            )
        ]

        events = session.scalars(
            select(IncidentEventRow)
            .where(
                IncidentEventRow.tenant_id == context.tenant_id,
                IncidentEventRow.incident_id == context.incident_id,
                IncidentEventRow.occurred_at >= start,
                IncidentEventRow.occurred_at <= end,
            )
            .limit(5000)
        ).all()
        for event in events:
            event_type = _event_evidence_type(event.event_type)
            if event_type is None:
                continue
            summary = event.payload.get("summary")
            candidates.append(
                EvidenceCandidate(
                    source=event.source,
                    timestamp=_utc(event.occurred_at),
                    service=context.service,
                    evidence_type=event_type,
                    raw_reference=f"incident-event://{event.id}",
                    normalized_summary=redact_evidence_summary(
                        str(summary) if summary else f"Recorded {event.event_type} event"
                    ),
                    confidence=1.0,
                    attributes={"event_id": str(event.id), "event_type": event.event_type},
                )
            )

        deployments = session.scalars(
            select(DeploymentRow)
            .where(
                DeploymentRow.tenant_id == context.tenant_id,
                DeploymentRow.service_id.in_(service_ids),
                DeploymentRow.started_at >= start,
                DeploymentRow.started_at <= end,
            )
            .limit(5000)
        ).all()
        for deployment in deployments:
            service = names.get(deployment.service_id, context.service)
            version = deployment.version
            candidates.append(
                EvidenceCandidate(
                    source="opsmind.deployments",
                    timestamp=_utc(deployment.started_at),
                    service=service,
                    evidence_type="deployment",
                    raw_reference=f"deployment://{deployment.id}",
                    normalized_summary=redact_evidence_summary(
                        f"Deployment {version} started ({deployment.status})"
                    ),
                    confidence=1.0,
                    attributes={
                        "deployment_id": str(deployment.id),
                        "version": version,
                        "commit_sha": deployment.commit_sha,
                    },
                )
            )

        dependencies = session.scalars(
            select(ServiceDependencyRow)
            .where(
                ServiceDependencyRow.tenant_id == context.tenant_id,
                ServiceDependencyRow.source_service_id.in_(service_ids),
                ServiceDependencyRow.valid_from <= end,
            )
            .limit(5000)
        ).all()
        for dependency in dependencies:
            target = names.get(dependency.target_service_id)
            source = names.get(dependency.source_service_id, context.service)
            if target is None or (dependency.valid_until and dependency.valid_until < start):
                continue
            candidates.append(
                EvidenceCandidate(
                    source="opsmind.service_dependencies",
                    timestamp=context.detected_at,
                    service=source,
                    evidence_type="service_dependency",
                    raw_reference=f"service-dependency://{dependency.id}",
                    normalized_summary=redact_evidence_summary(
                        f"{source} depends on {target} ({dependency.dependency_type})"
                    ),
                    confidence=1.0,
                    attributes={
                        "dependency_service": target,
                        "valid_from": _utc(dependency.valid_from).isoformat(),
                        "valid_until": (
                            _utc(dependency.valid_until).isoformat()
                            if dependency.valid_until
                            else None
                        ),
                    },
                )
            )

        logs = session.scalars(
            select(LogRecordRow)
            .where(
                LogRecordRow.tenant_id == context.tenant_id,
                LogRecordRow.service_id.in_(service_ids),
                LogRecordRow.observed_at >= start,
                LogRecordRow.observed_at <= end,
            )
            .limit(5000)
        ).all()
        for record in logs:
            service = (
                names.get(record.service_id, context.service)
                if record.service_id
                else context.service
            )
            event_kind = str(record.attributes.get("event_type", record.attributes.get("kind", "")))
            evidence_type = _event_evidence_type(event_kind) or "log"
            normalized = str(record.attributes.get("summary") or record.message)
            candidates.append(
                EvidenceCandidate(
                    source=record.source,
                    timestamp=_utc(record.observed_at),
                    service=service,
                    evidence_type=evidence_type,
                    raw_reference=record.external_ref or f"log-record://{record.id}",
                    normalized_summary=redact_evidence_summary(normalized),
                    confidence=0.95,
                    attributes={"level": record.severity, "trace_id": record.trace_id},
                )
            )

        metrics = session.scalars(
            select(MetricSnapshotRow)
            .where(
                MetricSnapshotRow.tenant_id == context.tenant_id,
                MetricSnapshotRow.service_id.in_(service_ids),
                MetricSnapshotRow.observed_at >= start,
                MetricSnapshotRow.observed_at <= end,
            )
            .limit(5000)
        ).all()
        for metric in metrics:
            service = (
                names.get(metric.service_id, context.service)
                if metric.service_id
                else context.service
            )
            candidates.append(
                EvidenceCandidate(
                    source=metric.source,
                    timestamp=_utc(metric.observed_at),
                    service=service,
                    evidence_type="metric_anomaly",
                    raw_reference=f"metric-snapshot://{metric.id}",
                    normalized_summary=redact_evidence_summary(
                        f"{metric.metric_name}={metric.value:g}"
                    ),
                    confidence=0.9,
                    attributes={"metric": metric.metric_name, "value": metric.value},
                )
            )
        return candidates


def _event_evidence_type(event_type: str) -> str | None:
    lowered = event_type.lower()
    categories = (
        ("anomal", "anomaly"),
        ("deploy", "deployment"),
        ("config", "configuration_change"),
        ("restart", "pod_restart"),
        ("infrastructure", "infrastructure_event"),
        ("infra", "infrastructure_event"),
    )
    return next((category for marker, category in categories if marker in lowered), None)


def redact_evidence_summary(value: str) -> str:
    """Remove common credential forms while retaining source references and context."""
    redacted = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [REDACTED]",
        value,
    )
    redacted = re.sub(
        r"(?i)(authorization|password|passwd|token|secret|api[_-]?key)\s*[:=]\s*"
        r"([^\s,;]+)",
        r"\1=[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"://[^:/\s]+:[^@/\s]+@", "://[REDACTED]@", redacted)
    redacted = re.sub(
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
        "[REDACTED_JWT]",
        redacted,
    )
    return redacted[:2000]


def redact_evidence_reference(value: str) -> str:
    """Remove embedded credentials from otherwise useful raw source references."""
    reference = re.sub(r"://[^:/\s]+:[^@/\s]+@", "://[REDACTED]@", value)
    return re.sub(
        r"(?i)([?&](?:authorization|password|passwd|token|secret|api[_-]?key)=)[^&]+",
        r"\1[REDACTED]",
        reference,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
