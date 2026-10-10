"""Tests for retries, duplicate delivery, and restart-safe stream processing."""

from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from uuid import UUID

import pytest
from opsmind_events.consumer import RedisStreamConsumer
from opsmind_events.contracts import EventUnitOfWork, EventUnitOfWorkFactory
from opsmind_events.metrics import EventMetrics
from opsmind_events.publisher import RedisStreamPublisher
from opsmind_events.redis_client import RedisStreamsClient
from opsmind_models.events import EventEnvelope, EventPayload
from opsmind_storage.event_inbox import SqlAlchemyEventUnitOfWorkFactory
from prometheus_client import generate_latest
from sqlalchemy.orm import Session, sessionmaker


class FakeStreams:
    """Small Redis Streams test double with consumer-group pending state."""

    def __init__(self) -> None:
        self.streams: dict[str, deque[tuple[str, dict[str, str]]]] = {}
        self.pending: dict[tuple[str, str, str], tuple[str, dict[str, str]]] = {}
        self.next_id = 0
        self.fail_ack_once = False
        self.fail_read_once = False
        self.add_options: list[dict[str, object]] = []

    def xadd(self, stream: str, fields: dict[str, str], **kwargs: object) -> str:
        self.add_options.append(kwargs)
        self.next_id += 1
        entry_id = f"{self.next_id}-0"
        self.streams.setdefault(stream, deque()).append((entry_id, fields))
        return entry_id

    def xinfo_groups(self, stream: str) -> list[dict[str, object]]:
        pending_count = sum(key[0] == stream for key in self.pending)
        lag = len(self.streams.get(stream, ()))
        return [{"name": "incident-engine", "pending": pending_count, "lag": lag}]

    def xgroup_create(self, *_: object, **__: object) -> None:
        return None

    def xautoclaim(
        self, stream: str, group: str, consumer: str, **_: object
    ) -> tuple[str, list[tuple[str, dict[str, str]]], list[str]]:
        del consumer
        pending = [value for key, value in self.pending.items() if key[:2] == (stream, group)]
        return "0-0", pending[:1], []

    def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        **_: object,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        del consumer
        if self.fail_read_once:
            self.fail_read_once = False
            raise ConnectionError("simulated Redis outage")
        stream = next(iter(streams))
        entries: list[tuple[str, dict[str, str]]] = []
        if self.streams.get(stream):
            entry = self.streams[stream].popleft()
            self.pending[(stream, group, entry[0])] = entry
            entries.append(entry)
        return [(stream, entries)] if entries else []

    def xack(self, stream: str, group: str, entry_id: str) -> int:
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise RuntimeError("simulated lost acknowledgement")
        return int(self.pending.pop((stream, group, entry_id), None) is not None)


class MemoryUow(EventUnitOfWork):
    def __init__(self, processed: set[tuple[str, UUID]], group: str) -> None:
        self.processed = processed
        self.group = group

    @property
    def session(self) -> object:
        return self

    def is_processed(self, event_id: UUID) -> bool:
        return (self.group, event_id) in self.processed

    def mark_processed(self, event_id: UUID) -> None:
        self.processed.add((self.group, event_id))


class MemoryUowFactory:
    def __init__(self) -> None:
        self.processed: set[tuple[str, UUID]] = set()

    @contextmanager
    def __call__(self, group: str) -> Iterator[EventUnitOfWork]:
        yield MemoryUow(self.processed, group)


def event_json() -> str:
    return EventEnvelope[EventPayload](
        event_type="IncidentCreated",
        source="test",
        correlation_id="corr-1",
        payload=EventPayload.model_validate({"incident_id": "i-1"}),
    ).model_dump_json()


def test_publisher_does_not_trim_stream_by_default() -> None:
    redis = FakeStreams()
    publisher = RedisStreamPublisher(
        cast(RedisStreamsClient, redis), "opsmind.events", EventMetrics()
    )
    envelope = EventEnvelope[EventPayload].model_validate_json(event_json())

    publisher.publish(envelope)

    assert redis.add_options == [{}]


def test_event_metrics_registry_is_available_to_scrape() -> None:
    metrics = EventMetrics()
    metrics.events_published_total.inc()

    rendered = generate_latest(metrics.registry).decode()

    assert "events_published_total 1.0" in rendered
    assert "events_processed_total" in rendered
    assert "events_failed_total" in rendered
    assert "event_processing_latency_bucket" in rendered
    assert "queue_depth" in rendered


def consumer(
    redis: FakeStreams,
    factory: MemoryUowFactory,
    handler: object,
    *,
    max_retries: int = 2,
    delays: list[float] | None = None,
) -> RedisStreamConsumer:
    return RedisStreamConsumer(
        redis,  # type: ignore[arg-type]
        "opsmind.events",
        "incident-engine",
        "worker-1",
        handler,  # type: ignore[arg-type]
        cast(EventUnitOfWorkFactory, factory),
        metrics=EventMetrics(),
        max_retries=max_retries,
        base_backoff_seconds=1,
        max_backoff_seconds=10,
        sleep=(delays.append if delays is not None else lambda _: None),
        jitter=lambda: 0,
        claim_idle_ms=0,
    )


def test_failed_event_retries_with_exponential_backoff() -> None:
    redis = FakeStreams()
    redis.xadd("opsmind.events", {"event": event_json(), "attempt": "0"})
    factory = MemoryUowFactory()
    delays: list[float] = []
    calls = 0

    event_id = (
        EventEnvelope[EventPayload]
        .model_validate_json(redis.streams["opsmind.events"][0][1]["event"])
        .event_id
    )

    def stable_handler(event: EventEnvelope[EventPayload], uow: MemoryUow) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("temporary downstream error")
        uow.mark_processed(event.event_id)

    worker = consumer(redis, factory, stable_handler, delays=delays)
    worker.consume_once(block_ms=0)
    worker.consume_once(block_ms=0)
    worker.consume_once(block_ms=0)

    assert calls == 3
    assert delays == [0.5, 1]
    assert ("incident-engine", event_id) in factory.processed
    assert not redis.streams.get(worker.dead_letter_stream)


def test_exhausted_retries_are_sent_to_dead_letter_stream() -> None:
    redis = FakeStreams()
    redis.xadd("opsmind.events", {"event": event_json(), "attempt": "0"})
    worker = consumer(
        redis,
        MemoryUowFactory(),
        lambda *_: (_ for _ in ()).throw(RuntimeError("bad")),
        max_retries=0,
    )

    worker.consume_once(block_ms=0)

    dead_letter = redis.streams[worker.dead_letter_stream][0][1]
    assert dead_letter["attempt"] == "0"
    assert dead_letter["error_type"] == "RuntimeError"


def test_queue_depth_counts_unread_and_pending_group_entries() -> None:
    redis = FakeStreams()
    redis.xadd("opsmind.events", {"event": event_json(), "attempt": "0"})
    metrics = EventMetrics()
    worker = RedisStreamConsumer(
        cast(RedisStreamsClient, redis),
        "opsmind.events",
        "incident-engine",
        "worker-1",
        lambda *_: (_ for _ in ()).throw(RuntimeError("temporary")),
        cast(EventUnitOfWorkFactory, MemoryUowFactory()),
        metrics=metrics,
        max_retries=1,
        sleep=lambda _: None,
        jitter=lambda: 0,
        claim_idle_ms=0,
    )

    worker.consume_once(block_ms=0)

    rendered = generate_latest(metrics.registry).decode()
    assert "queue_depth 1.0" in rendered


def test_duplicate_delivery_does_not_repeat_handler_side_effect() -> None:
    redis = FakeStreams()
    raw = event_json()
    redis.xadd("opsmind.events", {"event": raw})
    redis.xadd("opsmind.events", {"event": raw})
    factory = MemoryUowFactory()
    side_effects: list[UUID] = []

    def handler(event: EventEnvelope[EventPayload], uow: MemoryUow) -> None:
        side_effects.append(event.event_id)
        uow.mark_processed(event.event_id)

    worker = consumer(redis, factory, handler)
    worker.consume_once(block_ms=0)
    worker.consume_once(block_ms=0)

    assert side_effects == [EventEnvelope[EventPayload].model_validate_json(raw).event_id]


def test_restart_reclaims_committed_event_without_repeating_side_effect() -> None:
    redis = FakeStreams()
    raw = event_json()
    redis.xadd("opsmind.events", {"event": raw})
    redis.fail_ack_once = True
    factory = MemoryUowFactory()
    side_effects: list[UUID] = []

    def handler(event: EventEnvelope[EventPayload], uow: MemoryUow) -> None:
        side_effects.append(event.event_id)
        uow.mark_processed(event.event_id)

    first = consumer(redis, factory, handler)
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        first.consume_once(block_ms=0)

    restarted = consumer(redis, factory, handler)
    restarted.consume_once(block_ms=0)

    assert len(side_effects) == 1
    assert not redis.pending


def test_redis_outage_leaves_stream_entry_available_for_recovery() -> None:
    redis = FakeStreams()
    raw = event_json()
    redis.xadd("opsmind.events", {"event": raw})
    redis.fail_read_once = True
    factory = MemoryUowFactory()
    side_effects: list[UUID] = []

    def handler(event: EventEnvelope[EventPayload], uow: MemoryUow) -> None:
        side_effects.append(event.event_id)
        uow.mark_processed(event.event_id)

    worker = consumer(redis, factory, handler)

    with pytest.raises(ConnectionError, match="Redis outage"):
        worker.consume_once(block_ms=0)
    assert side_effects == []

    assert worker.consume_once(block_ms=0) == 1
    assert side_effects == [EventEnvelope[EventPayload].model_validate_json(raw).event_id]


def test_sqlalchemy_inbox_persists_idempotency_between_uow_instances(
    db_session: Session,
) -> None:
    # Use the fixture's engine to create independent sessions over the same database.
    engine = db_session.get_bind()
    factory = SqlAlchemyEventUnitOfWorkFactory(sessionmaker(bind=engine, expire_on_commit=False))
    event_id = EventEnvelope[EventPayload](
        event_type="IncidentCreated", source="test", correlation_id="corr", payload=EventPayload()
    ).event_id

    with factory("incident-engine") as uow:
        assert not uow.is_processed(event_id)
        uow.mark_processed(event_id)
    with factory("incident-engine") as restarted_uow:
        assert restarted_uow.is_processed(event_id)
