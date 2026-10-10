# ChaosLab

ChaosLab runs bounded failure experiments against the local ShopStack Docker Compose profile. It has no access to production Kubernetes clusters and does not issue Kubernetes API calls. The `pod-crash-loop` case reproduces repeated container startup failure in Compose; CPU, memory, latency, and packet-loss cases use ShopStack's explicit demo-only injection settings.

## Setup

Install the project (or refresh its editable environment after changing the CLI):

```bash
uv sync --locked --extra dev
make up
```

The CLI requires the Docker Compose v2 plugin and a running Docker daemon. Run it from the repository root. Experiments are restricted to named ShopStack services under the `shopstack` Compose profile. Durations default to 30 seconds and can be set from greater than zero through 300 seconds.

## Commands

```bash
uv run opsmind chaos list
uv run opsmind chaos run payment-latency
uv run opsmind chaos run payment-outage
uv run opsmind chaos run bad-deployment --duration 15
uv run opsmind chaos run cpu-saturation --dry-run
uv run opsmind chaos results --limit 10
```

Each run receives a UUID experiment ID. Results append as JSON Lines to `.opsmind/chaos/results.jsonl` by default. Override the file with `--results PATH`. Each record includes target, failure type, requested runtime measurement, timestamps, expected symptoms, rollback procedure, outcome, and any execution or rollback error.

## Experiment catalogue

| Name | Target | Failure type | Expected symptoms | Rollback |
|---|---|---|---|---|
| `payment-latency` | Payment Service | 1.2 s request latency | Payment p95 and order latency rise | Recreate payment with original environment |
| `payment-outage` | Payment Service | Service stopped | Payment readiness fails; orders fail | Start payment and wait for readiness |
| `inventory-outage` | Inventory Service | Service stopped | Inventory readiness fails; reservation fails | Start inventory and wait for readiness |
| `redis-outage` | ShopStack Redis | Service stopped | Redis probes fail; cache errors rise | Start Redis and wait for health |
| `postgres-connection-exhaustion` | Order Service | All five local DB pool connections held per request | Pool waits and request timeouts under concurrent traffic | Recreate order with the fault setting cleared |
| `cpu-saturation` | Order Service | 750 ms CPU work per business request | CPU and request latency rise | Recreate order with the fault setting cleared |
| `memory-pressure` | Payment Service | 128 MiB reserved at startup | Container working set rises | Recreate payment with the fault setting cleared |
| `pod-crash-loop` | Payment Service container | Startup crash | Container restarts; readiness fails | Recreate payment with crash setting cleared |
| `network-latency` | Payment Service request path | 1.5 s injected request latency | Payment and upstream order latency rise | Recreate payment with original environment |
| `network-packet-loss` | Payment Service request path | 20% simulated request drops | Injected HTTP 503s and dependency errors rise | Recreate payment with the fault setting cleared |
| `bad-deployment` | Payment Service | Faulty 100% HTTP 500 release configuration | Payment 5xx rate rises; orders fail | Recreate payment with original environment |
| `dependency-timeout` | Order Service | 100 ms outbound HTTP timeout | Downstream calls time out; order errors rise | Recreate order with the fault setting cleared |

The network cases deliberately simulate symptoms at the ShopStack HTTP request boundary. Packet loss is represented by sampled requests receiving HTTP 503, not by modifying the host network. `bad-deployment` applies a faulty service configuration rather than publishing or replacing an image. These choices keep the local demo bounded and reversible.

## Safe execution and cleanup

Fault settings are supplied only to the target service's Compose recreation; the CLI does not edit `.env`. Service and dependency outage experiments restart their target in a `finally` cleanup path. Every run records its result, including failures and rollback errors. Review a `rollback_failed` result immediately and use its documented rollback procedure. A forced process kill or Docker daemon failure can prevent cleanup, so the corresponding fault environment settings should be reset and the named service recreated before continuing.

Run traffic in another terminal during an experiment to produce request-level symptoms:

```bash
uv run python scripts/shopstack_traffic.py --requests 200 --concurrency 8
```

Observe the OpsMind Grafana dashboards at `http://localhost:3300`, ShopStack metrics at the service `/metrics` endpoints, and traces in Jaeger at `http://localhost:16686`. Detection and incident automation still depend on the OpsMind services, database migrations, and authentication/provider configuration being healthy.
