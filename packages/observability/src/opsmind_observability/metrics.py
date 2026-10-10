"""Prometheus metrics shared by OpsMind services."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


class ServiceMetrics:
    """Low-cardinality HTTP and dependency metrics for one service registry."""

    def __init__(self, service: str, registry: CollectorRegistry | None = None) -> None:
        self.service = service
        self.registry = registry or CollectorRegistry()
        labels = ("service", "method", "route", "status")
        self.http_requests = Counter(
            "http_requests_total", "HTTP requests", labels, registry=self.registry
        )
        self.http_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration",
            labels[:-1],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.http_in_flight = Gauge(
            "http_in_flight_requests",
            "In-flight HTTP requests",
            ("service",),
            registry=self.registry,
        )
        dep_labels = ("service", "dependency", "operation", "outcome")
        self.dependency_requests = Counter(
            "dependency_requests_total", "Dependency calls", dep_labels, registry=self.registry
        )
        self.dependency_duration = Histogram(
            "dependency_request_duration_seconds",
            "Dependency call duration",
            dep_labels,
            registry=self.registry,
        )
        self.db_checked_out = Gauge(
            "database_connections_checked_out",
            "Checked out SQLAlchemy pool connections",
            ("service",),
            registry=self.registry,
        )
        self.db_pool_size = Gauge(
            "database_pool_size",
            "Configured SQLAlchemy pool size",
            ("service",),
            registry=self.registry,
        )
        self.db_overflow = Gauge(
            "database_pool_overflow",
            "SQLAlchemy pool overflow connections",
            ("service",),
            registry=self.registry,
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
        self.incidents_created = Counter(
            "opsmind_incidents_created_total",
            "Incident records created by supported application write paths",
            registry=self.registry,
        )
        self.incidents_closed = Counter(
            "opsmind_incidents_closed_total",
            "Incidents closed through the API",
            registry=self.registry,
        )
        self.remediation_actions = Counter(
            "opsmind_remediation_actions_total",
            "Remediation API actions",
            ("action", "outcome"),
            registry=self.registry,
        )

    def install(self, app: FastAPI) -> None:
        """Install HTTP metrics and a Prometheus scrape endpoint."""

        @app.middleware("http")
        async def metrics_middleware(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if request.url.path == "/metrics":
                return await call_next(request)
            start = time.perf_counter()
            self.http_in_flight.labels(self.service).inc()
            status = "500"
            try:
                response = await call_next(request)
                status = str(response.status_code)
                return response
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                elapsed = time.perf_counter() - start
                self.http_requests.labels(self.service, request.method, route, status).inc()
                self.http_duration.labels(self.service, request.method, route).observe(elapsed)
                self.http_in_flight.labels(self.service).dec()

        @app.get("/metrics", include_in_schema=False)
        def metrics_endpoint() -> Response:
            return Response(generate_latest(self.registry), media_type=CONTENT_TYPE_LATEST)

    def instrument_engine(self, engine: Any) -> None:
        """Track SQLAlchemy pool checkout/checkin and pool capacity."""
        from sqlalchemy import event

        pool = engine.pool
        self.db_pool_size.labels(self.service).set(pool.size())
        event.listen(pool, "checkout", lambda *_: self.db_checked_out.labels(self.service).inc())
        event.listen(pool, "checkin", lambda *_: self.db_checked_out.labels(self.service).dec())
        event.listen(pool, "connect", lambda *_: self._update_pool(pool))
        event.listen(pool, "close", lambda *_: self._update_pool(pool))
        event.listen(pool, "checkout", lambda *_: self._update_pool(pool))
        event.listen(pool, "checkin", lambda *_: self._update_pool(pool))
        self._update_pool(pool)

    def _update_pool(self, pool: Any) -> None:
        self.db_overflow.labels(self.service).set(max(0, pool.overflow()))

    def observe_dependency(
        self, dependency: str, operation: str, elapsed: float, outcome: str
    ) -> None:
        labels = (self.service, dependency, operation)
        self.dependency_requests.labels(*labels, outcome).inc()
        self.dependency_duration.labels(*labels, outcome).observe(elapsed)

    def observe_redis(self, operation: str, elapsed: float, failed: bool) -> None:
        outcome = "error" if failed else "success"
        self.redis_duration.labels(self.service, operation, outcome).observe(elapsed)
        if failed:
            self.redis_failures.labels(self.service, operation).inc()
