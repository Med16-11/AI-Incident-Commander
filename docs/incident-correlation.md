# Incident Correlation Engine

The Incident Engine collects source-backed evidence around a persisted incident and builds a chronological timeline. The implementation is deterministic and does not call an LLM. Every evidence candidate must have a source, timezone-aware timestamp, service, evidence type, raw reference, normalized summary, and confidence in `[0, 1]` before it can be stored.

## Trigger and collection window

Call `POST /incidents/{incident_id}/correlate` with `Authorization: Bearer $OPSMIND_CORRELATION_API_TOKEN`. The endpoint is disabled when the token is unset and rejects requests without a matching bearer token. It loads the incident and dependency graph from PostgreSQL, then collects data from PostgreSQL, Prometheus, and Loki.

For local Compose, invoke the internal service at `http://localhost:8001`:

```bash
curl -X POST "http://localhost:8001/incidents/$INCIDENT_ID/correlate" \
  -H "Authorization: Bearer $OPSMIND_CORRELATION_API_TOKEN"
```

The default window is 10 minutes before and 5 minutes after `detected_at`; configure it with `OPSMIND_CORRELATION_BEFORE_SECONDS` and `OPSMIND_CORRELATION_AFTER_SECONDS`. Since post-detection evidence arrives later, the response includes `collection_complete`. Call the endpoint again after `window_end` to collect the completed window. Repeated calls are idempotent: stable content hashes suppress duplicate evidence and timeline rows.

## Correlated sources

- **Anomalies and metric snapshots:** incident events and stored metric snapshots, plus Prometheus HTTP error-rate, dependency error-rate, and p95 latency threshold breaches.
- **Logs:** persisted log records and Loki range results for the incident service and known dependencies. Structured `event_type`/`kind` values identify configuration, deployment, restart, and infrastructure events.
- **Deployments:** persisted deployment records, including version and commit reference.
- **Service dependencies:** dependency edges active at incident detection time.
- **Pod restarts:** Prometheus `kube_pod_container_status_restarts_total` increases for pods whose names identify the incident service or a known dependency.
- **Infrastructure events:** unavailable Prometheus targets attributed to a known service, plus stored events/logs classified as infrastructure events.
- **Configuration changes:** persisted incident events and structured logs classified as configuration changes.

Prometheus queries use configured static anomaly thresholds and bounded query steps. Loki is limited to 1,000 lines per request by default. Database results are tenant-scoped and time-bounded. Source failures are isolated: successful sources still persist evidence, and failed source names are returned for retry. A later call can fill in evidence from a recovered source.

## Persistence and evidence safety

`incident_evidence` stores source, service, type, timestamp, external reference, normalized summary, confidence, attributes, and a stable content hash. `incident_timeline` has one row per newly stored evidence record and links to it through `evidence_id`; rows are ordered by occurrence time. Tenant, incident, and content hash uniqueness plus incident row locking protect repeated and concurrent collection.

Summaries are derived from source records or deterministic Prometheus query values. Common bearer tokens, credential assignments, and JWT-shaped values are redacted from normalized summaries. Raw references point back to the source record/query; no model-generated claim is accepted as evidence.

## Tests

`tests/unit/test_incident_correlation.py` exercises a synthetic deployment → metrics → dependent-service logs → restart scenario, time-window filtering, source failure isolation, persistence/idempotency, provenance validation, credential redaction, Prometheus/Loki normalization, and the authenticated trigger endpoint.
