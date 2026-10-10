"""Tests for shared event-envelope invariants."""

from uuid import UUID

import pytest
from opsmind_models.events import EventEnvelope
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
