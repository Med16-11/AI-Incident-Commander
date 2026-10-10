"""Prometheus and Loki evidence adapters for incident correlation."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from opsmind_incident_engine.correlation import (
    EvidenceCandidate,
    IncidentContext,
    redact_evidence_summary,
)

_SAFE_LABELS = frozenset(
    {
        "service",
        "service_name",
        "pod",
        "container",
        "namespace",
        "job",
        "instance",
        "cluster",
        "environment",
        "dependency",
        "operation",
        "le",
    }
)


class PrometheusEvidenceSource:
    """Collect request anomalies, pod restarts, and unavailable targets from Prometheus."""

    name = "prometheus"

    def __init__(
        self,
        client: httpx.Client,
        base_url: str,
        step_seconds: int = 30,
        *,
        error_rate_threshold: float = 0.05,
        latency_p95_threshold_seconds: float = 1.0,
        dependency_error_rate_threshold: float = 0.10,
    ) -> None:
        if step_seconds < 1:
            raise ValueError("Prometheus query step must be positive")
        if not 0 < error_rate_threshold <= 1:
            raise ValueError("error-rate threshold must be between 0 and 1")
        if latency_p95_threshold_seconds <= 0:
            raise ValueError("latency threshold must be positive")
        if not 0 < dependency_error_rate_threshold <= 1:
            raise ValueError("dependency error-rate threshold must be between 0 and 1")
        self._client = client
        self._url = f"{base_url.rstrip('/')}/api/v1/query_range"
        self._step = step_seconds
        self._error_rate_threshold = error_rate_threshold
        self._latency_threshold = latency_p95_threshold_seconds
        self._dependency_error_rate_threshold = dependency_error_rate_threshold

    def collect(
        self, context: IncidentContext, start: datetime, end: datetime
    ) -> Iterable[EvidenceCandidate]:
        watched = {context.service, *context.dependencies}
        service_regex = "(" + "|".join(_escape_re2_literal(name) for name in sorted(watched)) + ")"
        service_selector = f'{{service=~"^{service_regex}$"}}'
        error_selector = f'{{service=~"^{service_regex}$",status=~"5.."}}'
        dependency_error_selector = f'{{service=~"^{service_regex}$",outcome="error"}}'
        dependency_selector = f'{{service=~"^{service_regex}$"}}'
        pod_regex = ".*" + service_regex + ".*"
        queries = (
            (
                "metric_anomaly",
                "sum by (service) (rate(http_requests_total"
                f"{error_selector}[5m])) / clamp_min(sum by (service) "
                f"(rate(http_requests_total{service_selector}[5m])), 0.001) "
                f"> {self._error_rate_threshold:g}",
                "HTTP error rate threshold exceeded",
            ),
            (
                "metric_anomaly",
                "histogram_quantile(0.95, sum by (service, le) "
                "(rate(http_request_duration_seconds_bucket"
                f"{service_selector}[5m]))) > {self._latency_threshold:g}",
                "p95 latency threshold exceeded",
            ),
            (
                "metric_anomaly",
                "sum by (service, dependency) (rate(dependency_requests_total"
                f"{dependency_error_selector}[5m])) / clamp_min(sum by "
                f"(service, dependency) (rate(dependency_requests_total"
                f"{dependency_selector}[5m])), 0.001) "
                f"> {self._dependency_error_rate_threshold:g}",
                "Dependency error rate threshold exceeded",
            ),
            (
                "pod_restart",
                f'increase(kube_pod_container_status_restarts_total{{pod=~"{pod_regex}"}}[5m])',
                "pod restart count",
            ),
            (
                "infrastructure_event",
                f'up{{instance=~"{pod_regex}"}} == 0',
                "Prometheus target unavailable",
            ),
        )
        items: list[EvidenceCandidate] = []
        step = max(self._step, math.ceil((end - start).total_seconds() / 1000))
        for evidence_type, expression, description in queries:
            response = self._client.get(
                self._url,
                params={
                    "query": expression,
                    "start": str(start.timestamp()),
                    "end": str(end.timestamp()),
                    "step": str(step),
                },
            )
            response.raise_for_status()
            document = response.json()
            if document.get("status") != "success":
                raise RuntimeError("Prometheus returned an unsuccessful range query")
            result = document.get("data", {}).get("result", [])
            for series in result if isinstance(result, list) else []:
                if not isinstance(series, dict):
                    continue
                labels = series.get("metric", {})
                if not isinstance(labels, dict):
                    continue
                service = _service_for(labels, watched, evidence_type)
                if service is None:
                    continue
                points: list[tuple[datetime, float]] = []
                for sample in series.get("values", []):
                    if not isinstance(sample, list) or len(sample) < 2:
                        continue
                    try:
                        epoch, value = float(sample[0]), float(sample[1])
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(epoch) or not math.isfinite(value):
                        continue
                    timestamp = datetime.fromtimestamp(epoch, UTC)
                    if evidence_type == "pod_restart" and value <= 0:
                        continue
                    if evidence_type == "infrastructure_event" and value != 0:
                        continue
                    points.append((timestamp, value))
                episodes: list[list[tuple[datetime, float]]] = []
                for point in points:
                    if not episodes or (point[0] - episodes[-1][-1][0]).total_seconds() > step * 2:
                        episodes.append([point])
                    else:
                        episodes[-1].append(point)
                for episode in episodes:
                    timestamp = episode[0][0]
                    value = max(point[1] for point in episode)
                    reference = "prometheus://query?" + urlencode(
                        {"query": expression, "time": timestamp.isoformat()}
                    )
                    safe_labels = {
                        str(key): str(value) for key, value in labels.items() if key in _SAFE_LABELS
                    }
                    dimensions = ", ".join(
                        f"{key}={safe_labels[key]}"
                        for key in sorted(safe_labels)
                        if key != "service"
                    )
                    suffix = f" ({dimensions})" if dimensions else ""
                    items.append(
                        EvidenceCandidate(
                            source=self.name,
                            timestamp=timestamp,
                            service=service,
                            evidence_type=evidence_type,
                            raw_reference=reference,
                            normalized_summary=redact_evidence_summary(
                                f"{description}: {value:g}{suffix}"
                            ),
                            confidence=0.9,
                            attributes={
                                "query": expression,
                                "value": value,
                                **safe_labels,
                            },
                        )
                    )
        return items


class LokiEvidenceSource:
    """Collect structured logs for the incident service and its known dependencies."""

    name = "loki"

    def __init__(self, client: httpx.Client, base_url: str, limit: int = 1000) -> None:
        if not 1 <= limit <= 5000:
            raise ValueError("Loki result limit must be between 1 and 5000")
        self._client = client
        self._url = f"{base_url.rstrip('/')}/loki/api/v1/query_range"
        self._limit = limit

    def collect(
        self, context: IncidentContext, start: datetime, end: datetime
    ) -> Iterable[EvidenceCandidate]:
        watched = {context.service, *context.dependencies}
        compose_aliases = {
            "api-gateway": "shopstack-api-gateway",
            "user-service": "shopstack-user-service",
            "order-service": "shopstack-order-service",
            "inventory-service": "shopstack-inventory-service",
            "payment-service": "shopstack-payment-service",
        }
        source_names = watched | {
            compose_aliases[name] for name in watched if name in compose_aliases
        }
        escaped = "|".join(_escape_re2_literal(name) for name in sorted(source_names))
        query = f'{{service=~"^({escaped})$"}}'
        response = self._client.get(
            self._url,
            params={
                "query": query,
                "start": str(int(start.timestamp() * 1_000_000_000)),
                "end": str(int(end.timestamp() * 1_000_000_000)),
                "limit": str(self._limit),
                "direction": "forward",
            },
        )
        response.raise_for_status()
        document = response.json()
        if document.get("status") != "success":
            raise RuntimeError("Loki returned an unsuccessful range query")
        result = document.get("data", {}).get("result", [])
        items: list[EvidenceCandidate] = []
        for stream in result if isinstance(result, list) else []:
            if not isinstance(stream, dict):
                continue
            labels = stream.get("stream", {})
            if not isinstance(labels, dict):
                continue
            service = labels.get("service") or labels.get("service_name")
            if not isinstance(service, str):
                continue
            service = {
                source_name: service_name for service_name, source_name in compose_aliases.items()
            }.get(service, service)
            if service not in watched:
                continue
            for pair in stream.get("values", []):
                if not isinstance(pair, list) or len(pair) < 2:
                    continue
                try:
                    timestamp = datetime.fromtimestamp(int(pair[0]) / 1_000_000_000, UTC)
                except (TypeError, ValueError, OverflowError):
                    continue
                line = str(pair[1])
                try:
                    parsed = json.loads(line)
                except (ValueError, TypeError):
                    parsed = {}
                fields = parsed if isinstance(parsed, dict) else {}
                level = str(fields.get("level", labels.get("level", "info"))).lower()
                message = str(
                    fields.get("event") or fields.get("message") or fields.get("msg") or line
                )
                event_type = str(fields.get("event_type", fields.get("kind", ""))).lower()
                if "config" in event_type:
                    evidence_type = "configuration_change"
                elif "restart" in event_type:
                    evidence_type = "pod_restart"
                elif "infrastructure" in event_type or "infra" in event_type:
                    evidence_type = "infrastructure_event"
                elif "deploy" in event_type:
                    evidence_type = "deployment"
                else:
                    evidence_type = "log"
                reference = "loki://query_range?" + urlencode(
                    {"query": query, "start": pair[0], "end": pair[0]}
                )
                items.append(
                    EvidenceCandidate(
                        source=self.name,
                        timestamp=timestamp,
                        service=str(service),
                        evidence_type=evidence_type,
                        raw_reference=reference,
                        normalized_summary=redact_evidence_summary(f"{level}: {message}"),
                        confidence=0.95,
                        attributes={
                            "level": level,
                            "labels": json.dumps(
                                {
                                    key: value
                                    for key, value in labels.items()
                                    if key in _SAFE_LABELS
                                },
                                sort_keys=True,
                            ),
                        },
                    )
                )
        return items


def _service_for(labels: dict[str, object], watched: set[str], evidence_type: str) -> str | None:
    """Resolve source labels to a known service without guessing unrelated targets."""
    service = labels.get("service") or labels.get("service_name")
    if isinstance(service, str) and service in watched:
        return service
    pod = labels.get("pod")
    if evidence_type == "pod_restart" and isinstance(pod, str):
        matches = [name for name in watched if name in pod]
        return max(matches, key=len) if matches else None
    if evidence_type == "infrastructure_event":
        target = f"{labels.get('job', '')} {labels.get('instance', '')}"
        matches = [name for name in watched if name in target]
        return max(matches, key=len) if matches else None
    return None


def _escape_re2_literal(value: str) -> str:
    """Escape RE2 metacharacters without escaping ordinary punctuation like hyphens."""
    return re.sub(r"([\\^$.|?*+(){}\[\]])", r"\\\1", value)
