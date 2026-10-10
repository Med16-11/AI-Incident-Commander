"""Telemetry package tests validate actual Prometheus exposition and trace context."""

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
    set_span_in_context,
)
from opsmind_common.logging import _add_trace_context
from opsmind_observability.metrics import ServiceMetrics


def test_http_metrics_include_low_cardinality_route_and_latency() -> None:
    app = FastAPI()
    metrics = ServiceMetrics("telemetry-test")
    metrics.install(app)

    @app.get("/things/{thing_id}")
    def read_thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    with TestClient(app) as client:
        assert client.get("/things/abc").status_code == 200
        assert client.get("/things/def").status_code == 200
        exposition = client.get("/metrics").text

    assert "http_requests_total{" in exposition
    assert 'route="/things/{thing_id}"' in exposition
    assert 'service="telemetry-test",status="200"} 2.0' in exposition
    assert "http_request_duration_seconds_count{" in exposition
    assert 'service="telemetry-test"} 2.0' in exposition


def test_dependency_metrics_capture_failure_and_latency() -> None:
    metrics = ServiceMetrics("telemetry-test")
    metrics.observe_dependency("postgres", "query", 0.025, "error")
    exposition = metrics.registry
    from prometheus_client import generate_latest

    text = generate_latest(exposition).decode()
    assert "dependency_requests_total{" in text
    assert 'dependency="postgres",operation="query",outcome="error"' in text
    assert 'service="telemetry-test"} 1.0' in text
    assert "dependency_request_duration_seconds_bucket" in text


def test_httpx_instrumentation_propagates_w3c_trace_context() -> None:
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(request.headers)
        return httpx.Response(200)

    provider = TracerProvider()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.instrument_client(client, tracer_provider=provider)
    try:
        with provider.get_tracer("test").start_as_current_span("upstream"):
            assert client.get("http://service.test/health").status_code == 200
    finally:
        instrumentor.uninstrument_client(client)
        client.close()

    assert observed.get("traceparent", "").startswith("00-")


def test_structured_log_processor_adds_trace_and_span_ids() -> None:
    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x1234,
            span_id=0x5678,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
    )
    from opentelemetry import context

    token = context.attach(set_span_in_context(span))
    try:
        fields = _add_trace_context(None, "info", {})
    finally:
        context.detach(token)

    assert fields["trace_id"] == "00000000000000000000000000001234"
    assert fields["span_id"] == "0000000000005678"
