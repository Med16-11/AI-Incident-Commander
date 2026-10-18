# OpsMind and ShopStack observability

## Local stack

Copy `.env.example` to `.env`, replace both password placeholders, then run `make up`. The command starts ShopStack, OpsMind, Prometheus, Grafana, the OpenTelemetry Collector, Jaeger, and cAdvisor. Open Grafana at `http://localhost:3300` (credentials are `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD`), Prometheus at `http://localhost:9090`, and Jaeger at `http://localhost:16686`. Generate ShopStack traffic with `uv run python scripts/shopstack_traffic.py`.

The standalone container dashboard uses cAdvisor. Pod restart metrics come from Kubernetes `kube-state-metrics`; they are available when Prometheus is configured to scrape the cluster. A Compose service restart alert is represented by the unavailable-target alert.

The local observability profile mounts the Docker socket for Alloy and runs cAdvisor with elevated container privileges. Treat both as local-development-only components; do not deploy these Compose permissions to a shared host. In Kubernetes, use a cluster-approved log collector and metrics agent with scoped service accounts and host access.

## Metrics

All application metrics are scraped from `/metrics` every 15 seconds. Application labels use a bounded service name, HTTP method, normalized route template, status code, dependency name, operation, or success/error outcome. Request paths, identifiers, query strings, and exception text are never metric labels. Histograms expose `_bucket`, `_sum`, and `_count`; calculate p50/p95/p99 with `histogram_quantile` in PromQL.

| Metric | Meaning |
|---|---|
| `http_requests_total{service,method,route,status}` | Completed application HTTP requests. `/metrics` scrapes are excluded from the count. |
| `http_request_duration_seconds{service,method,route}` | Request latency histogram; supports rate, average, and percentile queries. |
| `http_in_flight_requests{service}` | Requests currently being handled. |
| `dependency_requests_total{service,dependency,operation,outcome}` | Outbound ShopStack dependency calls, including HTTP status and transport failures as bounded outcomes. |
| `dependency_request_duration_seconds{service,dependency,operation,outcome}` | Outbound dependency latency histogram. |
| `database_connections_checked_out{service}` | SQLAlchemy connections currently checked out from the pool. |
| `database_pool_size{service}` | Configured SQLAlchemy pool size. |
| `database_pool_overflow{service}` | Current overflow connections above the base pool. |
| `redis_operation_duration_seconds{service,operation,outcome}` | ShopStack Redis operation latency histogram. |
| `redis_operation_failures_total{service,operation}` | ShopStack Redis operation failures. |
| `container_cpu_usage_seconds_total` | Container CPU time from cAdvisor; use `rate()` for CPU cores. |
| `container_memory_working_set_bytes` | Container resident working set from cAdvisor. |
| `kube_pod_container_status_restarts_total` | Kubernetes container restart counter from kube-state-metrics. |
| `opsmind_incidents_created_total` | Incident records created through API requests or the event projection worker. This process counter resets when its container restarts. |
| `opsmind_incidents_closed_total` | Incidents closed through the API. |
| `opsmind_incidents_current{state}` | Durable incident counts read from PostgreSQL at each API metrics scrape; aggregate replicas with `max by (state)` because each API replica reports the same database view. |
| `opsmind_remediation_actions_total{action,outcome}` | Remediation API operations and outcome. |
| `events_published_total`, `events_processed_total`, `events_failed_total`, `event_processing_latency`, `queue_depth` | Event transport metrics exposed by the event metrics component when a publisher/consumer is running. |
| `opsmind_ingestion_requests_total` | HTTP POST requests received by the event-ingestion endpoint, including rejected requests. |
| `opsmind_ingestion_events_published_total` | Events appended to Redis by the Go ingestion service. |
| `opsmind_ingestion_events_duplicate_total` | Replayed event IDs returned without a second stream entry. |
| `opsmind_ingestion_events_invalid_total` | Invalid event envelopes rejected before Redis publication. |
| `opsmind_ingestion_publish_failures_total` | Redis publication failures returned as retryable HTTP 503 responses. |
| `opsmind_ingestion_unauthorized_total` | Requests rejected because the bearer credential is missing or invalid. |

Prometheus derives request rate with `rate(http_requests_total[5m])`; error rate is the rate of 5xx requests divided by total request rate. Latency percentiles are calculated from histogram buckets. These are estimates within configured histogram bucket resolution.

The deterministic detector polls only series whose Prometheus `job` label matches
`OPSMIND_DETECTION_PROMETHEUS_JOB_REGEX` (default `shopstack`). Set this to the intended
monitored workload jobs when deploying; this keeps OpsMind's own health probes from
opening incidents about its response-control services unless those jobs are explicitly
included.

## Dashboards

Grafana provisions seven dashboards from `infra/observability/grafana/dashboards`:

1. **System Overview** — request rate, 5xx ratio, p95, and scrape health.
2. **Service Health** — per-service request and error rates, in-flight requests, target availability.
3. **API Latency** — p50/p95/p99 by normalized route and service, plus request rate.
4. **Errors** — 4xx/5xx rates, server-error ratio, dependency failures.
5. **Dependencies** — call volume, dependency and Redis p95, database pool usage/overflow.
6. **Infrastructure** — container CPU/memory, Kubernetes pod restarts, database connections, target health.
7. **Incident Response** — incident creation/closure counts, current durable incident counts by state, API latency/errors, remediation outcomes, and event processing/queue metrics.

## Alerts

Alert rules are in `infra/observability/alerts.yml` and visible under Prometheus **Alerts**. The supplied rules are:

| Alert | Trigger | Hold time | Severity |
|---|---|---:|---|
| `HighErrorRate` | 5xx ratio above 5% over five minutes | 5m | critical |
| `HighLatency` | Service p95 above 1 second | 5m | warning |
| `ServiceUnavailable` | Prometheus cannot scrape an OpsMind or ShopStack target | 2m | critical |
| `RepeatedPodRestarts` | At least 3 restarts per container over 15 minutes | 2m | warning |
| `DependencyFailureRate` | Dependency error ratio above 10% over five minutes | 3m | warning |

Alert rules evaluate inside Prometheus. Configure an Alertmanager receiver for paging/chat delivery in a deployment environment; this repository deliberately keeps credentials and destinations out of the checked-in configuration. Tune thresholds against service SLOs before production use.

## Tracing and logs

ShopStack Gateway, Order, Inventory, Payment, and User services use OpenTelemetry FastAPI, HTTPX, and SQLAlchemy instrumentation. The default W3C Trace Context propagator injects `traceparent` on outbound HTTP calls and extracts it on inbound calls. Traces travel through the OTLP Collector to Jaeger. OpsMind services also create FastAPI server spans. Set `OTEL_EXPORTER_OTLP_ENDPOINT` to a trusted OTLP collector in another environment.

Application logs are JSON and carry `correlation_id`; when a span is active they also include `trace_id` and `span_id`. Correlation IDs continue through ShopStack's `X-Correlation-ID`/`X-Request-ID` headers. Do not place credentials, payload bodies, or high-cardinality request data in metric labels or logs.

## Tests

`tests/unit/test_observability.py` verifies normalized routes, counters, request latency histograms, and dependency failure exposition. ShopStack tests verify its service metrics endpoint emits request, database pool, dependency, and Redis metric families. Run `make check` for the full Python verification suite.
