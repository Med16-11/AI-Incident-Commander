"""Incident correlation service composition root and internal trigger endpoint."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx
import structlog
from fastapi import FastAPI, Header, HTTPException, Request, status
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_common.logging import configure_logging
from opsmind_observability.metrics import ServiceMetrics
from opsmind_storage.database import create_database_engine, create_session_factory
from opsmind_storage.models import IncidentRow, ServiceDependencyRow, ServiceRow
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from opsmind_incident_engine.correlation import (
    CorrelationWindow,
    DatabaseEvidenceSource,
    IncidentContext,
    IncidentCorrelationEngine,
)
from opsmind_incident_engine.investigation import (
    AIInvestigationEngine,
    IncidentAnalysis,
    InvestigationFailure,
    InvestigationIncident,
    InvestigationTimeout,
    LLMProvider,
    Severity,
)
from opsmind_incident_engine.investigation_store import InvestigationRunStore
from opsmind_incident_engine.investigation_tools import DatabaseInvestigationTools
from opsmind_incident_engine.providers import OpenAICompatibleProvider
from opsmind_incident_engine.sources import LokiEvidenceSource, PrometheusEvidenceSource


class CorrelationResponse(BaseModel):
    """Summary of persisted source evidence from one correlation run."""

    incident_id: UUID
    evidence_count: int
    source_failures: list[str]
    window_start: datetime
    window_end: datetime
    collection_complete: bool


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    http_client: httpx.Client | None = None,
    llm_provider: LLMProvider | None = None,
) -> ASGIApp:
    """Create an injectable correlation service with a fail-closed internal API."""
    configuration = settings or Settings()
    configure_logging(configuration)
    logger = structlog.get_logger(service="incident-engine")
    engine = create_database_engine(configuration.database_url) if session_factory is None else None
    sessions = session_factory or create_session_factory(engine)  # type: ignore[arg-type]
    owned_http_client = http_client is None
    client = http_client or httpx.Client(timeout=10.0)
    ai_provider = llm_provider
    llm_key = configuration.llm_api_key.get_secret_value() if configuration.llm_api_key else ""
    if (
        ai_provider is None
        and configuration.llm_base_url
        and configuration.llm_model
        and llm_key.strip()
    ):
        ai_provider = OpenAICompatibleProvider(
            client,
            base_url=configuration.llm_base_url,
            api_key=llm_key,
            model=configuration.llm_model,
            timeout_seconds=configuration.llm_timeout_seconds,
            allow_insecure_http=configuration.environment in {"local", "dev"},
        )
    metrics = ServiceMetrics("incident-engine")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owned_http_client:
            client.close()
        if engine is not None:
            engine.dispose()

    app = FastAPI(
        title="OpsMind Incident Correlation Engine",
        description=(
            "Deterministically correlates source-backed events into an auditable incident "
            "timeline and can run evidence-grounded AI investigations through an explicit "
            "read-only tool allowlist."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    metrics.install(app)

    @app.exception_handler(Exception)
    async def correlation_error_handler(request: Request, exc: Exception) -> JSONResponse:
        error_status = 503 if isinstance(exc, SQLAlchemyError) else 500
        error_code = "storage_unavailable" if error_status == 503 else "internal_error"
        logger.error(
            "incident_correlation_request_failed",
            error_type=type(exc).__name__,
            status_code=error_status,
        )
        return JSONResponse(
            status_code=error_status,
            content={
                "error": {
                    "code": error_code,
                    "message": "Incident correlation could not be completed",
                    "correlation_id": getattr(request.state, "correlation_id", "unknown"),
                }
            },
        )

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "incident-engine"}

    @app.get("/health/ready", tags=["health"], response_model=None)
    def readiness() -> Response | dict[str, str]:
        try:
            with sessions() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return Response(
                content='{"status":"not_ready","service":"incident-engine"}',
                status_code=503,
                media_type="application/json",
            )
        return {"status": "ready", "service": "incident-engine"}

    @app.post(
        "/incidents/{incident_id}/correlate",
        response_model=CorrelationResponse,
        status_code=status.HTTP_200_OK,
        tags=["correlation"],
        summary="Collect and persist incident evidence",
        description=(
            "Internal service-to-service operation. Requires the configured bearer token. "
            "All timeline entries are backed by a persisted evidence record and source reference."
        ),
    )
    def correlate_incident(
        incident_id: UUID,
        authorization: str | None = Header(default=None),
    ) -> CorrelationResponse:
        expected_token = configuration.correlation_api_token
        if expected_token is None:
            raise HTTPException(503, "Incident correlation trigger is not configured")
        auth_parts = authorization.split(maxsplit=1) if authorization else []
        supplied_token = (
            auth_parts[1] if len(auth_parts) == 2 and auth_parts[0].casefold() == "bearer" else ""
        )
        if not secrets.compare_digest(supplied_token, expected_token.get_secret_value()):
            raise HTTPException(401, "Invalid service credentials")

        with sessions() as session:
            incident = session.scalar(select(IncidentRow).where(IncidentRow.id == incident_id))
            if incident is None:
                raise HTTPException(404, "Incident was not found")
            service = (
                session.scalar(
                    select(ServiceRow).where(
                        ServiceRow.id == incident.service_id,
                        ServiceRow.tenant_id == incident.tenant_id,
                    )
                )
                if incident.service_id
                else None
            )
            service_name = service.name if service is not None else "unknown"
            dependencies: tuple[str, ...] = ()
            if service is not None:
                dependency_rows = session.scalars(
                    select(ServiceRow.name)
                    .join(
                        ServiceDependencyRow,
                        ServiceDependencyRow.target_service_id == ServiceRow.id,
                    )
                    .where(
                        ServiceRow.tenant_id == incident.tenant_id,
                        ServiceRow.environment == service.environment,
                        ServiceDependencyRow.tenant_id == incident.tenant_id,
                        ServiceDependencyRow.source_service_id == service.id,
                        ServiceDependencyRow.valid_from <= incident.detected_at,
                        (
                            ServiceDependencyRow.valid_until.is_(None)
                            | (ServiceDependencyRow.valid_until >= incident.detected_at)
                        ),
                    )
                ).all()
                dependencies = tuple(sorted(set(dependency_rows)))

            context = IncidentContext(
                incident_id=incident.id,
                tenant_id=incident.tenant_id,
                service=service_name,
                environment=service.environment if service is not None else None,
                detected_at=(
                    incident.detected_at.replace(tzinfo=UTC)
                    if incident.detected_at.tzinfo is None
                    else incident.detected_at
                ),
                dependencies=dependencies,
            )
            correlation = IncidentCorrelationEngine(
                sources=(
                    DatabaseEvidenceSource(sessions),
                    PrometheusEvidenceSource(
                        client,
                        configuration.prometheus_url,
                        error_rate_threshold=configuration.detection_error_rate_threshold,
                        latency_p95_threshold_seconds=(
                            configuration.detection_latency_p95_threshold_seconds
                        ),
                        dependency_error_rate_threshold=(
                            configuration.detection_dependency_error_rate_threshold
                        ),
                    ),
                    LokiEvidenceSource(client, configuration.loki_url),
                ),
                window=CorrelationWindow(
                    before_seconds=configuration.correlation_before_seconds,
                    after_seconds=configuration.correlation_after_seconds,
                ),
            )
        result = correlation.collect(context)
        try:
            with sessions() as session, session.begin():
                correlation.persist(session, context, result)
        except LookupError as exc:
            raise HTTPException(404, "Incident was not found") from exc

        start, end = correlation.window.bounds(context.detected_at)
        logger.info(
            "incident_correlation_completed",
            incident_id=str(incident_id),
            evidence_count=len(result.evidence),
            source_failures=list(result.source_failures),
        )
        return CorrelationResponse(
            incident_id=incident_id,
            evidence_count=len(result.evidence),
            source_failures=list(result.source_failures),
            window_start=start,
            window_end=end,
            collection_complete=datetime.now(UTC) >= end,
        )

    @app.post(
        "/incidents/{incident_id}/investigate",
        response_model=IncidentAnalysis,
        tags=["investigation"],
        summary="Investigate an incident with evidence-backed read tools",
        description=(
            "Internal service-to-service endpoint. The model can invoke only the eight "
            "documented read tools. Every supported claim is validated against evidence "
            "persisted by those tools. Requires an investigation bearer token and configured LLM."
        ),
    )
    def investigate_incident(
        incident_id: UUID,
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> IncidentAnalysis:
        expected_token = configuration.investigation_api_token
        if expected_token is None:
            raise HTTPException(503, "Incident investigation trigger is not configured")
        auth_parts = authorization.split(maxsplit=1) if authorization else []
        supplied_token = (
            auth_parts[1] if len(auth_parts) == 2 and auth_parts[0].casefold() == "bearer" else ""
        )
        if not secrets.compare_digest(supplied_token, expected_token.get_secret_value()):
            raise HTTPException(401, "Invalid service credentials")
        if ai_provider is None:
            raise HTTPException(503, "LLM provider is not configured")

        with sessions() as session:
            incident = session.scalar(select(IncidentRow).where(IncidentRow.id == incident_id))
            if incident is None:
                raise HTTPException(404, "Incident was not found")
            service = (
                session.scalar(
                    select(ServiceRow).where(
                        ServiceRow.id == incident.service_id,
                        ServiceRow.tenant_id == incident.tenant_id,
                    )
                )
                if incident.service_id
                else None
            )
            context = InvestigationIncident(
                incident_id=incident.id,
                title=incident.title,
                description=incident.description,
                severity=cast(Severity, incident.severity),
                state=incident.state,
                service_id=incident.service_id,
                service=service.name if service is not None else None,
                environment=service.environment if service is not None else None,
                detected_at=(
                    incident.detected_at.replace(tzinfo=UTC).isoformat()
                    if incident.detected_at.tzinfo is None
                    else incident.detected_at.isoformat()
                ),
            )

        tools = DatabaseInvestigationTools(
            sessions,
            client,
            prometheus_url=configuration.prometheus_url,
            loki_url=configuration.loki_url,
            after_seconds=configuration.correlation_after_seconds,
        )
        correlation_id = str(request.scope.get("state", {}).get("correlation_id", uuid4()))
        run_id = uuid4()
        run_store = InvestigationRunStore(
            sessions,
            run_id=run_id,
            tenant_id=incident.tenant_id,
            incident_id=incident_id,
            correlation_id=correlation_id,
        )
        run_store.create()
        engine = AIInvestigationEngine(
            ai_provider,
            tools.registry,
            max_tool_calls=configuration.investigation_max_tool_calls,
            max_execution_seconds=configuration.investigation_max_execution_seconds,
            tool_timeout_seconds=configuration.investigation_tool_timeout_seconds,
            max_context_chars=configuration.investigation_max_context_chars,
            confidence_threshold=configuration.investigation_confidence_threshold,
            observer=run_store,
        )
        try:
            analysis = engine.investigate(context)
        except InvestigationFailure as exc:
            run_store.finish("TIMED_OUT" if isinstance(exc, InvestigationTimeout) else "FAILED")
            logger.error(
                "incident_investigation_failed",
                incident_id=str(incident_id),
                investigation_run_id=str(run_id),
                error_type=type(exc).__name__,
            )
            error_status = 504 if isinstance(exc, InvestigationTimeout) else 502
            raise HTTPException(error_status, "Evidence-grounded investigation failed") from exc
        except Exception as exc:
            run_store.finish("FAILED")
            logger.error(
                "incident_investigation_failed",
                incident_id=str(incident_id),
                investigation_run_id=str(run_id),
                error_type=type(exc).__name__,
            )
            raise
        run_store.finish("COMPLETED")
        response.headers["X-Investigation-Run-ID"] = str(run_id)
        logger.info(
            "incident_investigation_completed",
            incident_id=str(incident_id),
            investigation_run_id=str(run_id),
            tool_call_count=len(analysis.investigation_steps),
            evidence_count=len(analysis.supporting_evidence),
            confidence=analysis.confidence,
        )
        return analysis

    tracer_provider = TracerProvider(resource=Resource.create({"service.name": "incident-engine"}))
    if configuration.otel_exporter_otlp_endpoint:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=configuration.otel_exporter_otlp_endpoint,
                    insecure=configuration.otel_exporter_otlp_insecure,
                )
            )
        )
    trace.set_tracer_provider(tracer_provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)
    return CorrelationIdMiddleware(app)


app = create_app()
