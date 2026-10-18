"""Authenticated client for the Go event-ingestion boundary."""

import time
from urllib.parse import urlsplit

import httpx
from opsmind_models.events import EventEnvelope
from pydantic import BaseModel


class EventIngestionError(RuntimeError):
    """Raised when Go event ingestion cannot accept or confirm an event."""


class EventIngestionPublisher:
    """Send typed OpsMind events through the Go service into Redis Streams."""

    def __init__(self, client: httpx.Client, base_url: str, bearer_token: str) -> None:
        parsed_url = urlsplit(base_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError(
                "event-ingestion URL must be HTTP(S), with no credentials, query, or fragment"
            )
        try:
            port = parsed_url.port
        except ValueError as exc:
            raise ValueError("event-ingestion URL port is invalid") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("event-ingestion URL port is invalid")
        if not bearer_token:
            raise ValueError("event-ingestion bearer token is required")
        self._client = client
        self._url = f"{base_url.rstrip('/')}/v1/events"
        self._bearer_token = bearer_token

    def publish[PayloadT: BaseModel](self, event: EventEnvelope[PayloadT]) -> str:
        """Publish one event and return the Redis stream entry ID."""
        payload = event.model_dump_json()
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json",
            "X-Correlation-ID": event.correlation_id,
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.post(self._url, content=payload, headers=headers)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.1 * (2**attempt))
                    continue
                break
            if response.status_code in (httpx.codes.ACCEPTED, httpx.codes.OK):
                try:
                    stream_entry_id = response.json()["stream_entry_id"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise EventIngestionError(
                        "event-ingestion returned an invalid response"
                    ) from exc
                if not isinstance(stream_entry_id, str) or not stream_entry_id:
                    raise EventIngestionError("event-ingestion returned an invalid stream entry ID")
                return stream_entry_id
            if response.status_code >= 500 and attempt < 2:
                time.sleep(0.1 * (2**attempt))
                continue
            raise EventIngestionError(
                f"event-ingestion rejected the event with HTTP {response.status_code}"
            )
        raise EventIngestionError("event-ingestion unavailable after 3 attempts") from last_error
