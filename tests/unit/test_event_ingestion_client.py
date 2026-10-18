"""Contract tests for the Python-to-Go event-ingestion boundary."""

import json

import httpx
import pytest
from opsmind_clients import EventIngestionError, EventIngestionPublisher
from opsmind_models.events import EventEnvelope, EventPayload


def test_publisher_sends_typed_event_with_token_and_correlation_id() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"stream_entry_id": "1740000000000-0"})

    event = EventEnvelope[EventPayload](
        event_type="IncidentCreated",
        source="detection-engine",
        correlation_id="incident-correlation-1",
        payload=EventPayload.model_validate({"incident_id": "incident-1"}),
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        publisher = EventIngestionPublisher(
            client, "http://event-ingestion:8000/", "unit-test-token-with-sufficient-length"
        )
        entry_id = publisher.publish(event)

    assert entry_id == "1740000000000-0"
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/events"
    assert seen[0].headers["authorization"] == "Bearer unit-test-token-with-sufficient-length"
    assert seen[0].headers["x-correlation-id"] == "incident-correlation-1"
    assert json.loads(seen[0].content)["event_id"] == str(event.event_id)


def test_publisher_accepts_idempotent_replay_response() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"stream_entry_id": "1740000000000-0"})
    )
    with httpx.Client(transport=transport) as client:
        publisher = EventIngestionPublisher(client, "http://ingestion:8000", "test-token")
        event = EventEnvelope[EventPayload](
            event_type="IncidentUpdated",
            source="unit-test",
            correlation_id="corr-2",
            payload=EventPayload.model_validate({}),
        )
        assert publisher.publish(event) == "1740000000000-0"


def test_publisher_retries_transient_failure_with_same_idempotency_key() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(202, json={"stream_entry_id": "1740000000000-0"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        publisher = EventIngestionPublisher(client, "http://ingestion:8000", "test-token")
        event = EventEnvelope[EventPayload](
            event_type="IncidentCreated",
            source="unit-test",
            correlation_id="corr-retry",
            payload=EventPayload.model_validate({}),
        )
        publisher.publish(event)

    assert len(requests) == 2
    assert requests[0].content == requests[1].content


def test_publisher_reports_service_errors_without_echoing_response_body() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(503, text="password must not appear in errors")
    )
    with httpx.Client(transport=transport) as client:
        publisher = EventIngestionPublisher(client, "http://ingestion:8000", "test-token")
        event = EventEnvelope[EventPayload](
            event_type="IncidentCreated",
            source="unit-test",
            correlation_id="corr-3",
            payload=EventPayload.model_validate({}),
        )
        with pytest.raises(EventIngestionError, match="HTTP 503") as error:
            publisher.publish(event)
    assert "password" not in str(error.value)


@pytest.mark.parametrize("url", ["file:///tmp/events", "http://user:pass@host", "https://host?q=x"])
def test_publisher_rejects_unsafe_endpoint_urls(url: str) -> None:
    with httpx.Client() as client, pytest.raises(ValueError):
        EventIngestionPublisher(client, url, "test-token")
