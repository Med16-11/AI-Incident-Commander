# OpsMind benchmarks

Run reproducible local benchmarks from the repository root with the Compose stack
already running and database migrations applied:

```bash
uv sync --locked --extra dev
uv run python scripts/benchmark_opsmind.py --sizes 10,100 --concurrency 20
```

This runs 10 and 100 incident scenarios sequentially, then runs the larger scenario
with up to 20 incidents in flight. Raw measurements are printed and saved under
`docs/benchmark-results/<run-id>.json`. Override `--sizes`, `--concurrency`, or `--output`
to change the workload or result path. The script requires the local `.env`, PostgreSQL,
and Redis ports configured by Compose. The benchmark tenant is unique per run. Incident
and remediation data plus temporary Redis streams are deleted after measurement;
append-only audit records are retained under that temporary tenant as the benchmark's
audit trail.

## Measurement definitions and scope

- **Anomaly detection latency:** elapsed time in the deterministic `detect_metric` call
  for labeled synthetic error-rate observations.
- **Incident creation latency:** time for a real `POST /incidents` handler invocation
  through the ASGI transport to return a persisted incident.
- **Investigation duration and tool calls:** time and calls for the bounded investigation
  engine using one synthetic metric evidence tool and a deterministic local provider.
- **Remediation latency:** proposal through policy validation, distinct-human approval,
  and execution through the real remediation service handlers; the executor is a
  deterministic no-op benchmark adapter.
- **False-positive and false-negative rates:** calculated from 200 labeled synthetic
  metric cases (100 above and 100 below the configured static threshold). These are
  detector results on this generated set, not estimates of production accuracy.
- **MTTR:** elapsed time from before the create request until the incident reaches
  `RESOLVED`, including the stub investigation and remediation steps.
- **Event throughput:** measured Redis Streams publish count divided by publish wall
  time; the script verifies stream length before deleting its temporary stream.
- **API throughput:** HTTP request count divided by scenario wall time, covering the
  incident create/state transition requests and remediation proposal/approval/execute
  requests. API calls use an in-process ASGI transport, so the measurement excludes TCP,
  reverse proxy, and load balancer overhead.

The sequential and concurrent scenarios use the same handlers, PostgreSQL, and Redis.
The model provider and infrastructure executor are intentionally local deterministic
adapters; these results do not represent external LLM, Slack, or Kubernetes latency.
Measured JSON artifacts are retained in `docs/benchmark-results/`.

## Measured results

Completed on **2026-10-06**. The raw JSON artifact contains the run ID, full percentiles,
request/status counts, and environment data.
Host: macOS 15.3.1, arm64, Python 3.14.5; local Compose PostgreSQL and Redis. All
HTTP results use in-process ASGI transport. The 100-concurrent case caps in-flight
incident flows at 20. Values below come from the raw run artifact
[`6646ad29-0542-4386-a1d8-217e61658a52.json`](benchmark-results/6646ad29-0542-4386-a1d8-217e61658a52.json).

| Workload | Incidents | Flow throughput (incidents/s) | API throughput (req/s) | Detection mean / p95 (ms) | Incident create mean / p95 (ms) | Investigation mean / p95 (ms) | Tool calls (mean) | Remediation mean / p95 (ms) | MTTR mean / p95 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Sequential | 10 | 11.490 | 103.411 | 0.106 / 0.098 | 12.599 / 14.895 | 1.990 / 2.090 | 1 | 27.675 / 39.396 | 86.987 / 121.997 |
| Sequential | 100 | 12.974 | 116.765 | 0.065 / 0.077 | 8.877 / 11.447 | 1.934 / 2.155 | 1 | 24.895 / 32.257 | 77.044 / 104.805 |
| Concurrent (limit 20) | 100 | 26.372 | 237.348 | 0.118 / 0.518 | 86.942 / 102.936 | 10.641 / 22.951 | 1 | 238.364 / 292.421 | 742.497 / 843.596 |

| Redis event publishes | Persisted | Throughput (events/s) | Publish mean / p95 (ms) |
| ---: | ---: | ---: | ---: |
| 10 | 10 | 2,182.77 | 0.368 / 0.240 |
| 100 | 100 | 2,908.12 | 0.330 / 0.379 |

The detector labeled all 200 generated samples as expected: 100 true positives, 100
true negatives, zero false positives, and zero false negatives (0% rates on this
synthetic set only). The API recorded 90 requests for the 10-flow run and 900 requests
for each 100-flow run; all returned HTTP 200 or 201.

In the measured 20-way workload, incident creation mean latency rose from 8.877 ms in
the sequential 100-flow run to 86.942 ms, and remediation mean latency rose from
24.895 ms to 238.364 ms. End-to-end MTTR mean rose from 77.044 ms to 742.497 ms, while
overall flow throughput rose from 12.974 to 26.372 incidents/s. This is the observed
behavior on this local setup; it is not a capacity claim for production infrastructure.
