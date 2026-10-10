"""Shared FastAPI application factory for service skeletons."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opsmind_observability.metrics import ServiceMetrics
from starlette.types import ASGIApp

from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_common.logging import configure_logging


def create_service_app(service_name: str) -> ASGIApp:
    """Create a minimally configured service with liveness and readiness routes."""
    settings = Settings()
    configure_logging(settings)
    logger = structlog.get_logger(service=service_name)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("service_started", environment=settings.environment)
        yield
        logger.info("service_stopped")

    fastapi_app = FastAPI(title=f"OpsMind {service_name}", version="0.1.0", lifespan=lifespan)
    ServiceMetrics(service_name).install(fastapi_app)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.otel_exporter_otlp_endpoint,
                    insecure=settings.otel_exporter_otlp_insecure,
                )
            )
        )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(fastapi_app, tracer_provider=provider)

    @fastapi_app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @fastapi_app.get("/health/ready", tags=["health"])
    async def readiness() -> dict[str, str]:
        return {"status": "ready", "service": service_name}

    return CorrelationIdMiddleware(fastapi_app)
