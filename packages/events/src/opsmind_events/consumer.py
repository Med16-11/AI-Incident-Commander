"""At-least-once Redis Streams consumer with transactional inbox semantics."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from opsmind_models.events import EventEnvelope, EventPayload

from opsmind_events.contracts import EventHandler, EventUnitOfWorkFactory, Sleep
from opsmind_events.metrics import EventMetrics
from opsmind_events.redis_client import RedisStreamsClient, StreamEntry


class RedisStreamConsumer:
    """Consume a group, reclaim abandoned work, and retry failed events."""

    def __init__(
        self,
        redis: RedisStreamsClient,
        stream: str,
        group: str,
        consumer: str,
        handler: EventHandler,
        unit_of_work_factory: EventUnitOfWorkFactory,
        *,
        metrics: EventMetrics | None = None,
        max_retries: int = 5,
        base_backoff_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
        claim_idle_ms: int = 60_000,
        sleep: Sleep = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not stream or not group or not consumer:
            raise ValueError("stream, group, and consumer names are required")
        if max_retries < 0 or base_backoff_seconds < 0 or max_backoff_seconds < 0:
            raise ValueError("retry limits and backoff values cannot be negative")
        self._redis = redis
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._handler = handler
        self._uow_factory = unit_of_work_factory
        self._metrics = metrics or EventMetrics()
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._claim_idle_ms = claim_idle_ms
        self._sleep = sleep
        self._jitter = jitter

    @property
    def dead_letter_stream(self) -> str:
        """Name of this stream's dead-letter stream."""
        return f"{self._stream}:dead-letter"

    def ensure_group(self) -> None:
        """Create the consumer group and stream if absent."""
        try:
            self._redis.xgroup_create(self._stream, self._group, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def consume_once(self, *, block_ms: int = 1000, count: int = 10) -> int:
        """Claim abandoned entries first, then consume new entries; return count handled."""
        if block_ms < 0 or count < 1:
            raise ValueError("block_ms must be nonnegative and count must be positive")
        self.ensure_group()
        claimed = self._redis.xautoclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=self._claim_idle_ms,
            start_id="0-0",
            count=count,
        )
        messages: list[StreamEntry] = claimed[1] if claimed and len(claimed) > 1 else []
        if not messages:
            response = self._redis.xreadgroup(
                self._group,
                self._consumer,
                {self._stream: ">"},
                count=count,
                block=block_ms,
            )
            messages = [entry for _, entries in response for entry in entries]
        for message_id, fields in messages:
            self._process(message_id, fields)
        self._update_queue_depth()
        return len(messages)

    def _update_queue_depth(self) -> None:
        """Publish unread plus pending entries for this consumer group."""
        try:
            groups = self._redis.xinfo_groups(self._stream)
            group_info = next(
                (
                    info
                    for info in groups
                    if info.get("name") in {self._group, self._group.encode()}
                ),
                None,
            )
            if group_info is not None:
                pending_value = group_info.get("pending")
                lag_value = group_info.get("lag")
                pending = int(pending_value) if isinstance(pending_value, (int, str, bytes)) else 0
                lag = int(lag_value) if isinstance(lag_value, (int, str, bytes)) else 0
                self._metrics.queue_depth.set(pending + lag)
        except Exception:
            # Queue-depth scraping is best-effort and must not fail message delivery.
            return

    def _process(self, message_id: str, fields: dict[str, str]) -> None:
        started = time.perf_counter()
        try:
            self._process_message(message_id, fields)
        finally:
            self._metrics.event_processing_latency.observe(time.perf_counter() - started)

    def _process_message(self, message_id: str, fields: dict[str, str]) -> None:
        raw_event = fields.get("event")
        attempt = 0
        try:
            attempt = int(fields.get("attempt", "0"))
            if attempt < 0:
                raise ValueError("attempt count cannot be negative")
            if raw_event is None:
                raise ValueError("stream entry is missing event field")
            event = EventEnvelope[EventPayload].model_validate_json(raw_event)
            with self._uow_factory(self._group) as uow:
                if not uow.is_processed(event.event_id):
                    self._handler(event, uow)
                    uow.mark_processed(event.event_id)
        except Exception as exc:
            self._metrics.events_failed_total.inc()
            if attempt >= self._max_retries:
                self._redis.xadd(
                    self.dead_letter_stream,
                    {
                        "event": raw_event or "",
                        "attempt": str(attempt),
                        "error_type": type(exc).__name__,
                        "source_stream": self._stream,
                        "source_entry_id": message_id,
                    },
                )
                self._redis.xack(self._stream, self._group, message_id)
            else:
                delay = min(self._max_backoff, self._base_backoff * (2**attempt))
                delay = min(self._max_backoff, delay * (0.5 + self._jitter()))
                self._sleep(delay)
                self._redis.xadd(
                    self._stream,
                    {"event": raw_event or "", "attempt": str(attempt + 1)},
                )
                self._redis.xack(self._stream, self._group, message_id)
            return
        # Keep acknowledgement outside the handler exception path. If Redis is
        # unavailable after commit, the pending entry is reclaimed and the inbox
        # suppresses the already-committed handler effects.
        self._redis.xack(self._stream, self._group, message_id)
        self._metrics.events_processed_total.inc()
