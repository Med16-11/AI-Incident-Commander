# OpsMind reliability review

## Scope and reproduction

Failure paths were exercised with deterministic fault doubles (HTTP timeouts, unavailable
stores/adapters, malformed model responses) and SQLite-backed API integration tests. The
test suite does not stop the running Compose services or a real Slack, LLM, Prometheus, or
Kubernetes endpoint. Kubernetes live-cluster tests remain opt-in. Reproduce the regression
coverage with:

```bash
uv run pytest -q
```

## Failure behavior

| Failure injected | Observed and final behavior | Regression coverage |
| --- | --- | --- |
| Redis unavailable | Stream operations raise to the owning worker loop. Detection releases its deduplication key when publish fails, so the next poll can retry. A consumer leaves unread entries available; after Redis recovers, it consumes them. Notification workers log the failure and retry after a short delay. | `test_redis_outage_leaves_stream_entry_available_for_recovery`; `test_redis_publish_failure_releases_dedup_key_for_next_poll` |
| PostgreSQL unavailable | Readiness returns 503. API database-backed requests now return a sanitized 503 `storage_unavailable` response with the request correlation ID; incident and remediation engines also report not-ready. Internal database details are not returned. | `test_api_readiness_returns_503_when_database_is_unavailable`; `test_api_database_request_returns_sanitized_503_when_storage_is_unavailable`; `test_database_backed_service_readiness_returns_503_when_database_is_unavailable` |
| LLM unavailable | Provider transport/protocol failures become `ProviderFailure`. The investigation endpoint returns 502 and persists the run as `FAILED`; if the execution deadline expires before a usable analysis is available, it returns 504 / `TIMED_OUT`. No unsupported analysis is returned. | `test_llm_network_timeout_becomes_provider_failure`; `test_llm_outage_returns_upstream_failure_and_marks_run_failed`; `test_execution_deadline_stops_a_slow_provider` |
| Prometheus unavailable | The detection cycle logs the source failure and continues with Loki results. Metric coverage is absent for that poll; the service poll loop continues at its configured interval. | `test_prometheus_timeout_still_processes_loki_signals` |
| Slack unavailable | Posting raises and the event is not marked processed; the consumer retries with exponential backoff and eventually sends exhausted messages to the dead-letter stream. The incident thread root is saved only after Slack returns a timestamp. Stable event IDs are sent as `client_msg_id`. | `test_slack_delivery_timeout_does_not_mark_thread_as_created`; `test_failed_event_retries_with_exponential_backoff`; `test_exhausted_retries_are_sent_to_dead_letter_stream` |
| Kubernetes API unavailable | Retryable HTTP/network failures receive bounded retries (initial call plus configured retries), then propagate. The remediation boundary records adapter failures as `FAILED`; it does not claim success. A timeout after a remote write can still be ambiguous, so operators should inspect live deployment state before manually retrying. | `test_unavailable_kubernetes_api_retries_then_fails_closed`; `test_remediation_timeout_is_audited_and_marked_failed` |
| Duplicate events | The inbox key is `(consumer group, event ID)`. Repeated delivery skips an already committed handler effect; the inbox transaction and handler writes commit together. | `test_duplicate_delivery_does_not_repeat_handler_side_effect`; `test_sqlalchemy_inbox_persists_idempotency_between_uow_instances` |
| Out-of-order evidence | Correlation filters to the configured incident window, deduplicates evidence, then orders the timeline by timestamp with source/reference tie-breakers. Redis preserves append order, but the generic consumer does not sequence events by `aggregate_version`; producers must append aggregate events in order. | `test_synthetic_payment_incident_collects_correlated_evidence_chronologically` (source records are deliberately reversed) |
| Consumer crash | Unacknowledged stream entries remain pending and are reclaimed after the idle threshold. The transactional inbox prevents a committed handler side effect from repeating after recovery. | `test_restart_reclaims_committed_event_without_repeating_side_effect` |
| Network timeout | Prometheus/Loki and LLM clients have bounded HTTP timeouts. Detection continues with the other source; investigation records missing/failed evidence or a provider failure. Kubernetes and Slack timeouts enter their adapter/consumer failure paths. | `test_prometheus_timeout_still_processes_loki_signals`; `test_llm_network_timeout_becomes_provider_failure`; `test_slack_delivery_timeout_does_not_mark_thread_as_created`; `test_unavailable_kubernetes_api_retries_then_fails_closed` |
| Malformed LLM response | Invalid provider envelopes raise `ProviderFailure`; malformed final JSON or schema violations raise `InvalidAnalysis`. The API marks the run failed and does not store model output as a valid analysis. | `test_malformed_provider_response_envelope_becomes_provider_failure`; `test_malformed_structured_llm_response_is_rejected`; `test_fabricated_evidence_id_is_rejected` |
| Conflicting evidence | The investigator can cite both records as alternatives and must keep the conclusion evidence-linked; the confidence validator rejects unsupported high-confidence root causes. | `test_contradictory_evidence_can_be_reported_as_alternatives` |
| Expired approval | Approval after expiry returns 410 and persists the expired decision. Execution without an active approval is rejected and the executor is not called. | `test_expired_approval_is_persisted_and_never_executed` |
| Unauthorized remediation | The server-side permission check and human-only approval/execution boundary reject unauthorized callers before adapter execution. | `test_approval_security_boundaries_and_rejection_are_enforced`; `test_self_approval_is_rejected_and_dry_run_does_not_execute` |
| Remediation timeout | Adapter exceptions are recorded as a failed execution with completion time and an audit entry. The response contains a generic failure result, not exception detail. Repeating the same idempotency key returns the recorded execution instead of executing again. | `test_remediation_timeout_is_audited_and_marked_failed`; `test_ai_can_propose_but_human_must_approve_and_execute` |

## Guarantees and limits

- Event processing is at-least-once. Handler side effects must participate in the supplied
  unit of work; external effects such as Slack use stable event identifiers and may still
  require downstream deduplication.
- Redis outages are retried by service loops, but Redis Streams are not a durable substitute
  for a separately replicated event log. Delivery resumes only after Redis is available.
- Dead-letter entries contain the raw serialized event, attempt count, source stream/entry,
  and error type. They do not contain exception text that could expose sensitive data.
- Timeline ordering is event-time ordering after evidence collection. The general consumer
  does not buffer missing aggregate versions or reorder retried events.
- A Kubernetes write timeout is an ambiguous outcome: Kubernetes may have applied the write
  even if the client did not receive the response. OpsMind stores the action as failed and
  requires inspection before a manual retry; it never automatically repeats a timed-out
  remediation outside the adapter's bounded retry policy.
- Mocked-provider and adapter tests establish failure handling, not availability of external
  systems or production SLOs. The Kubernetes integration test requires an explicitly enabled
  local cluster and was skipped in the default suite.

## Change made during review

The incident API previously mapped unhandled SQLAlchemy errors to its generic 500 response,
even though the readiness endpoint correctly returned 503. SQLAlchemy failures now return
the structured, sanitized `503 storage_unavailable` response. Regression coverage was added
for the outage cases above, including Redis recovery, source fallbacks, timeouts, ordering,
malformed model output, and audit behavior.
