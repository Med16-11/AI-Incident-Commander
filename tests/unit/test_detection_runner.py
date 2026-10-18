"""Polling runner suppresses repeated stream entries and incidents."""

from datetime import UTC, datetime

import httpx
import pytest
from opsmind_detection_engine.detection import (
    DetectorSettings,
    DeterministicDetector,
    IncidentCorrelator,
    LogObservation,
    MetricObservation,
)
from opsmind_detection_engine.runner import DetectionRunner


class _Source:
    def __init__(self, result: list[object]) -> None:
        self.result = result

    def fetch(self) -> list[object]:
        return self.result


class _Loki:
    def __init__(self, result: list[object] | None = None) -> None:
        self.result = result or []

    def fetch(self) -> list[object]:
        return self.result


class _Store:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool | None:
        if nx and name in self.keys:
            return None
        self.keys.add(name)
        return True

    def delete(self, name: str) -> int:
        self.keys.discard(name)
        return 1


class _Publisher:
    def __init__(self) -> None:
        self.events: list[object] = []

    def publish(self, event: object) -> str:
        self.events.append(event)
        return str(len(self.events))


def test_repeat_poll_publishes_one_anomaly_and_one_incident() -> None:
    timestamp = datetime.now(UTC)
    settings = DetectorSettings()
    prometheus = _Source(
        [
            MetricObservation(
                service="payment-service",
                metric="error_rate",
                value=0.25,
                observed_at=timestamp,
            )
        ]
    )
    publisher = _Publisher()
    runner = DetectionRunner(
        prometheus,  # type: ignore[arg-type]
        _Loki(),  # type: ignore[arg-type]
        DeterministicDetector(settings),
        IncidentCorrelator(settings),
        publisher,
        _Store(),
    )

    assert runner.run_once() == (1, 1)
    assert runner.run_once() == (0, 0)
    assert len(publisher.events) == 2


def test_prometheus_timeout_still_processes_loki_signals() -> None:
    class TimedOutPrometheus:
        def fetch(self) -> list[object]:
            raise httpx.ReadTimeout("Prometheus did not respond")

    settings = DetectorSettings(log_occurrence_threshold=1)
    publisher = _Publisher()
    runner = DetectionRunner(
        TimedOutPrometheus(),  # type: ignore[arg-type]
        _Loki([LogObservation(service="payment", message="charge failed")]),  # type: ignore[arg-type]
        DeterministicDetector(settings),
        IncidentCorrelator(settings),
        publisher,
        _Store(),
    )

    anomalies, incidents = runner.run_once()

    assert anomalies == 1
    assert incidents == 1
    assert len(publisher.events) == 2


def test_redis_publish_failure_releases_dedup_key_for_next_poll() -> None:
    class FailOncePublisher(_Publisher):
        failed = False

        def publish(self, event: object) -> str:
            if not self.failed:
                self.failed = True
                raise ConnectionError("Redis unavailable")
            return super().publish(event)

    settings = DetectorSettings()
    observation = MetricObservation(
        service="payment-service",
        metric="error_rate",
        value=0.25,
        observed_at=datetime.now(UTC),
    )
    publisher = FailOncePublisher()
    runner = DetectionRunner(
        _Source([observation]),  # type: ignore[arg-type]
        _Loki(),  # type: ignore[arg-type]
        DeterministicDetector(settings),
        IncidentCorrelator(settings),
        publisher,
        _Store(),
    )

    with pytest.raises(ConnectionError, match="Redis unavailable"):
        runner.run_once()
    assert runner.run_once() == (1, 1)
