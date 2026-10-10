"""Prometheus metrics for the event transport."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class EventMetrics:
    """Metrics wrapper with injectable registry for isolated tests."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        target = registry or CollectorRegistry()
        self.registry = target
        self.events_published_total = Counter(
            "events_published_total", "Events published to Redis Streams", registry=target
        )
        self.events_processed_total = Counter(
            "events_processed_total", "Events processed successfully", registry=target
        )
        self.events_failed_total = Counter(
            "events_failed_total", "Event processing failures", registry=target
        )
        self.event_processing_latency = Histogram(
            "event_processing_latency",
            "Time spent processing an event in seconds",
            registry=target,
        )
        self.queue_depth = Gauge(
            "queue_depth",
            "Unread and pending events for a consumer group",
            registry=target,
        )
