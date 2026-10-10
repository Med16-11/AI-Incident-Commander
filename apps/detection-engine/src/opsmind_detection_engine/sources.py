"""HTTP adapters for Prometheus instant queries and Loki log queries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from opsmind_detection_engine.detection import LogObservation, MetricObservation


class PrometheusSource:
    """Read normalized service-level signals from the Prometheus HTTP API."""

    def __init__(
        self, client: httpx.Client, base_url: str, *, job_regex: str = "shopstack"
    ) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/api/v1/query"
        self._job_matcher = f"job=~{json.dumps(job_regex)}"

    def _query(self, query: str) -> list[dict[str, Any]]:
        response = self._client.get(self._url, params={"query": query})
        response.raise_for_status()
        document = response.json()
        if document.get("status") != "success":
            raise RuntimeError("Prometheus returned an unsuccessful query response")
        results = document.get("data", {}).get("result", [])
        return results if isinstance(results, list) else []

    def fetch(self) -> list[MetricObservation]:
        """Collect request rate, 5xx ratio, p95 latency, and dependency failures."""
        error_rate_query = (
            f"sum by (service) "
            f'(rate(http_requests_total{{{self._job_matcher},status=~"5.."}}[5m])) '
            "/ clamp_min(sum by (service) "
            f"(rate(http_requests_total{{{self._job_matcher}}}[5m])), 0.001)"
        )
        queries: tuple[tuple[str, str, str], ...] = (
            (
                "traffic_rate",
                f"sum by (service) (rate(http_requests_total{{{self._job_matcher}}}[5m]))",
                "requests/s",
            ),
            (
                "error_rate",
                error_rate_query,
                "ratio",
            ),
            (
                "latency_p95",
                "histogram_quantile(0.95, sum by (service, le) "
                f"(rate(http_request_duration_seconds_bucket{{{self._job_matcher}}}[5m])))",
                "seconds",
            ),
            (
                "dependency_error_rate",
                "sum by (service, dependency) "
                f'(rate(dependency_requests_total{{{self._job_matcher},outcome="error"}}[5m])) '
                "/ clamp_min(sum by (service, dependency) "
                f"(rate(dependency_requests_total{{{self._job_matcher}}}[5m])), 0.001)",
                "ratio",
            ),
        )
        now = datetime.now(UTC)
        observations: list[MetricObservation] = []
        for metric, query, unit in queries:
            for result in self._query(query):
                labels = result.get("metric", {})
                pair = result.get("value", [])
                if not isinstance(labels, dict) or len(pair) < 2 or not labels.get("service"):
                    continue
                try:
                    value = float(pair[1])
                except (TypeError, ValueError):
                    continue
                observations.append(
                    MetricObservation(
                        service=str(labels["service"]),
                        metric=metric,
                        value=value,
                        unit=unit,
                        observed_at=now,
                        dependency=str(labels["dependency"]) if labels.get("dependency") else None,
                    )
                )
        return observations


class LokiSource:
    """Read recent structured error logs from Loki's range-query API."""

    def __init__(self, client: httpx.Client, base_url: str, query: str | None = None) -> None:
        self._client = client
        self._url = f"{base_url.rstrip('/')}/loki/api/v1/query_range"
        self._query = query or '{service=~".+"} | json | level=~"error|fatal|critical"'

    def fetch(self, window_seconds: int = 60) -> list[LogObservation]:
        end = datetime.now(UTC)
        start = end - timedelta(seconds=window_seconds)
        response = self._client.get(
            self._url,
            params={
                "query": self._query,
                "start": str(int(start.timestamp() * 1_000_000_000)),
                "end": str(int(end.timestamp() * 1_000_000_000)),
                "limit": "1000",
                "direction": "backward",
            },
        )
        response.raise_for_status()
        document = response.json()
        if document.get("status") != "success":
            raise RuntimeError("Loki returned an unsuccessful query response")
        data = document.get("data", {}).get("result", [])
        observations: list[LogObservation] = []
        for stream in data if isinstance(data, list) else []:
            if not isinstance(stream, dict):
                continue
            labels = stream.get("stream", {})
            service = labels.get("service") or labels.get("service_name")
            service = {
                "shopstack-api-gateway": "api-gateway",
                "shopstack-order-service": "order-service",
                "shopstack-inventory-service": "inventory-service",
                "shopstack-payment-service": "payment-service",
                "shopstack-user-service": "user-service",
                "api": "opsmind-api",
            }.get(str(service), service)
            for pair in stream.get("values", []):
                if not service or not isinstance(pair, list) or len(pair) < 2:
                    continue
                line = str(pair[1])
                try:
                    fields = json.loads(line)
                except (ValueError, TypeError):
                    fields = {}
                if not isinstance(fields, dict):
                    fields = {}
                level = str(fields.get("level", labels.get("level", "error"))).lower()
                message = str(
                    fields.get("event") or fields.get("message") or fields.get("msg") or line
                )
                try:
                    observed_at = datetime.fromtimestamp(int(pair[0]) / 1_000_000_000, UTC)
                except (TypeError, ValueError, OverflowError):
                    observed_at = end
                observations.append(
                    LogObservation(
                        service=str(service),
                        message=message,
                        level=level,
                        observed_at=observed_at,
                        dependency=str(fields["dependency"]) if fields.get("dependency") else None,
                        labels={str(k): str(v) for k, v in labels.items() if k != "service"},
                    )
                )
        return observations
