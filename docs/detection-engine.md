# Detection Engine

The Detection Engine polls Prometheus and Loki, applies deterministic rules, and publishes typed events to the `opsmind:events` Redis Stream. It does not call an LLM.

## Signal sources

- Prometheus instant queries normalize request rate, 5xx ratio, p95 latency, and dependency error ratios into `MetricObservation` values.
- Loki range queries read structured error logs from Docker services or another configured Loki endpoint. ShopStack and OpsMind Compose service labels are normalized to the service names used by metrics.
- Source failures are logged independently; one unavailable source does not prevent processing the other source.

The local Compose observability profile runs Loki and Grafana Alloy. Alloy tails Docker container logs and labels them with the Compose service name. It mounts the Docker socket to read logs; use a Kubernetes log collector and scoped permissions outside local development.

## Deterministic detectors

| Detector | Behavior | Default |
|---|---|---|
| Static threshold | Flags values outside configured lower/upper bounds. | Error rate `>0.05`; dependency error rate `>0.10`; p95 `>1.0s`. |
| Rolling baseline | Flags a value more than 50% above or below a 30-sample mean after 8 samples. | 30 observations, 8-sample warm-up. |
| Z-score | Flags values at least 3.5 population standard deviations from the rolling mean after warm-up. | `3.5`. |
| Error-rate | Applies error-specific detector attribution and static thresholds to HTTP 5xx ratios and dependency failure ratios. | Uses the static defaults above. |
| Latency | Applies latency-specific detector attribution and static threshold to p95 request latency. | `1.0s`. |
| Sudden traffic drop | Flags request rate at or below 50% of its warmed rolling mean. | 50% of baseline. |
| Repeated error logs | Groups normalized error fingerprints and emits after the occurrence threshold within the time window. | 5 matches in 60 seconds. |

Every metric/log anomaly event contains an anomaly signature, service, observation time, explanation, evidence values, and an integer score from 0 to 100. Multiple matching metric rules produce one event with all contributing detector names. Log fingerprints normalize UUIDs and numeric IDs to avoid creating separate groups for individual requests.

Thresholds and windows can be set with `OPSMIND_DETECTION_*` environment variables. Rolling samples are in process memory and warm up again after a service restart.

## Correlation and duplicate suppression

The correlator groups error, latency, dependency, and log anomalies by normalized root service and service-degradation class. A dependency anomaly is attributed to its dependency, so an Order Service failure attributed to Payment Service groups with Payment Service latency and error anomalies. Traffic-drop alerts use their own class. Repeated copies of the same anomaly signature do not increase the signal count or create another incident.

Distinct signals within the default 120-second window update the same incident with `IncidentUpdated`; only the initial signal emits `IncidentCreated`. Events carry the same incident aggregate ID and causal anomaly ID. A Redis `SET NX EX` key suppresses duplicate anomaly stream entries within a polling bucket and duplicate `IncidentCreated` events for 15 minutes. After the deduplication TTL, a later occurrence can create a new incident.

## Events

- `MetricAnomalyDetected` uses `MetricAnomalyPayload`.
- `LogAnomalyDetected` uses `LogAnomalyPayload`.
- A new correlation group emits `IncidentCreated` with score, signature, service, and the initial anomaly ID.
- Additional unique correlated signals emit `IncidentUpdated` with the same aggregate ID and the accumulated anomaly IDs.

The detection service polls on startup and every `OPSMIND_DETECTION_POLL_INTERVAL_SECONDS` (default 30 seconds). Its health and Prometheus metrics are exposed on the standard service endpoints.

## Tests

`tests/unit/test_detection.py` covers baseline false positives, static/rolling/z-score/traffic/log detectors, duplicate alerts, related and unrelated service/dependency incidents, the correlation window, and typed incident events. `tests/unit/test_detection_sources.py` covers Prometheus and Loki response normalization. `tests/unit/test_detection_runner.py` checks repeated polling does not republish the same anomaly or incident.
