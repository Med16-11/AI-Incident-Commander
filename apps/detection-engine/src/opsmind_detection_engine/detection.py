"""Deterministic metric and log anomaly detection with bounded correlation."""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import fmean, pstdev
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from opsmind_models.events import CorrelatedIncidentPayload, EventEnvelope, OpsMindEventType
from pydantic import BaseModel, ConfigDict, Field, field_validator


class MetricObservation(BaseModel):
    """A normalized Prometheus sample at one observation time."""

    service: str = Field(min_length=1, max_length=160)
    metric: str = Field(min_length=1, max_length=160)
    value: float
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    unit: str = ""
    dependency: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def value_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric value must be finite")
        return value


class LogObservation(BaseModel):
    """A normalized error log event fetched from Loki."""

    service: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=4000)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    level: str = "error"
    dependency: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class MetricAnomalyPayload(BaseModel):
    """Typed evidence and score for a metric anomaly."""

    model_config = ConfigDict(extra="forbid")

    anomaly_signature: str
    service: str
    metric: str
    observed_value: float
    unit: str
    observed_at: datetime
    score: int = Field(ge=0, le=100)
    detectors: list[str]
    summary: str
    dependency: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class LogAnomalyPayload(BaseModel):
    """Typed evidence and score for a repeated error-log pattern."""

    model_config = ConfigDict(extra="forbid")

    anomaly_signature: str
    service: str
    fingerprint: str
    message: str
    observed_at: datetime
    occurrence_count: int = Field(ge=1)
    score: int = Field(ge=0, le=100)
    summary: str
    dependency: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


AnomalyPayload = MetricAnomalyPayload | LogAnomalyPayload
AnomalyEvent = EventEnvelope[AnomalyPayload]
_MAX_LOG_FINGERPRINTS = 10_000
_MAX_LOG_IDENTITIES_PER_FINGERPRINT = 2_000


@dataclass(frozen=True, slots=True)
class Threshold:
    """Optional inclusive static lower and upper bounds."""

    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class DetectorSettings:
    """Deterministic detector tuning; all time values are seconds."""

    thresholds: dict[str, Threshold] = field(
        default_factory=lambda: {
            "error_rate": Threshold(maximum=0.05),
            "dependency_error_rate": Threshold(maximum=0.10),
            "latency_p95": Threshold(maximum=1.0),
        }
    )
    baseline_window: int = 30
    baseline_min_samples: int = 8
    baseline_deviation_ratio: float = 0.50
    z_score_threshold: float = 3.5
    traffic_drop_ratio: float = 0.50
    log_window_seconds: int = 60
    log_occurrence_threshold: int = 5
    correlation_window_seconds: int = 120
    deduplication_ttl_seconds: int = 900


@dataclass(frozen=True, slots=True)
class DetectorFinding:
    detector: str
    score: int
    summary: str


def _score(base: int, magnitude: float) -> int:
    """Map deterministic severity to the public 0–100 scale."""
    return min(100, max(0, round(base + min(30.0, abs(magnitude) * 5.0))))


def _signature(service: str, metric: str, dependency: str | None, labels: dict[str, str]) -> str:
    dimensions = "|".join(
        (service, metric, dependency or "", ";".join(f"{k}={v}" for k, v in sorted(labels.items())))
    )
    return hashlib.sha256(dimensions.encode()).hexdigest()


class DeterministicDetector:
    """Static, rolling, z-score, error, latency, and traffic-drop detectors."""

    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self.settings = settings or DetectorSettings()
        self._history: dict[tuple[str, str, str | None], deque[float]] = defaultdict(
            lambda: deque(maxlen=self.settings.baseline_window)
        )
        self._logs: dict[tuple[str, str, str | None], deque[datetime]] = defaultdict(
            lambda: deque(
                maxlen=max(
                    self.settings.log_occurrence_threshold, _MAX_LOG_IDENTITIES_PER_FINGERPRINT
                )
            )
        )
        self._seen_logs: dict[tuple[str, str, str | None], set[tuple[datetime, str]]] = defaultdict(
            set
        )

    def detect_metric(self, observation: MetricObservation) -> AnomalyEvent | None:
        key = (observation.service, observation.metric, observation.dependency)
        history = self._history[key]
        findings: list[DetectorFinding] = []
        threshold = self.settings.thresholds.get(observation.metric)
        if threshold and threshold.maximum is not None and observation.value > threshold.maximum:
            findings.append(
                DetectorFinding(
                    "static_threshold",
                    _score(65, observation.value / max(abs(threshold.maximum), 0.001) - 1),
                    f"{observation.metric} exceeded {threshold.maximum:g}{observation.unit}",
                )
            )
        if threshold and threshold.minimum is not None and observation.value < threshold.minimum:
            findings.append(
                DetectorFinding(
                    "static_threshold",
                    _score(65, threshold.minimum - observation.value),
                    f"{observation.metric} fell below {threshold.minimum:g}{observation.unit}",
                )
            )

        if len(history) >= self.settings.baseline_min_samples:
            baseline = fmean(history)
            scale = max(abs(baseline), 0.001)
            relative_delta = (observation.value - baseline) / scale
            if abs(relative_delta) >= self.settings.baseline_deviation_ratio:
                findings.append(
                    DetectorFinding(
                        "rolling_baseline",
                        _score(55, abs(relative_delta)),
                        f"{observation.metric} deviated {relative_delta:+.0%} "
                        "from its rolling baseline",
                    )
                )
            deviation = pstdev(history)
            if deviation > max(abs(baseline) * 0.001, 1e-9):
                z_score = (observation.value - baseline) / deviation
                if abs(z_score) >= self.settings.z_score_threshold:
                    findings.append(
                        DetectorFinding(
                            "z_score",
                            _score(60, abs(z_score) - self.settings.z_score_threshold),
                            f"{observation.metric} deviated {z_score:.1f} standard deviations",
                        )
                    )
            if observation.metric == "traffic_rate" and baseline > 0:
                ratio = observation.value / baseline
                if ratio <= self.settings.traffic_drop_ratio:
                    findings.append(
                        DetectorFinding(
                            "sudden_traffic_drop",
                            _score(65, 1.0 - ratio),
                            f"Traffic fell to {ratio:.0%} of its rolling baseline",
                        )
                    )

        # Include the new sample only after comparisons so it cannot mute itself.
        history.append(observation.value)
        if not findings:
            return None
        # Metric-specialized rules are also explicit in the event provenance.
        specialized: list[str] = []
        if observation.metric in {"error_rate", "dependency_error_rate"}:
            specialized.append("error_rate")
        elif observation.metric == "latency_p95":
            specialized.append("latency")
        unique_detectors = list(dict.fromkeys([f.detector for f in findings] + specialized))
        score = max(f.score for f in findings)
        signature = _signature(
            observation.service, observation.metric, observation.dependency, observation.labels
        )
        payload = MetricAnomalyPayload(
            anomaly_signature=signature,
            service=observation.service,
            metric=observation.metric,
            observed_value=observation.value,
            unit=observation.unit,
            observed_at=observation.observed_at,
            score=score,
            detectors=unique_detectors,
            summary="; ".join(dict.fromkeys(f.summary for f in findings)),
            dependency=observation.dependency,
            labels=observation.labels,
        )
        return EventEnvelope[AnomalyPayload](
            event_type=OpsMindEventType.METRIC_ANOMALY_DETECTED,
            correlation_id=str(uuid4()),
            source="detection-engine",
            payload=payload,
        )

    def detect_logs(self, observations: list[LogObservation]) -> list[AnomalyEvent]:
        """Emit once per repeated error fingerprint in the configured time window."""
        grouped: dict[tuple[str, str, str | None], list[LogObservation]] = defaultdict(list)
        cutoff_default = datetime.now(UTC) - timedelta(seconds=self.settings.log_window_seconds)
        for key, timestamps in tuple(self._logs.items()):
            while timestamps and timestamps[0] < cutoff_default:
                timestamps.popleft()
            seen = self._seen_logs.get(key)
            if seen is not None:
                seen.difference_update({item for item in seen if item[0] < cutoff_default})
            # A threshold emission clears the active window but keeps recent identities
            # so polling the same backend page cannot emit the same anomaly repeatedly.
            if not timestamps and not seen:
                self._logs.pop(key, None)
                self._seen_logs.pop(key, None)
        for observation in sorted(observations, key=lambda item: item.observed_at):
            if observation.level.lower() not in {"error", "fatal", "critical"}:
                continue
            fingerprint = _log_fingerprint(observation.message)
            key = (observation.service, fingerprint, observation.dependency)
            identity = (
                observation.observed_at,
                hashlib.sha256(observation.message.encode()).hexdigest(),
            )
            if key not in self._logs and len(self._logs) >= _MAX_LOG_FINGERPRINTS:
                evicted_key = next(iter(self._logs))
                self._logs.pop(evicted_key, None)
                self._seen_logs.pop(evicted_key, None)
                grouped.pop(evicted_key, None)
            times = self._logs[key]
            cutoff = observation.observed_at - timedelta(seconds=self.settings.log_window_seconds)
            while times and times[0] < max(cutoff, cutoff_default):
                times.popleft()
            seen = self._seen_logs[key]
            seen.difference_update({item for item in seen if item[0] < max(cutoff, cutoff_default)})
            if identity in seen:
                continue
            if len(seen) >= _MAX_LOG_IDENTITIES_PER_FINGERPRINT:
                oldest_identity = min(seen, key=lambda item: item[0])
                seen.discard(oldest_identity)
            seen.add(identity)
            times.append(observation.observed_at)
            grouped[key].append(observation)

        events: list[AnomalyEvent] = []
        for (service, fingerprint, dependency), current in grouped.items():
            timestamps = self._logs[(service, fingerprint, dependency)]
            if len(timestamps) < self.settings.log_occurrence_threshold:
                continue
            first = current[0]
            signature = _signature(service, f"log:{fingerprint}", dependency, {})
            count = len(timestamps)
            payload = LogAnomalyPayload(
                anomaly_signature=signature,
                service=service,
                fingerprint=fingerprint,
                message=first.message[:1000],
                observed_at=max(item.observed_at for item in current),
                occurrence_count=count,
                score=min(100, 55 + 5 * max(0, count - self.settings.log_occurrence_threshold)),
                summary=(
                    f"{count} matching error logs occurred within "
                    f"{self.settings.log_window_seconds}s"
                ),
                dependency=dependency,
                labels=first.labels,
            )
            events.append(
                EventEnvelope[AnomalyPayload](
                    event_type=OpsMindEventType.LOG_ANOMALY_DETECTED,
                    correlation_id=str(uuid4()),
                    source="detection-engine",
                    payload=payload,
                )
            )
            # Keep the exact group from emitting on every poll; the next window starts clean.
            timestamps.clear()
        return events


def _log_fingerprint(message: str) -> str:
    normalized = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", message.lower())
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    return hashlib.sha256(" ".join(normalized.split()).encode()).hexdigest()


@dataclass(slots=True)
class CorrelatedIncident:
    """In-memory correlation state; durable incident creation is published once."""

    id: UUID
    signature: str
    root_service: str
    opened_at: datetime
    last_seen_at: datetime
    score: int
    anomaly_signatures: set[str] = field(default_factory=set)
    anomaly_count: int = 0
    anomaly_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CorrelationResult:
    incident: CorrelatedIncident
    created: bool
    newly_correlated: bool


class IncidentCorrelator:
    """Group related service-degradation anomalies and suppress duplicate incidents."""

    def __init__(self, settings: DetectorSettings | None = None) -> None:
        self.settings = settings or DetectorSettings()
        self._open: dict[str, CorrelatedIncident] = {}

    def correlate(self, event: AnomalyEvent) -> CorrelationResult:
        payload = event.payload
        service = payload.service
        dependency = payload.dependency
        root = dependency or service
        metric = payload.metric if isinstance(payload, MetricAnomalyPayload) else "log_error_rate"
        category = "traffic_degradation" if metric == "traffic_rate" else "service_degradation"
        signature = f"{root}:{category}"
        existing = self._open.get(signature)
        if existing is not None:
            age = (payload.observed_at - existing.opened_at).total_seconds()
            if abs(age) <= self.settings.deduplication_ttl_seconds:
                newly_correlated = payload.anomaly_signature not in existing.anomaly_signatures
                existing.last_seen_at = max(existing.last_seen_at, payload.observed_at)
                if payload.anomaly_signature not in existing.anomaly_signatures:
                    existing.anomaly_signatures.add(payload.anomaly_signature)
                    existing.anomaly_count += 1
                    existing.anomaly_ids.append(event.event_id)
                    within_window = abs(age) <= self.settings.correlation_window_seconds
                    existing.score = min(
                        100,
                        max(existing.score, payload.score) + (10 if within_window else 0),
                    )
                return CorrelationResult(existing, False, newly_correlated)

        incident = CorrelatedIncident(
            id=uuid5(NAMESPACE_URL, f"opsmind:{signature}:{payload.observed_at.isoformat()}"),
            signature=signature,
            root_service=root,
            opened_at=payload.observed_at,
            last_seen_at=payload.observed_at,
            score=payload.score,
            anomaly_signatures={payload.anomaly_signature},
            anomaly_count=1,
            anomaly_ids=[event.event_id],
        )
        self._open[signature] = incident
        return CorrelationResult(incident, True, False)


def incident_created_event(
    incident: CorrelatedIncident,
) -> EventEnvelope[CorrelatedIncidentPayload]:
    """Build a stable, typed event for a newly correlated incident."""
    return EventEnvelope[CorrelatedIncidentPayload](
        event_id=incident.id,
        event_type=OpsMindEventType.INCIDENT_CREATED,
        correlation_id=str(incident.id),
        source="detection-engine",
        causation_id=incident.anomaly_ids[0] if incident.anomaly_ids else None,
        aggregate_type="incident",
        aggregate_id=incident.id,
        payload=CorrelatedIncidentPayload(
            incident_id=incident.id,
            signature=incident.signature,
            service=incident.root_service,
            score=incident.score,
            anomaly_ids=incident.anomaly_ids,
            first_seen_at=incident.opened_at,
            last_seen_at=incident.last_seen_at,
            summary=(
                f"Detected {incident.anomaly_count} correlated signals for {incident.root_service}"
            ),
        ),
    )


def incident_updated_event(
    incident: CorrelatedIncident,
) -> EventEnvelope[CorrelatedIncidentPayload]:
    """Build an idempotent event adding newly correlated signals to an incident."""
    causation_id = incident.anomaly_ids[-1]
    return EventEnvelope[CorrelatedIncidentPayload](
        event_id=uuid5(NAMESPACE_URL, f"opsmind:{incident.id}:signal:{causation_id}"),
        event_type=OpsMindEventType.INCIDENT_UPDATED,
        correlation_id=str(incident.id),
        causation_id=causation_id,
        source="detection-engine",
        aggregate_type="incident",
        aggregate_id=incident.id,
        aggregate_version=incident.anomaly_count,
        payload=CorrelatedIncidentPayload(
            incident_id=incident.id,
            signature=incident.signature,
            service=incident.root_service,
            score=incident.score,
            anomaly_ids=incident.anomaly_ids,
            first_seen_at=incident.opened_at,
            last_seen_at=incident.last_seen_at,
            summary=(
                f"Correlated signal added; {incident.anomaly_count} signals "
                f"for {incident.root_service}"
            ),
        ),
    )
