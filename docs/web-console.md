# OpsMind web console

The React + TypeScript console is in `apps/web`. It provides an incident dashboard, incident detail workspace, service topology, investigation timeline, remediation center, historical incident archive, and system health view.

## Run it

The normal local stack builds the web console with the rest of Compose:

```bash
make up
make migrate
```

Open <http://localhost:5173>. For frontend-only development, install the locked dependencies and start Vite:

```bash
make web-install
make web-dev
```

Vite proxies `/api` to the incident API, `/remediation-api` to the remediation engine, and `/prometheus` to Prometheus. Proxy targets can be changed with `OPSMIND_API_PROXY_TARGET`, `OPSMIND_REMEDIATION_PROXY_TARGET`, and `OPSMIND_PROMETHEUS_PROXY_TARGET`. The production web image uses Nginx to provide the same-origin reverse proxy and SPA route fallback.

## Authentication and authorization

The console does not make authorization decisions. It sends an optional bearer credential from the current browser tab (or same-origin cookies) to the API, displays server `401`/`403` responses, and submits approval/rejection requests to the Remediation API. It does not infer a user's permissions or decide whether an action is safe. API and remediation services remain responsible for tenant isolation, permission checks, approval expiry, policy validation, and audit logging.

In local Compose, set `OPSMIND_LOCAL_DEMO_AUTH_ENABLED=true` and `OPSMIND_LOCAL_DEMO_TOKEN` to a private value generated with `openssl rand -hex 32`, rebuild the API and remediation containers, and paste the same value into **Connect session**. The local backend maps that token to the demo tenant. It is not an account system. The Kubernetes `dev` overlay supports the same opt-in using its runtime Secret. Staging and production keep the flag disabled and deny requests until a real identity provider is configured. The console keeps the bearer token in memory only; re-enter it after a full page reload. Use HTTPS outside local development.

## Live updates

Incident details and investigation timeline use the authenticated `GET /incidents/{id}/stream` SSE endpoint. The API checks `incidents:read` and tenant-scopes each timeline query. The browser uses `fetch` streaming so bearer credentials can be sent; it refreshes timeline data after updates or reconnects. SSE is a presentation channel only. Mutations still use the existing incident and remediation APIs.

## Data shown

- Incident detail loads the incident record, timeline, evidence, hypotheses, and remediation proposals from their existing endpoints. Metric charts, log panels, and trace panels are populated from evidence types returned by the API; absent evidence is shown as unavailable rather than fabricated.
- Topology is assembled from the service registry and each service's dependency endpoint. The graph arrow points from a caller to the service it depends on.
- The history page lists resolved and closed incident records. Historical context is labeled separately from current incident evidence.
- System health checks the API readiness endpoint and queries Prometheus `up`; registry entries without a corresponding health signal remain labeled as registered or unknown.

The UI's API contract currently relies on the evidence and service APIs already implemented by OpsMind. Metrics, logs, and traces are displayed when collected evidence includes those records; this console does not query raw observability backends directly.
