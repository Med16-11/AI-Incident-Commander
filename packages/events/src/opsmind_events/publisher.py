"""Redis Streams event publisher."""

from opsmind_models.events import EventEnvelope
from pydantic import BaseModel

from opsmind_events.metrics import EventMetrics
from opsmind_events.redis_client import RedisStreamsClient


class RedisStreamPublisher:
    """Publish versioned events to a Redis Stream."""

    def __init__(
        self,
        redis: RedisStreamsClient,
        stream: str,
        metrics: EventMetrics | None = None,
        max_length: int | None = None,
    ) -> None:
        if not stream:
            raise ValueError("stream name cannot be empty")
        if max_length is not None and max_length < 1:
            raise ValueError("max_length must be positive")
        self._redis = redis
        self._stream = stream
        self._metrics = metrics or EventMetrics()
        self._max_length = max_length

    def publish[PayloadT: BaseModel](self, event: EventEnvelope[PayloadT]) -> str:
        """Append one serialized event and return its Redis stream entry ID."""
        fields = {"event": event.model_dump_json()}
        if self._max_length is None:
            entry_id = self._redis.xadd(self._stream, fields)
        else:
            entry_id = self._redis.xadd(
                self._stream,
                fields,
                maxlen=self._max_length,
                approximate=True,
            )
        self._metrics.events_published_total.inc()
        return entry_id
