"""Tests for normalized Prometheus and Loki HTTP adapters."""

import json
from datetime import UTC, datetime

import httpx
from opsmind_detection_engine.sources import LokiSource, PrometheusSource


def test_prometheus_samples_are_normalized_into_detector_observations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        labels = {"service": "payment-service"}
        if "dependency" in query:
            labels["dependency"] = "payment-service"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"metric": labels, "value": [1, "0.2"]}]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observations = PrometheusSource(client, "http://prometheus").fetch()

    assert {sample.metric for sample in observations} == {
        "traffic_rate",
        "error_rate",
        "latency_p95",
        "dependency_error_rate",
    }
    assert all(sample.service == "payment-service" for sample in observations)
    dependency = next(sample for sample in observations if sample.metric == "dependency_error_rate")
    assert dependency.dependency == "payment-service"
    assert dependency.value == 0.2


def test_loki_json_error_logs_are_normalized_and_service_names_mapped() -> None:
    timestamp_ns = int(datetime.now(UTC).timestamp() * 1_000_000_000)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {
                                "service": "shopstack-payment-service",
                                "environment": "local",
                            },
                            "values": [
                                [
                                    str(timestamp_ns),
                                    json.dumps({"level": "error", "event": "payment declined"}),
                                ]
                            ],
                        }
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observations = LokiSource(client, "http://loki").fetch()

    assert len(observations) == 1
    assert observations[0].service == "payment-service"
    assert observations[0].level == "error"
    assert observations[0].message == "payment declined"
