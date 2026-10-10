# ShopStack demo application

ShopStack is a small distributed commerce workload intended to generate realistic telemetry for OpsMind. It has an API gateway, user service, order service, inventory service, payment service, PostgreSQL, Redis, and a Jaeger OTLP collector. It runs normally with all fault controls disabled.

## Start with Docker Compose

From the repository root:

```bash
cp .env.example .env
# Replace SHOPSTACK_DB_PASSWORD and GRAFANA_ADMIN_PASSWORD with local-only passwords.
make up
```

Compose starts its dependencies. Services are available at:

| Component | Local address |
|---|---|
| API Gateway | `http://localhost:8080` |
| User Service | `http://localhost:8101` |
| Order Service | `http://localhost:8102` |
| Inventory Service | `http://localhost:8103` |
| Payment Service | `http://localhost:8104` |
| Grafana | `http://localhost:3300` |
| Prometheus | `http://localhost:9090` |
| Jaeger UI | `http://localhost:16686` |

Every application service exposes `/health`, `/ready`, and `/metrics`. Grafana provisions the seven OpsMind dashboards and Prometheus provisions the SLI alerts. The deterministic demo user is `11111111-1111-4111-8111-111111111111`; seeded SKUs are `SKU-COFFEE`, `SKU-TEA`, and `SKU-MUG`. See [observability.md](observability.md) for metric definitions, alert conditions, and dashboard details.

## Generate traffic

Install the repository dependencies with `uv sync --locked --extra dev`, then run:

```bash
uv run python scripts/shopstack_traffic.py --requests 100 --concurrency 8
```

Or send one request directly:

```bash
curl -i http://localhost:8080/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-order-001' \
  -d '{"user_id":"11111111-1111-4111-8111-111111111111","sku":"SKU-COFFEE","quantity":2,"amount":"18.50"}'
```

The request path is gateway → order → user lookup → inventory reservation → payment authorization. Repeating an idempotency key with the same body returns the original order; reusing it with a different body returns a conflict. The order service releases inventory after a known payment failure. If payment times out or returns an ambiguous server error, the order is marked `COMPENSATION_REQUIRED` and inventory remains reserved because payment may have succeeded.

## Failure injection

Faults are configured independently per service through Compose environment variables. Set a variable in `.env` and recreate the target container. For example, simulate a 429 response from payment:

```dotenv
SHOPSTACK_PAYMENT_FAILURE_HTTP_429_RATE=0.25
```

```bash
docker compose --profile shopstack up -d --force-recreate shopstack-payment-service
```

The available suffixes are `GATEWAY`, `USER`, `ORDER`, `INVENTORY`, and `PAYMENT`. Each supports:

| Variable suffix | Effect |
|---|---|
| `FAILURE_LATENCY_MS` | Adds request latency |
| `FAILURE_HTTP_500_RATE` | Randomly returns HTTP 500 |
| `FAILURE_HTTP_429_RATE` | Randomly returns HTTP 429 and `Retry-After` |
| `FAILURE_MEMORY_MB` | Reserves the configured memory when the process starts |
| `FAILURE_CPU_STRESS_MS` | Burns CPU for the configured time on each application request |
| `FAILURE_DEPENDENCY_TIMEOUT_MS` | Sets outbound HTTP timeout to this value |
| `FAILURE_DB_CONNECTIONS` | On database-backed services, holds pool connections while a request runs (maximum five) |

For example, `SHOPSTACK_ORDER_FAILURE_LATENCY_MS=250` affects only the order service. Values default to zero. Keep memory settings within the container limit. The database pool has five connections, so setting the connection fault to five can make that service's database requests time out.

## Kubernetes

Build and load the shared image into your local cluster, for example:

```bash
docker build -f infra/docker/Dockerfile -t opsmind-shopstack:local .
kubectl create namespace shopstack
kubectl -n shopstack create secret generic shopstack-db --from-literal=password='replace-this-locally'
kubectl apply -k infra/kubernetes/shopstack
kubectl -n shopstack port-forward service/shopstack-api-gateway 8080:8000
```

The manifests deploy PostgreSQL, Redis, Jaeger, and each application service. The database password is read from the `shopstack-db` Secret, which is deliberately not committed. For an existing cluster, use an external secret manager and replace the sample local database, cache, and tracing deployments with managed services. Configure fault controls with `kubectl set env deployment/shopstack-payment-service SHOPSTACK_FAILURE_HTTP_500_RATE=0.1` and remove them by setting the value back to `0`. The sample Jaeger endpoint uses insecure local OTLP; set `OTEL_EXPORTER_OTLP_INSECURE=false` when sending traces to a TLS-protected collector.

Stop the local Compose deployment with `docker compose down`; use `docker compose down -v` only when you also want to delete its persisted databases.
