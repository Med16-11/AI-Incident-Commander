# Local development

## Prerequisites

- Docker Desktop or Docker Engine with Compose v2, running
- Python 3.12+ and `uv`
- Node.js/npm only when building or changing the frontend

## Start the local system

```bash
cp .env.example .env
openssl rand -hex 32
```

Put the generated value in `.env` as `OPSMIND_LOCAL_DEMO_TOKEN`. Keep
`OPSMIND_LOCAL_DEMO_AUTH_ENABLED=true` and `OPSMIND_ENVIRONMENT=local`; replace the
ShopStack PostgreSQL and Grafana password placeholders with local-only values. Then run:

```bash
uv sync --locked --extra dev
make up
make migrate
```

`make up` builds and starts the Compose profiles in the background. The services include
OpsMind, ShopStack, the two PostgreSQL/Redis pairs, Prometheus, Loki, Grafana, the OpenTelemetry
Collector, Alloy, and Jaeger. `make logs` follows container logs.

Open the OpsMind console at <http://localhost:5173>, choose **Connect session**, and paste the
same generated token. The token maps to the local demo tenant; the UI keeps it in memory only,
so re-enter it after a full page reload. This local adapter is not an identity provider. The
API and remediation services continue to deny requests in staging and production unless a real
identity provider is injected.

## Run the demo

```bash
make demo
```

The demo creates a faulty Payment Service scenario, sends traffic, detects and investigates the
incident, requests terminal approval for a simulated rollback, and writes
`.opsmind/demo/latest-report.md`. The resolved incident appears in **Historical incidents** or
under **All** on the incident dashboard; the default dashboard filter shows active incidents.
Prometheus/Grafana display metrics, and Jaeger displays request traces. They do not display the
incident record itself.

## Local endpoints

- OpsMind console: <http://localhost:5173>
- API/OpenAPI: <http://localhost:8000/docs>
- Grafana: <http://localhost:3300>
- Prometheus: <http://localhost:9090>
- Jaeger: <http://localhost:16686>
- ShopStack gateway: <http://localhost:8080>

## Quality checks

```bash
make check
make web-install
make web-build
cd apps/web && npm test
```

The Kubernetes adapter integration test is separate and requires a configured local cluster.
The Compose PostgreSQL trust-auth setup binds to loopback and is for local development only.

## Stop services

```bash
make down
```

This preserves local database volumes. `docker compose down -v` removes the database data too.
