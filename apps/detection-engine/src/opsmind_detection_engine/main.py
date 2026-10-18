"""Polling detection service that emits typed events through Go ingestion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import httpx
import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opsmind_clients import EventIngestionPublisher
from opsmind_common.config import Settings
from opsmind_common.correlation import CorrelationIdMiddleware
from opsmind_common.logging import configure_logging
from opsmind_observability.metrics import ServiceMetrics
from prometheus_client import Counter
from redis import Redis
from starlette.types import ASGIApp

from opsmind_detection_engine.detection import (
    DetectorSettings,
    DeterministicDetector,
    IncidentCorrelator,
    Threshold,
)
from opsmind_detection_engine.runner import DeduplicationStore, DetectionRunner
from opsmind_detection_engine.sources import LokiSource, PrometheusSource


def create_app(settings: Settings | None = None) -> ASGIApp:
    """Configure signal polling, deduplication, telemetry, and health endpoints."""
    configuration = settings or Settings()
    configure_logging(configuration)
    logger = structlog.get_logger(service="detection-engine")
    metrics = ServiceMetrics("detection-engine")
    cycles = Counter(
        "detection_cycles_total",
        "Completed deterministic detection polling cycles",
        registry=metrics.registry,
    )
    anomalies_published = Counter(
        "detection_anomalies_published_total",
        "Published metric and log anomaly events",
        ("kind",),
        registry=metrics.registry,
    )
    incidents_published = Counter(
        "detection_incidents_published_total",
        "Published correlated incident events",
        registry=metrics.registry,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if (
            configuration.event_ingestion_token is None
            or not configuration.event_ingestion_token.get_secret_value()
        ):
            raise RuntimeError("OPSMIND_EVENT_INGESTION_TOKEN is required by the detection engine")
        client = httpx.Client(timeout=5.0)
        redis_client = Redis.from_url(configuration.redis_url, decode_responses=True)
        detector_settings = DetectorSettings(
            thresholds={
                "error_rate": Threshold(maximum=configuration.detection_error_rate_threshold),
                "dependency_error_rate": Threshold(
                    maximum=configuration.detection_dependency_error_rate_threshold
                ),
                "latency_p95": Threshold(
                    maximum=configuration.detection_latency_p95_threshold_seconds
                ),
            },
            baseline_window=configuration.detection_baseline_window,
            baseline_min_samples=min(
                configuration.detection_baseline_min_samples,
                configuration.detection_baseline_window,
            ),
            baseline_deviation_ratio=configuration.detection_baseline_deviation_ratio,
            z_score_threshold=configuration.detection_z_score_threshold,
            traffic_drop_ratio=configuration.detection_traffic_drop_ratio,
            log_window_seconds=configuration.detection_log_window_seconds,
            log_occurrence_threshold=configuration.detection_log_occurrence_threshold,
            correlation_window_seconds=configuration.detection_correlation_window_seconds,
            deduplication_ttl_seconds=configuration.detection_deduplication_ttl_seconds,
        )
        runner = DetectionRunner(
            PrometheusSource(
                client,
                configuration.prometheus_url,
                job_regex=configuration.detection_prometheus_job_regex,
            ),
            LokiSource(client, configuration.loki_url),
            DeterministicDetector(detector_settings),
            IncidentCorrelator(detector_settings),
            EventIngestionPublisher(
                client,
                configuration.event_ingestion_url,
                configuration.event_ingestion_token.get_secret_value(),
            ),
            cast("DeduplicationStore", redis_client),
        )
        stopping = asyncio.Event()

        async def poll_forever() -> None:
            while not stopping.is_set():
                try:
                    anomaly_count, incident_count = await asyncio.to_thread(runner.run_once)
                    cycles.inc()
                    anomalies_published.labels("metric_or_log").inc(anomaly_count)
                    incidents_published.inc(incident_count)
                except Exception as exc:
                    logger.error("detection_cycle_failed", error_type=type(exc).__name__)
                try:
                    await asyncio.wait_for(
                        stopping.wait(), timeout=configuration.detection_poll_interval_seconds
                    )
                except TimeoutError:
                    continue

        task = asyncio.create_task(poll_forever(), name="opsmind-detection-poller")
        logger.info(
            "detection_poller_started",
            prometheus_url=configuration.prometheus_url,
            loki_url=configuration.loki_url,
            poll_interval_seconds=configuration.detection_poll_interval_seconds,
        )
        try:
            yield
        finally:
            stopping.set()
            await task
            client.close()
            redis_client.close()
            logger.info("detection_poller_stopped")

    app = FastAPI(title="OpsMind Detection Engine", version="0.1.0", lifespan=lifespan)
    metrics.install(app)

    @app.get("/health/live", tags=["health"])
    async def liveness() -> dict[str, str]:
        return {"status": "ok", "service": "detection-engine"}

    @app.get("/health/ready", tags=["health"])
    async def readiness() -> dict[str, str]:
        return {"status": "ready", "service": "detection-engine"}

    provider = TracerProvider(resource=Resource.create({"service.name": "detection-engine"}))
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
