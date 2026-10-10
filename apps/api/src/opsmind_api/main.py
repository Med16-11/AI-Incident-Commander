"""Incident management API composition root."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import structlog
from fastapi import FastAPI, Response
from fastapi.exceptions import RequestValidationError
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_common.logging import configure_logging
from opsmind_observability.metrics import ServiceMetrics
from opsmind_storage.database import create_database_engine, create_session_factory
from opsmind_storage.models import IncidentRow
from prometheus_client import Gauge
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from opsmind_api.auth import (
    AuthenticationBackend,
    AuthorizationPolicy,
    DenyAllAuthentication,
    LocalDemoAuthentication,
    PermissionAuthorization,
)
from opsmind_api.errors import (
    ApiError,
    api_error_handler,
    http_error_handler,
    internal_error_handler,
    validation_error_handler,
)
from opsmind_api.routes import router
from opsmind_api.service_routes import router as services_router


def create_app(
    *,
    session_factory: sessionmaker[Session] | None = None,
    authentication_backend: AuthenticationBackend | None = None,
    authorization_policy: AuthorizationPolicy | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build an API app with injectable persistence and identity interfaces."""
    configuration = settings or Settings()
    configure_logging(configuration)
    logger = structlog.get_logger(service="api")
    if session_factory is None:
        created_engine = create_database_engine(configuration.database_url)
        engine: Engine | None = created_engine
        sessions = create_session_factory(created_engine)
    else:
        engine = None
        sessions = session_factory

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("service_started", environment=configuration.environment)
        yield
        if engine is not None:
            engine.dispose()
        logger.info("service_stopped")

    app = FastAPI(
        title="OpsMind Incident Management API",
        description=(
            "Tenant-scoped incident lifecycle and evidence API. Authentication and "
            "authorization are injected through explicit interfaces."
        ),
        version="0.1.0",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "health", "description": "Service health and readiness."},
            {"name": "incidents", "description": "Incident lifecycle and investigation data."},
            {"name": "services", "description": "Service registry and dependency topology."},
        ],
    )
    app.state.session_factory = sessions
    metrics = ServiceMetrics("opsmind-api")
    current_incidents = Gauge(
        "opsmind_incidents_current",
        "Current durable incident records grouped by state",
        ("state",),
        registry=metrics.registry,
    )
    metrics.install(app)
    if engine is not None:
        metrics.instrument_engine(engine)
    tracer_provider = TracerProvider(resource=Resource.create({"service.name": "opsmind-api"}))
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
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=tracer_provider)

    @app.middleware("http")
    async def incident_process_metrics(request: Any, call_next: Any) -> Any:
        if request.url.path == "/metrics":
            try:
                with sessions() as session:
                    counts = session.execute(
                        select(IncidentRow.state, func.count(IncidentRow.id)).group_by(
                            IncidentRow.state
                        )
                    ).all()
                current_incidents.clear()
                for state, count in counts:
                    current_incidents.labels(str(state)).set(int(count))
            except SQLAlchemyError as exc:
                logger.warning("incident_metrics_refresh_failed", error_type=type(exc).__name__)
        response = await call_next(request)
        path = request.url.path
        if response.status_code < 400:
            if request.method == "POST" and path == "/incidents":
                if response.status_code == 201:
                    metrics.incidents_created.inc()
            elif request.method == "POST" and path.endswith("/close"):
                metrics.incidents_closed.inc()
        if "/remediations" in path and request.method in {"POST", "PATCH"}:
            action = path.rsplit("/", 1)[-1]
            outcome = "success" if response.status_code < 400 else "error"
            metrics.remediation_actions.labels(action, outcome).inc()
        return response

    configured_token = (
        configuration.local_demo_token.get_secret_value().strip()
        if configuration.local_demo_token is not None
        else ""
    )
    if authentication_backend is not None:
        app.state.authentication_backend = authentication_backend
    elif (
        configuration.environment.casefold() in {"local", "dev"}
        and configuration.local_demo_auth_enabled
        and configured_token
    ):
        app.state.authentication_backend = LocalDemoAuthentication(configured_token)
    else:
        app.state.authentication_backend = DenyAllAuthentication()
    app.state.authorization_policy = authorization_policy or PermissionAuthorization()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(ApiError, cast(Any, api_error_handler))
    app.add_exception_handler(StarletteHTTPException, cast(Any, http_error_handler))
    app.add_exception_handler(Exception, cast(Any, internal_error_handler))
    app.add_exception_handler(RequestValidationError, cast(Any, validation_error_handler))

    @app.get("/health/live", tags=["health"], summary="Liveness probe")
    def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "api"}

    @app.get("/health/ready", tags=["health"], summary="Readiness probe", response_model=None)
    def readiness() -> Response | dict[str, str]:
        try:
            with sessions() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return Response(
                content='{"status":"not_ready","service":"api"}',
                status_code=503,
                media_type="application/json",
            )
        return {"status": "ready", "service": "api"}

    app.include_router(router)
    app.include_router(services_router)
    return app


app = create_app()
