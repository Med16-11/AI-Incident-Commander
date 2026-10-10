"""OpenTelemetry tracing and Prometheus HTTP instrumentation."""

import time
from contextvars import ContextVar
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import Engine

current_metrics: ContextVar["ServiceMetrics | None"] = ContextVar("shopstack_metrics", default=None)


class ServiceMetrics:
    """Low-cardinality service request metrics with an isolated registry."""

    def __init__(self) -> None:
        self.service_name = "unknown"
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "http_requests_total",
            "HTTP requests handled by this ShopStack service",
            ("service", "method", "route", "status"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            ("service", "method", "route"),
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "http_in_flight_requests",
            "HTTP requests currently being handled",
            ("service",),
            registry=self.registry,
        )
        self.dependency_requests = Counter(
            "dependency_requests_total",
            "ShopStack dependency calls",
            ("service", "dependency", "operation", "outcome"),
            registry=self.registry,
        )
        self.dependency_duration = Histogram(
            "dependency_request_duration_seconds",
            "ShopStack dependency latency",
            ("service", "dependency", "operation", "outcome"),
            registry=self.registry,
        )
        self.db_checked_out = Gauge(
            "database_connections_checked_out",
            "Checked out database connections",
            ("service",),
            registry=self.registry,
        )
        self.db_pool_size = Gauge(
            "database_pool_size", "Database pool size", ("service",), registry=self.registry
        )
        self.db_overflow = Gauge(
            "database_pool_overflow", "Database pool overflow", ("service",), registry=self.registry
        )
        self.redis_duration = Histogram(
            "redis_operation_duration_seconds",
            "Redis operation latency",
            ("service", "operation", "outcome"),
            registry=self.registry,
        )
        self.redis_failures = Counter(
            "redis_operation_failures_total",
            "Failed Redis operations",
            ("service", "operation"),
            registry=self.registry,
        )

    def instrument_engine(self, engine: Engine, service: str) -> None:
        from sqlalchemy import event

        pool: Any = engine.pool
        self.db_pool_size.labels(service).set(pool.size())
        event.listen(pool, "checkout", lambda *_: self.db_checked_out.labels(service).inc())
        event.listen(pool, "checkin", lambda *_: self.db_checked_out.labels(service).dec())
        event.listen(
            pool,
            "connect",
            lambda *_: self.db_overflow.labels(service).set(max(0, pool.overflow())),
        )


def _observe_dependency(dependency: str, operation: str, start: float, outcome: str) -> None:
    metrics = current_metrics.get()
    if metrics is None:
        return
    labels = (metrics.service_name, dependency, operation, outcome)
    metrics.dependency_requests.labels(*labels).inc()
    metrics.dependency_duration.labels(*labels).observe(time.perf_counter() - start)


def configure_tracing(
    app: FastAPI,
    service_name: str,
    otel_endpoint: str | None,
    otel_insecure: bool,
    http_client: httpx.Client,
    engine: Engine | None,
) -> TracerProvider:
    """Instrument FastAPI and outbound HTTPX calls with an OTLP-capable provider."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otel_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint, insecure=otel_insecure))
        )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    HTTPXClientInstrumentor().instrument_client(http_client, tracer_provider=provider)
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=provider)
    return provider
