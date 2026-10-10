# OpsMind event transport

The `opsmind_events` package publishes versioned event envelopes to Redis
Streams and consumes them through named consumer groups. It expects a synchronous
`redis.Redis` client configured from `OPSMIND_REDIS_URL`.

## Delivery and idempotency

Delivery is **at least once**. A handler runs inside the supplied transactional
unit of work; its database writes and `processed_events` inbox record must share
the same transaction. The consumer acknowledges the Redis entry only after that
transaction commits. If the process commits and dies before `XACK`, the next
consumer reclaims the pending entry, observes the inbox record, skips the
handler, and acknowledges it. Inbox keys are scoped by consumer group so each
independent projection can process the same event once.

Do not perform non-transactional external side effects directly in a handler.
Stage them in a transactional outbox and give the downstream operation its own
idempotency key. Redis delivery alone cannot make an external API call exactly
once.

Handler failures are re-enqueued with an incremented attempt count and
exponential delay. Once `max_retries` is exceeded, the original envelope and
failure details are appended to `<stream>:dead-letter`. Configure Redis with
persistence and retention appropriate to the deployment; stream trimming must
not remove unprocessed entries. The publisher does not trim streams by default
because Redis `MAXLEN` trimming can delete pending events. If `max_length` is
explicitly configured, its retention must exceed the maximum backlog and must
not remove pending work.

## Metrics

`EventMetrics` exports `events_published_total`, `events_processed_total`,
`events_failed_total`, `event_processing_latency`, and `queue_depth`. Queue
depth is the consumer group's unread lag plus pending deliveries; acknowledged
stream history is excluded. Pass a shared `EventMetrics` instance to publishers
and consumers and expose its Prometheus registry through the service metrics endpoint.
