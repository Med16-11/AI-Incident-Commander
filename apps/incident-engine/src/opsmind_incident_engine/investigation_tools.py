"""Tenant-scoped read tools used by the AI investigation workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
from opsmind_models.domain.topology import ServiceDescriptor, ServiceTopology, TopologyEdge
from opsmind_storage.models import (
    DeploymentRow,
    IncidentEvidenceRow,
    IncidentRow,
    IncidentTimelineRow,
    LogRecordRow,
    ServiceDependencyRow,
    ServiceRow,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from opsmind_incident_engine.correlation import (
    EvidenceCandidate,
    IncidentContext,
    _utc,
    redact_evidence_summary,
)
from opsmind_incident_engine.historical import (
    HistoricalIncidentRetriever,
    PostgreSQLFullTextHistoricalRetriever,
)
from opsmind_incident_engine.investigation import (
    InvestigationEvidence,
    InvestigationIncident,
    InvestigationToolBackend,
    InvestigationToolRegistry,
    ToolName,
    ToolResult,
)
from opsmind_incident_engine.sources import LokiEvidenceSource


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TimelineSearchArgs(ToolArguments):
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=100, ge=1, le=100)


class LogSearchArgs(ToolArguments):
    service: str | None = Field(default=None, max_length=200)
    query: str | None = Field(default=None, max_length=200)
    window_minutes: int = Field(default=60, ge=1, le=1440)
    limit: int = Field(default=100, ge=1, le=500)


MetricName = Literal[
    "http_error_rate",
    "request_latency_p95_seconds",
    "request_rate",
    "dependency_error_rate",
]


class MetricQueryArgs(ToolArguments):
    service: str = Field(min_length=1, max_length=200)
    metric: MetricName
    window_minutes: int = Field(default=5, ge=1, le=60)


class ServiceArgs(ToolArguments):
    service: str = Field(min_length=1, max_length=200)


class DeploymentSearchArgs(ServiceArgs):
    lookback_hours: int = Field(default=168, ge=1, le=720)
    limit: int = Field(default=50, ge=1, le=100)


class PreviousIncidentArgs(ServiceArgs):
    query: str | None = Field(default=None, max_length=2000)
    limit: int = Field(default=10, ge=1, le=50)


class RunbookSearchArgs(ServiceArgs):
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


class DatabaseInvestigationTools(InvestigationToolBackend):
    """Concrete read-only tool implementations; the model receives no session or SQL."""

    descriptions: dict[ToolName, str] = {
        "search_incident_timeline": "Search this incident's persisted timeline and evidence.",
        "search_logs": (
            "Search tenant-scoped persisted logs and Loki for a service and bounded time window."
        ),
        "query_metrics": "Query one of the supported predefined service metrics from Prometheus.",
        "get_service_dependencies": "Read registered direct and transitive service dependencies.",
        "get_recent_deployments": "List recent persisted deployments for a service.",
        "get_service_health": "Read the current Prometheus target health for a service.",
        "get_previous_incidents": (
            "Find similar resolved historical incidents in this tenant. Results are past context, "
            "never current evidence. Supply a concise symptom query."
        ),
        "get_runbooks": "Search runbooks explicitly registered in service metadata.",
    }

    argument_models: dict[ToolName, type[BaseModel]] = {
        "search_incident_timeline": TimelineSearchArgs,
        "search_logs": LogSearchArgs,
        "query_metrics": MetricQueryArgs,
        "get_service_dependencies": ServiceArgs,
        "get_recent_deployments": DeploymentSearchArgs,
        "get_service_health": ServiceArgs,
        "get_previous_incidents": PreviousIncidentArgs,
        "get_runbooks": RunbookSearchArgs,
    }

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: httpx.Client,
        *,
        prometheus_url: str,
        loki_url: str,
        after_seconds: int = 300,
        historical_retriever: HistoricalIncidentRetriever | None = None,
    ) -> None:
        self._sessions = session_factory
        self._client = client
        self._prometheus_url = f"{prometheus_url.rstrip('/')}/api/v1/query"
        self._loki_url = loki_url
        self._after_seconds = after_seconds
        self._historical_retriever = historical_retriever or PostgreSQLFullTextHistoricalRetriever()
        self.handlers = {
            "search_incident_timeline": lambda args, incident: self.search_incident_timeline(
                TimelineSearchArgs.model_validate(args.model_dump()), incident
            ),
            "search_logs": lambda args, incident: self.search_logs(
                LogSearchArgs.model_validate(args.model_dump()), incident
            ),
            "query_metrics": lambda args, incident: self.query_metrics(
                MetricQueryArgs.model_validate(args.model_dump()), incident
            ),
            "get_service_dependencies": lambda args, incident: self.get_service_dependencies(
                ServiceArgs.model_validate(args.model_dump()), incident
            ),
            "get_recent_deployments": lambda args, incident: self.get_recent_deployments(
                DeploymentSearchArgs.model_validate(args.model_dump()), incident
            ),
            "get_service_health": lambda args, incident: self.get_service_health(
                ServiceArgs.model_validate(args.model_dump()), incident
            ),
            "get_previous_incidents": lambda args, incident: self.get_previous_incidents(
                PreviousIncidentArgs.model_validate(args.model_dump()), incident
            ),
            "get_runbooks": lambda args, incident: self.get_runbooks(
                RunbookSearchArgs.model_validate(args.model_dump()), incident
            ),
        }
        self.registry = InvestigationToolRegistry(self)

    def search_incident_timeline(
        self, args: TimelineSearchArgs, incident: InvestigationIncident
    ) -> ToolResult:
        with self._sessions() as session:
            tenant_id = self._tenant(session, incident)
            timeline_rows = session.scalars(
                select(IncidentTimelineRow)
                .where(
                    IncidentTimelineRow.tenant_id == tenant_id,
                    IncidentTimelineRow.incident_id == incident.incident_id,
                    IncidentTimelineRow.evidence_id.is_(None),
                )
                .order_by(IncidentTimelineRow.sequence)
                .limit(args.limit)
            ).all()
            timeline_candidates = [
                EvidenceCandidate(
                    source="opsmind.incident_timeline",
                    timestamp=_utc(row.occurred_at),
                    service=incident.service or "unknown",
                    evidence_type=row.event_type,
                    raw_reference=f"incident-timeline://{row.id}",
                    normalized_summary=redact_evidence_summary(row.summary),
                    confidence=1.0,
                    attributes={"timeline_id": str(row.id), "sequence": row.sequence},
                )
                for row in timeline_rows
            ]
        if timeline_candidates:
            self._persist_candidates(incident, timeline_candidates)
        with self._sessions() as session:
            rows = session.scalars(
                select(IncidentEvidenceRow)
                .where(
                    IncidentEvidenceRow.tenant_id == tenant_id,
                    IncidentEvidenceRow.incident_id == incident.incident_id,
                )
                .order_by(IncidentEvidenceRow.observed_at.desc())
                .limit(args.limit)
            ).all()
            evidence = [self._evidence_from_row(row) for row in rows]
            if args.query:
                needle = args.query.casefold()
                evidence = [item for item in evidence if needle in item.summary.casefold()]
            warnings = ["Incident timeline is a point-in-time view and may still be incomplete."]
            if not evidence:
                warnings.append("No matching timeline evidence was found.")
            return ToolResult(evidence=evidence, warnings=warnings)

    def search_logs(self, args: LogSearchArgs, incident: InvestigationIncident) -> ToolResult:
        warnings: list[str] = []
        candidates: list[EvidenceCandidate] = []
        with self._sessions() as session:
            tenant_id = self._tenant(session, incident)
            service = self._resolve_service(session, tenant_id, args.service, incident)
            if service is None:
                return ToolResult(
                    evidence=[], warnings=["No matching tenant service is registered."]
                )
            start, end = self._bounds(incident, args.window_minutes * 60)
            query = select(LogRecordRow).where(
                LogRecordRow.tenant_id == tenant_id,
                LogRecordRow.observed_at >= start,
                LogRecordRow.observed_at <= end,
                LogRecordRow.service_id == service.id,
            )
            if args.query:
                query = query.where(LogRecordRow.message.ilike(f"%{args.query}%"))
            rows = session.scalars(
                query.order_by(LogRecordRow.observed_at.desc()).limit(args.limit)
            ).all()
            for row in rows:
                candidates.append(
                    EvidenceCandidate(
                        source=row.source,
                        timestamp=_utc(row.observed_at),
                        service=service.name,
                        evidence_type="log",
                        raw_reference=row.external_ref or f"log-record://{row.id}",
                        normalized_summary=redact_evidence_summary(
                            f"{row.severity}: {row.message}"
                        ),
                        confidence=0.95,
                        attributes={"severity": row.severity, "trace_id": row.trace_id},
                    )
                )
            service_context = self._incident_context(session, incident, service)

        try:
            loki = LokiEvidenceSource(self._client, self._loki_url, limit=args.limit)
            candidates.extend(
                item
                for item in loki.collect(service_context, start, end)
                if not args.query or args.query.casefold() in item.normalized_summary.casefold()
            )
        except (httpx.HTTPError, ValueError, RuntimeError, AttributeError):
            warnings.append("Loki log source is unavailable or returned invalid data.")
        evidence = self._persist_candidates(incident, candidates[: args.limit])
        if not evidence:
            warnings.append("No matching logs were found in the requested time window.")
        return ToolResult(evidence=evidence, warnings=warnings)

    def query_metrics(self, args: MetricQueryArgs, incident: InvestigationIncident) -> ToolResult:
        with self._sessions() as session:
            tenant_id = self._tenant(session, incident)
            service = self._resolve_service(session, tenant_id, args.service, incident)
        if service is None:
            return ToolResult(evidence=[], warnings=["No matching tenant service is registered."])
        expression = _metric_expression(args.metric, service.name, args.window_minutes)
        try:
            response = self._client.get(
                self._prometheus_url,
                params={"query": expression},
            )
            response.raise_for_status()
            document = response.json()
            if document.get("status") != "success":
                raise ValueError("unsuccessful Prometheus response")
            series = document.get("data", {}).get("result", [])
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return ToolResult(
                evidence=[], warnings=["Prometheus metrics source is unavailable or invalid."]
            )
        candidates: list[EvidenceCandidate] = []
        for item in series if isinstance(series, list) else []:
            if not isinstance(item, dict):
                continue
            labels = item.get("metric", {})
            sample = item.get("value")
            if not isinstance(labels, dict) or labels.get("service") != service.name:
                continue
            if not isinstance(sample, list) or len(sample) < 2:
                continue
            try:
                observed_at = datetime.fromtimestamp(float(sample[0]), UTC)
                value = float(sample[1])
            except (TypeError, ValueError, OverflowError):
                continue
            if value != value or value in {float("inf"), float("-inf")}:
                continue
            candidates.append(
                EvidenceCandidate(
                    source="prometheus",
                    timestamp=observed_at,
                    service=service.name,
                    evidence_type="metric_observation",
                    raw_reference="prometheus://query?" + urlencode({"query": expression}),
                    normalized_summary=redact_evidence_summary(
                        f"{args.metric}={value:g} over {args.window_minutes} minute(s)"
                    ),
                    confidence=0.95,
                    attributes={"metric": args.metric, "value": value},
                )
            )
        evidence = self._persist_candidates(incident, candidates)
        return ToolResult(
            evidence=evidence,
            warnings=[]
            if evidence
            else ["Prometheus returned no samples for this service and metric."],
        )

    def get_service_dependencies(
        self, args: ServiceArgs, incident: InvestigationIncident
    ) -> ToolResult:
        with self._sessions() as session:
            tenant_id = self._tenant(session, incident)
            service = self._resolve_service(session, tenant_id, args.service, incident)
            if service is None:
                return ToolResult(
                    evidence=[], warnings=["No matching tenant service is registered."]
                )
            services = session.scalars(
                select(ServiceRow).where(ServiceRow.tenant_id == tenant_id)
            ).all()
            now = datetime.now(UTC)
            edge_rows = session.scalars(
                select(ServiceDependencyRow).where(
                    ServiceDependencyRow.tenant_id == tenant_id,
                    ServiceDependencyRow.valid_from <= now,
                    or_(
                        ServiceDependencyRow.valid_until.is_(None),
                        ServiceDependencyRow.valid_until >= now,
                    ),
                )
            ).all()
            graph = ServiceTopology(
                [
                    ServiceDescriptor(
                        id=row.id,
                        tenant_id=row.tenant_id,
                        name=row.name,
                        environment=row.environment,
                    )
                    for row in services
                ],
                [TopologyEdge(row.source_service_id, row.target_service_id) for row in edge_rows],
            )
            result = graph.analyze(service.id)
            direct_dependencies = (
                ", ".join(item.name for item in result.direct_dependencies)
                or "no registered services"
            )
            upstream_services = ", ".join(item.name for item in result.upstream_services) or "none"
            downstream_services = (
                ", ".join(item.name for item in result.downstream_services) or "none"
            )
            summary = (
                f"{service.name} depends on "
                f"{direct_dependencies}; upstream callers: {upstream_services}; "
                f"downstream services: {downstream_services}"
            )
            candidate = EvidenceCandidate(
                source="opsmind.service_dependencies",
                timestamp=now,
                service=service.name,
                evidence_type="service_dependency_graph",
                raw_reference=f"service-topology://{tenant_id}/{service.id}",
                normalized_summary=redact_evidence_summary(summary),
                confidence=1.0,
                attributes={
                    "direct_dependencies": ",".join(
                        item.name for item in result.direct_dependencies
                    ),
                    "upstream_services": ",".join(item.name for item in result.upstream_services),
                    "downstream_services": ",".join(
                        item.name for item in result.downstream_services
                    ),
                    "critical_path": ",".join(item.name for item in result.critical_path),
                    "likely_blast_radius_count": len(result.blast_radius),
                },
            )
        return ToolResult(evidence=self._persist_candidates(incident, [candidate]), warnings=[])

    def get_recent_deployments(
        self, args: DeploymentSearchArgs, incident: InvestigationIncident
    ) -> ToolResult:
        with self._sessions() as session:
            tenant_id = self._tenant(session, incident)
            service = self._resolve_service(session, tenant_id, args.service, incident)
            if service is None:
                return ToolResult(
                    evidence=[], warnings=["No matching tenant service is registered."]
                )
            start = datetime.now(UTC) - timedelta(hours=args.lookback_hours)
            rows = session.scalars(
                select(DeploymentRow)
                .where(
                    DeploymentRow.tenant_id == tenant_id,
                    DeploymentRow.service_id == service.id,
                    DeploymentRow.started_at >= start,
                )
                .order_by(DeploymentRow.started_at.desc())
                .limit(args.limit)
            ).all()
            candidates = [
                EvidenceCandidate(
                    source="opsmind.deployments",
                    timestamp=_utc(row.started_at),
                    service=service.name,
                    evidence_type="deployment",
                    raw_reference=f"deployment://{row.id}",
                    normalized_summary=redact_evidence_summary(
                        f"Deployment {row.version} started with status {row.status}"
                    ),
                    confidence=1.0,
                    attributes={"version": row.version, "commit_sha": row.commit_sha},
                )
                for row in rows
            ]
        evidence = self._persist_candidates(incident, candidates)
        return ToolResult(
            evidence=evidence,
            warnings=[] if evidence else ["No deployments were found in the requested period."],
        )

    def get_service_health(self, args: ServiceArgs, incident: InvestigationIncident) -> ToolResult:
        with self._sessions() as session:
            service = self._resolve_service(
                session, self._tenant(session, incident), args.service, incident
            )
        if service is None:
            return ToolResult(evidence=[], warnings=["No matching tenant service is registered."])
        expression = f'up{{service="{_promql_string(service.name)}"}}'
        try:
            response = self._client.get(self._prometheus_url, params={"query": expression})
            response.raise_for_status()
            document = response.json()
            if document.get("status") != "success":
                raise ValueError("unsuccessful Prometheus response")
            series = document.get("data", {}).get("result", [])
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return ToolResult(
                evidence=[],
                warnings=["Prometheus service health source is unavailable or invalid."],
            )
        observed_at = datetime.now(UTC)
        candidates: list[EvidenceCandidate] = []
        for item in series if isinstance(series, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("value"), list):
                continue
            labels, sample = item.get("metric", {}), item["value"]
            if (
                not isinstance(labels, dict)
                or labels.get("service") != service.name
                or len(sample) < 2
            ):
                continue
            try:
                healthy = float(sample[1]) == 1.0
            except (TypeError, ValueError):
                continue
            candidates.append(
                EvidenceCandidate(
                    source="prometheus",
                    timestamp=observed_at,
                    service=service.name,
                    evidence_type="service_health",
                    raw_reference="prometheus://query?" + urlencode({"query": expression}),
                    normalized_summary=(
                        f"Prometheus target for {service.name} is "
                        f"{'healthy' if healthy else 'unhealthy'}"
                    ),
                    confidence=0.95,
                    attributes={"healthy": healthy},
                )
            )
        evidence = self._persist_candidates(incident, candidates)
        return ToolResult(
            evidence=evidence,
            warnings=[] if evidence else ["Prometheus has no health target for this service."],
        )

    def get_previous_incidents(
        self, args: PreviousIncidentArgs, incident: InvestigationIncident
    ) -> ToolResult:
        with self._sessions() as session:
            tenant_id = self._tenant(session, incident)
            service = self._resolve_service(session, tenant_id, args.service, incident)
            if service is None:
                return ToolResult(
                    evidence=[], warnings=["No matching tenant service is registered."]
                )
            query = args.query or " ".join(
                item for item in (incident.title, incident.description or "", service.name) if item
            )
            historical = self._historical_retriever.search(
                session, tenant_id, query, limit=args.limit
            )
            historical = [item for item in historical if item.incident_id != incident.incident_id]
        return ToolResult(
            evidence=[],
            historical_incidents=historical,
            warnings=[]
            if historical
            else ["No similar resolved historical incidents were found for this service."],
        )

    def get_runbooks(self, args: RunbookSearchArgs, incident: InvestigationIncident) -> ToolResult:
        with self._sessions() as session:
            tenant_id = self._tenant(session, incident)
            service = self._resolve_service(session, tenant_id, args.service, incident)
            if service is None:
                return ToolResult(
                    evidence=[], warnings=["No matching tenant service is registered."]
                )
            runbooks = service.metadata_json.get("runbooks", [])
            if not isinstance(runbooks, list):
                runbooks = []
            candidates: list[EvidenceCandidate] = []
            for entry in runbooks[: args.limit]:
                if isinstance(entry, str):
                    title, body, reference = entry, entry, f"runbook://{service.id}/{entry}"
                elif isinstance(entry, dict):
                    title = str(entry.get("title", "Runbook"))
                    body = str(entry.get("content", entry.get("summary", title)))
                    reference = str(entry.get("url", f"runbook://{service.id}/{title}"))
                else:
                    continue
                material = f"{title} {body}"
                if args.query and args.query.casefold() not in material.casefold():
                    continue
                candidates.append(
                    EvidenceCandidate(
                        source="opsmind.service_runbooks",
                        timestamp=datetime.now(UTC),
                        service=service.name,
                        evidence_type="runbook",
                        raw_reference=reference,
                        normalized_summary=redact_evidence_summary(f"{title}: {body}"),
                        confidence=1.0,
                    )
                )
        evidence = self._persist_candidates(incident, candidates)
        return ToolResult(
            evidence=evidence,
            warnings=[] if evidence else ["No matching runbooks are registered for this service."],
        )

    def _persist_candidates(
        self, incident: InvestigationIncident, candidates: list[EvidenceCandidate]
    ) -> list[InvestigationEvidence]:
        """Persist read-tool evidence and a linked timeline row before returning its ID."""
        if not candidates:
            return []
        evidence: list[InvestigationEvidence] = []
        with self._sessions() as session, session.begin():
            incident_row = session.scalar(
                select(IncidentRow).where(IncidentRow.id == incident.incident_id).with_for_update()
            )
            if incident_row is None:
                raise LookupError("incident was not found")
            sequence = int(
                session.scalar(
                    select(func.max(IncidentTimelineRow.sequence)).where(
                        IncidentTimelineRow.tenant_id == incident_row.tenant_id,
                        IncidentTimelineRow.incident_id == incident.incident_id,
                    )
                )
                or 0
            )
            for item in candidates:
                row = session.scalar(
                    select(IncidentEvidenceRow).where(
                        IncidentEvidenceRow.tenant_id == incident_row.tenant_id,
                        IncidentEvidenceRow.incident_id == incident.incident_id,
                        IncidentEvidenceRow.content_hash == item.content_hash,
                    )
                )
                if row is None:
                    row = IncidentEvidenceRow(
                        tenant_id=incident_row.tenant_id,
                        incident_id=incident.incident_id,
                        evidence_type=item.evidence_type,
                        source=item.source,
                        service=item.service,
                        normalized_summary=item.normalized_summary,
                        confidence=item.confidence,
                        external_ref=item.raw_reference,
                        content={"attributes": item.attributes},
                        content_hash=item.content_hash,
                        observed_at=item.timestamp,
                    )
                    session.add(row)
                    session.flush()
                    sequence += 1
                    session.add(
                        IncidentTimelineRow(
                            tenant_id=incident_row.tenant_id,
                            incident_id=incident.incident_id,
                            evidence_id=row.id,
                            sequence=sequence,
                            event_type=item.evidence_type,
                            summary=item.normalized_summary,
                            occurred_at=item.timestamp,
                        )
                    )
                evidence.append(self._evidence_from_row(row))
        return evidence

    @staticmethod
    def _evidence_from_row(row: IncidentEvidenceRow) -> InvestigationEvidence:
        return InvestigationEvidence(
            evidence_id=row.id,
            source=row.source,
            timestamp=_utc(row.observed_at).isoformat(),
            service=row.service,
            evidence_type=row.evidence_type,
            summary=row.normalized_summary,
            confidence=row.confidence,
        )

    @staticmethod
    def _tenant(session: Session, incident: InvestigationIncident) -> UUID:
        tenant_id = session.scalar(
            select(IncidentRow.tenant_id).where(IncidentRow.id == incident.incident_id)
        )
        if tenant_id is None:
            raise LookupError("incident was not found")
        return tenant_id

    @staticmethod
    def _resolve_service(
        session: Session,
        tenant_id: UUID,
        name: str | None,
        incident: InvestigationIncident,
    ) -> ServiceRow | None:
        service_id = incident.service_id if name is None else None
        query = select(ServiceRow).where(ServiceRow.tenant_id == tenant_id)
        if service_id is not None:
            query = query.where(ServiceRow.id == service_id)
            if incident.environment is not None:
                query = query.where(ServiceRow.environment == incident.environment)
        elif name is not None:
            query = query.where(ServiceRow.name == name)
            if incident.environment is not None:
                query = query.where(ServiceRow.environment == incident.environment)
        else:
            return None
        return session.scalar(query)

    def _incident_context(
        self, session: Session, incident: InvestigationIncident, service: ServiceRow
    ) -> IncidentContext:
        tenant_id = self._tenant(session, incident)
        detected_at = datetime.fromisoformat(incident.detected_at.replace("Z", "+00:00"))
        if detected_at.tzinfo is None:
            detected_at = detected_at.replace(tzinfo=UTC)
        dependencies = session.scalars(
            select(ServiceRow.name)
            .join(ServiceDependencyRow, ServiceDependencyRow.target_service_id == ServiceRow.id)
            .where(
                ServiceRow.tenant_id == tenant_id,
                ServiceRow.environment == service.environment,
                ServiceDependencyRow.tenant_id == tenant_id,
                ServiceDependencyRow.source_service_id == service.id,
                ServiceDependencyRow.valid_from <= detected_at,
                or_(
                    ServiceDependencyRow.valid_until.is_(None),
                    ServiceDependencyRow.valid_until >= detected_at,
                ),
            )
        ).all()
        return IncidentContext(
            incident_id=incident.incident_id,
            tenant_id=tenant_id,
            service=service.name,
            environment=service.environment,
            detected_at=detected_at,
            dependencies=tuple(sorted(set(dependencies))),
        )

    def _bounds(
        self, incident: InvestigationIncident, lookback_seconds: int
    ) -> tuple[datetime, datetime]:
        detected_at = datetime.fromisoformat(incident.detected_at.replace("Z", "+00:00"))
        if detected_at.tzinfo is None:
            detected_at = detected_at.replace(tzinfo=UTC)
        return detected_at - timedelta(seconds=lookback_seconds), detected_at + timedelta(
            seconds=self._after_seconds
        )


def _promql_string(value: str) -> str:
    """Escape a label value before embedding it in one of the fixed PromQL templates."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metric_expression(metric: MetricName, service: str, window_minutes: int) -> str:
    label = _promql_string(service)
    window = f"{window_minutes}m"
    expressions: dict[MetricName, str] = {
        "http_error_rate": (
            f"sum by (service) (rate(http_requests_total"
            f'{{service="{label}",status=~"5.."}}[{window}])) '
            f"/ clamp_min(sum by (service) (rate(http_requests_total"
            f'{{service="{label}"}}[{window}])), 0.001)'
        ),
        "request_latency_p95_seconds": (
            f"histogram_quantile(0.95, sum by (service, le) "
            f'(rate(http_request_duration_seconds_bucket{{service="{label}"}}[{window}])))'
        ),
        "request_rate": (
            f'sum by (service) (rate(http_requests_total{{service="{label}"}}[{window}]))'
        ),
        "dependency_error_rate": (
            f"sum by (service) (rate(dependency_requests_total"
            f'{{service="{label}",outcome="error"}}[{window}])) '
            f"/ clamp_min(sum by (service) (rate(dependency_requests_total"
            f'{{service="{label}"}}[{window}])), '
            "0.001)"
        ),
    }
    return expressions[metric]
