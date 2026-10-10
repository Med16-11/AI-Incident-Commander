"""One polling cycle and Redis-backed deduplication for detection events."""

from __future__ import annotations

from typing import Protocol

import structlog
from opsmind_events.publisher import RedisStreamPublisher

from opsmind_detection_engine.detection import (
    AnomalyEvent,
    DeterministicDetector,
    IncidentCorrelator,
    incident_created_event,
    incident_updated_event,
)
from opsmind_detection_engine.sources import LokiSource, PrometheusSource


class DeduplicationStore(Protocol):
    """Atomic expiring key-value operation required for duplicate suppression."""

    def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool | None: ...

    def delete(self, name: str) -> int: ...


class DetectionRunner:
    """Poll signal sources, emit typed anomalies, and publish one incident per group."""

    def __init__(
        self,
        prometheus: PrometheusSource,
        loki: LokiSource,
        detector: DeterministicDetector,
        correlator: IncidentCorrelator,
        publisher: RedisStreamPublisher,
        deduplication_store: DeduplicationStore,
    ) -> None:
        self.prometheus = prometheus
        self.loki = loki
        self.detector = detector
        self.correlator = correlator
        self.publisher = publisher
        self.deduplication_store = deduplication_store
        self.logger = structlog.get_logger(service="detection-engine")

    def run_once(self) -> tuple[int, int]:
        """Poll both sources and return counts of published anomalies/incidents."""
        anomalies: list[AnomalyEvent] = []
        try:
            for sample in self.prometheus.fetch():
                event = self.detector.detect_metric(sample)
                if event is not None:
                    anomalies.append(event)
        except Exception as exc:
            self.logger.error("prometheus_detection_poll_failed", error_type=type(exc).__name__)
        try:
            anomalies.extend(self.detector.detect_logs(self.loki.fetch()))
        except Exception as exc:
            self.logger.error("loki_detection_poll_failed", error_type=type(exc).__name__)

        published_anomalies = 0
        published_incidents = 0
        for event in anomalies:
            payload = event.payload
            time_bucket = int(payload.observed_at.timestamp()) // 120
            anomaly_key = f"opsmind:detection:anomaly:{payload.anomaly_signature}:{time_bucket}"
            if self.deduplication_store.set(anomaly_key, "1", nx=True, ex=240):
                try:
                    self.publisher.publish(event)
                except Exception:
                    self.deduplication_store.delete(anomaly_key)
                    raise
                published_anomalies += 1

            result = self.correlator.correlate(event)
            if result.created:
                incident_key = f"opsmind:detection:incident:{result.incident.signature}"
                if not self.deduplication_store.set(
                    incident_key,
                    str(result.incident.id),
                    nx=True,
                    ex=self.correlator.settings.deduplication_ttl_seconds,
                ):
                    continue
                try:
                    self.publisher.publish(incident_created_event(result.incident))
                except Exception:
                    self.deduplication_store.delete(incident_key)
                    raise
                published_incidents += 1
            elif result.newly_correlated:
                update_key = (
                    f"opsmind:detection:incident-update:{result.incident.id}:"
                    f"{payload.anomaly_signature}"
                )
                if not self.deduplication_store.set(
                    update_key,
                    "1",
                    nx=True,
                    ex=self.correlator.settings.deduplication_ttl_seconds,
                ):
                    continue
                try:
                    self.publisher.publish(incident_updated_event(result.incident))
                except Exception:
                    self.deduplication_store.delete(update_key)
                    raise
        return published_anomalies, published_incidents
