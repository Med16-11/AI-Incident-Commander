"""Unit tests for deterministic signal detection and incident correlation."""

from datetime import UTC, datetime, timedelta

import pytest
from opsmind_detection_engine import detection as detection_module
from opsmind_detection_engine.detection import (
    AnomalyEvent,
    DetectorSettings,
    DeterministicDetector,
    IncidentCorrelator,
    LogAnomalyPayload,
    LogObservation,
    MetricAnomalyPayload,
    MetricObservation,
    incident_created_event,
    incident_updated_event,
)
from opsmind_models.events import EventEnvelope, OpsMindEventType


def _metric(
    detector: DeterministicDetector,
    service: str,
    metric: str,
    value: float,
    *,
    dependency: str | None = None,
    observed_at: datetime | None = None,
) -> AnomalyEvent | None:
    return detector.detect_metric(
        MetricObservation(
            service=service,
            metric=metric,
            value=value,
            dependency=dependency,
            observed_at=observed_at or datetime.now(UTC),
        )
    )


def test_stable_rolling_baseline_does_not_emit_false_positives() -> None:
    detector = DeterministicDetector()
    for _ in range(12):
        assert _metric(detector, "checkout", "latency_p95", 0.20) is None


def test_static_error_rate_and_latency_rules_emit_scored_events() -> None:
    detector = DeterministicDetector()
    error = _metric(detector, "payment-service", "error_rate", 0.20)
    latency = _metric(detector, "payment-service", "latency_p95", 1.8)

    assert error is not None
    assert isinstance(error.payload, MetricAnomalyPayload)
    assert error.event_type == OpsMindEventType.METRIC_ANOMALY_DETECTED
    assert isinstance(error.payload, MetricAnomalyPayload)
    assert "static_threshold" in error.payload.detectors
    assert "error_rate" in error.payload.detectors
    assert 0 <= error.payload.score <= 100
    assert latency is not None
    assert isinstance(latency.payload, MetricAnomalyPayload)
    assert "latency" in latency.payload.detectors


def test_rolling_baseline_and_z_score_detect_outliers_after_warmup() -> None:
    detector = DeterministicDetector()
    for value in (1.0, 1.01, 0.99, 1.02, 0.98, 1.0, 1.01, 0.99):
        assert _metric(detector, "catalog", "custom_metric", value) is None

    event = _metric(detector, "catalog", "custom_metric", 2.0)
    assert event is not None
    assert isinstance(event.payload, MetricAnomalyPayload)
    assert "rolling_baseline" in event.payload.detectors
    assert "z_score" in event.payload.detectors


def test_sudden_traffic_drop_requires_rolling_baseline() -> None:
    detector = DeterministicDetector()
    for _ in range(10):
        assert _metric(detector, "gateway", "traffic_rate", 100.0) is None

    event = _metric(detector, "gateway", "traffic_rate", 20.0)
    assert event is not None
    assert isinstance(event.payload, MetricAnomalyPayload)
    assert "sudden_traffic_drop" in event.payload.detectors


def test_repeated_error_logs_emit_once_and_duplicate_polls_are_ignored() -> None:
    detector = DeterministicDetector(DetectorSettings(log_occurrence_threshold=3))
    now = datetime.now(UTC)
    logs = [
        LogObservation(
            service="payment-service",
            message=f"charge failed for order {index}",
            observed_at=now + timedelta(seconds=index),
        )
        for index in range(3)
    ]

    events = detector.detect_logs(logs)
    assert len(events) == 1
    assert events[0].event_type == OpsMindEventType.LOG_ANOMALY_DETECTED
    assert isinstance(events[0].payload, LogAnomalyPayload)
    assert events[0].payload.occurrence_count == 3
    assert detector.detect_logs(logs) == []


def test_log_detector_bounds_fingerprint_and_message_deduplication_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection_module, "_MAX_LOG_FINGERPRINTS", 2)
    monkeypatch.setattr(detection_module, "_MAX_LOG_IDENTITIES_PER_FINGERPRINT", 3)
    detector = DeterministicDetector(DetectorSettings(log_occurrence_threshold=100))
    now = datetime.now(UTC)
    detector.detect_logs(
        [
            LogObservation(service="payments", message=message, observed_at=now)
            for message in ("error alpha", "error bravo", "error charlie", "error delta")
        ]
    )
    assert len(detector._logs) <= 2
    assert len(detector._seen_logs) <= 2

    detector.detect_logs(
        [
            LogObservation(
                service="payments",
                message="same fingerprint",
                observed_at=now + timedelta(seconds=index),
            )
            for index in range(10)
        ]
    )
    assert len(next(iter(detector._seen_logs.values()))) <= 3


def test_correlated_payment_and_order_signals_create_one_incident() -> None:
    detector = DeterministicDetector()
    correlator = IncidentCorrelator()
    now = datetime.now(UTC)
    signals = [
        _metric(detector, "payment-service", "error_rate", 0.20, observed_at=now),
        _metric(
            detector,
            "payment-service",
            "latency_p95",
            2.0,
            observed_at=now + timedelta(seconds=30),
        ),
        _metric(
            detector,
            "order-service",
            "dependency_error_rate",
            0.40,
            dependency="payment-service",
            observed_at=now + timedelta(seconds=90),
        ),
    ]
    assert all(signal is not None for signal in signals)

    results = [correlator.correlate(signal) for signal in signals if signal is not None]
    assert [result.created for result in results] == [True, False, False]
    assert len({result.incident.id for result in results}) == 1
    assert results[-1].incident.root_service == "payment-service"
    assert results[-1].incident.anomaly_count == 3


def test_duplicate_alerts_do_not_create_duplicate_incidents() -> None:
    detector = DeterministicDetector()
    correlator = IncidentCorrelator()
    event = _metric(detector, "payment-service", "error_rate", 0.20)
    assert event is not None

    first = correlator.correlate(event)
    duplicate = correlator.correlate(event)
    assert first.created is True
    assert duplicate.created is False
    assert first.incident.id == duplicate.incident.id
    assert duplicate.incident.anomaly_count == 1


def test_unrelated_services_create_separate_incidents() -> None:
    detector = DeterministicDetector()
    correlator = IncidentCorrelator()
    payment = _metric(detector, "payment-service", "error_rate", 0.20)
    inventory = _metric(detector, "inventory-service", "error_rate", 0.20)
    assert payment is not None and inventory is not None

    payment_result = correlator.correlate(payment)
    inventory_result = correlator.correlate(inventory)
    assert payment_result.created and inventory_result.created
    assert payment_result.incident.id != inventory_result.incident.id


def test_distinct_dependency_failures_from_one_service_are_separate() -> None:
    detector = DeterministicDetector()
    correlator = IncidentCorrelator()
    payment = _metric(
        detector, "order-service", "dependency_error_rate", 0.4, dependency="payment-service"
    )
    inventory = _metric(
        detector, "order-service", "dependency_error_rate", 0.4, dependency="inventory-service"
    )
    assert payment is not None and inventory is not None

    first = correlator.correlate(payment)
    second = correlator.correlate(inventory)
    assert first.created and second.created
    assert first.incident.id != second.incident.id


def test_correlated_signals_are_linked_with_incident_updated_event() -> None:
    detector = DeterministicDetector()
    correlator = IncidentCorrelator()
    now = datetime.now(UTC)
    payment_error = _metric(detector, "payment-service", "error_rate", 0.2, observed_at=now)
    payment_latency = _metric(
        detector, "payment-service", "latency_p95", 2.0, observed_at=now + timedelta(seconds=30)
    )
    assert payment_error is not None and payment_latency is not None
    first = correlator.correlate(payment_error)
    second = correlator.correlate(payment_latency)

    created = incident_created_event(first.incident)
    updated = incident_updated_event(second.incident)
    assert created.event_type == OpsMindEventType.INCIDENT_CREATED
    assert updated.event_type == OpsMindEventType.INCIDENT_UPDATED
    assert created.aggregate_id == updated.aggregate_id
    assert updated.payload.anomaly_ids == [payment_error.event_id, payment_latency.event_id]


def test_correlation_window_does_not_merge_later_unrelated_occurrence() -> None:
    detector = DeterministicDetector()
    correlator = IncidentCorrelator(DetectorSettings(deduplication_ttl_seconds=120))
    now = datetime.now(UTC)
    first = _metric(detector, "payment-service", "error_rate", 0.20, observed_at=now)
    second = _metric(
        detector,
        "payment-service",
        "error_rate",
        0.25,
        observed_at=now + timedelta(minutes=4),
    )
    assert first is not None and second is not None

    assert correlator.correlate(first).created is True
    assert correlator.correlate(second).created is True


def test_incident_event_has_typed_contract() -> None:
    detector = DeterministicDetector()
    event = _metric(detector, "payment-service", "error_rate", 0.20)
    assert event is not None
    assert event.event_type == OpsMindEventType.METRIC_ANOMALY_DETECTED
    incident = IncidentCorrelator().correlate(event)
    created = incident_created_event(incident.incident)
    assert isinstance(created, EventEnvelope)
    assert created.event_type == OpsMindEventType.INCIDENT_CREATED
