"""Tests for shared event-envelope invariants."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from opsmind_models.events import EventEnvelope, EventPayload
from pydantic import BaseModel, ValidationError


class ExamplePayload(BaseModel):
    message: str


def test_event_envelope_requires_safe_correlation_id() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope[ExamplePayload](
            event_type="Example",
            source="unit-test",
            correlation_id="invalid correlation id",
            payload=ExamplePayload(message="test"),
        )


def test_event_envelope_retains_aggregate_metadata() -> None:
    aggregate_id = UUID("d9428888-122b-11e1-b85c-61cd3cbb3210")
    event = EventEnvelope[ExamplePayload](
        event_type="Example",
        source="unit-test",
        correlation_id="incident-123",
        aggregate_type="incident",
        aggregate_id=aggregate_id,
        aggregate_version=4,
        payload=ExamplePayload(message="test"),
    )

    assert event.aggregate_type == "incident"
    assert event.aggregate_id == aggregate_id
    assert event.aggregate_version == 4


def test_shared_go_ingestion_fixture_matches_python_event_contract() -> None:
    repository_root = Path(__file__).parents[2]
    fixture_path = repository_root / "packages/models/contracts/event-envelope-v1.fixture.json"
    schema_path = repository_root / "packages/models/contracts/event-envelope-v1.schema.json"

    envelope = EventEnvelope[EventPayload].model_validate_json(fixture_path.read_text())
    schema = json.loads(schema_path.read_text())

    assert envelope.event_type == "MetricAnomalyDetected"
    assert envelope.correlation_id == "sample-run-01"
    assert schema["title"] == "OpsMind Event Envelope v1"
