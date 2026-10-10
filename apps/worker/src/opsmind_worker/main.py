"""Redis event consumer that persists detection incidents into PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import cast
from uuid import uuid4

import structlog
from fastapi import FastAPI, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_common.logging import configure_logging
from opsmind_events.consumer import RedisStreamConsumer
from opsmind_events.metrics import EventMetrics
from opsmind_events.redis_client import RedisStreamsClient
from opsmind_observability.metrics import ServiceMetrics
from opsmind_storage.database import create_database_engine, create_session_factory
from opsmind_storage.event_inbox import SqlAlchemyEventUnitOfWorkFactory
from redis import Redis
from sqlalchemy import text
from starlette.types import ASGIApp

from opsmind_worker.incident_projection import IncidentProjectionHandler


def create_app(settings: Settings | None = None) -> ASGIApp:
    """Compose the worker consumer, transactional storage, and health endpoints."""
    configuration = settings or Settings()
    configure_logging(configuration)
    logger = structlog.get_logger(service="worker")
    engine = create_database_engine(configuration.database_url)
    sessions = create_session_factory(engine)
    redis_client = Redis.from_url(
        configuration.redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    metrics = ServiceMetrics("worker")
    event_metrics = EventMetrics(metrics.registry)
    consumer = RedisStreamConsumer(
        cast(RedisStreamsClient, redis_client),
        "opsmind:events",
        "opsmind-incident-projection",
        f"worker-{uuid4().hex[:12]}",
        IncidentProjectionHandler(metrics.incidents_created),
        SqlAlchemyEventUnitOfWorkFactory(sessions),
        metrics=event_metrics,
    )
    stopping = asyncio.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async def consume_forever() -> None:
            while not stopping.is_set():
                try:
                    await asyncio.to_thread(consumer.consume_once, block_ms=500, count=25)
                except Exception as exc:
                    logger.error("event_consumer_cycle_failed", error_type=type(exc).__name__)
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stopping.wait(), timeout=1.0)

        task = asyncio.create_task(consume_forever(), name="opsmind-incident-event-consumer")
        logger.info(
            "event_consumer_started",
            stream="opsmind:events",
            consumer_group="opsmind-incident-projection",
        )
        try:
            yield
        finally:
            stopping.set()
            await task
            redis_client.close()
            engine.dispose()
            logger.info("event_consumer_stopped")

    app = FastAPI(title="OpsMind Event Worker", version="0.1.0", lifespan=lifespan)
    app.state.consumer = consumer
    metrics.install(app)

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "worker"}

    @app.get("/health/ready", tags=["health"], response_model=None)
    async def readiness() -> Response | dict[str, str]:
        try:
            with sessions() as session:
                session.execute(text("SELECT 1"))
            redis_client.ping()
        except Exception:
            return Response(
                content='{"status":"not_ready","service":"worker"}',
                status_code=503,
                media_type="application/json",
            )
        return {"status": "ready", "service": "worker"}

    provider = TracerProvider(resource=Resource.create({"service.name": "worker"}))
    if configuration.otel_exporter_otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=configuration.otel_exporter_otlp_endpoint,
                    insecure=configuration.otel_exporter_otlp_insecure,
                )
            )
        )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    return CorrelationIdMiddleware(app)


app = create_app()
