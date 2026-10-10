# End-to-end incident response demo

Run the complete ShopStack incident scenario from the repository root:

```bash
cp .env.example .env
# Set local PostgreSQL/Grafana passwords and OPSMIND_LOCAL_DEMO_TOKEN in .env.
uv sync --locked --extra dev
uv run opsmind demo run
```

Generate the local-only console credential with `openssl rand -hex 32` and set the
result as `OPSMIND_LOCAL_DEMO_TOKEN` in `.env`, with `OPSMIND_LOCAL_DEMO_AUTH_ENABLED=true`.
The local Compose authentication backend activates only in `local` or `dev` environments
when this flag and token are present. After the
services start, open `http://localhost:5173`, choose **Connect session**, and paste the
same value. Other environments keep the deny-all default until a real identity provider
is configured.

Docker Desktop (or Docker Engine with Compose) must be running. The command builds and
starts OpsMind, ShopStack, Prometheus, Loki, and the other local observability services,
applies database migrations, injects a faulty Payment Service deployment, and sends
180 synthetic orders. Use `--requests 40` through `--requests 1000` to change the traffic
volume. The demo intentionally waits for the telemetry scrape and then for a single
correlated incident; first-time image builds can take several minutes.

At the remediation checkpoint, the command prints the proposed rollback and asks:

```text
Approve this rollback? [y/N]
```

Enter `y` or `yes` to continue. Any other response declines the action. The command
always attempts to restore the healthy Payment Service configuration during cleanup if
an earlier stage fails.

## What the scenario exercises

The runner persists a demo service registry and deployment history, produces real
ShopStack traffic, and uses the existing Prometheus/Loki detection sources and
deterministic Detection Runner. The Detection Engine publishes its correlated incident
to Redis Streams; the Worker consumes it using a transactional PostgreSQL inbox and
creates the incident, audit record, and initial timeline entry. The runner waits for
that normal projection before continuing. It then gathers current timeline evidence and
runs the bounded investigation engine through
its read-only tool registry, evaluates a rollback proposal through the remediation
policy, records a human approval decision, transitions the incident through mitigation
and resolution, and writes a report containing the timeline, cited evidence, actions,
recovery time, and audit records.

The default mode is self-contained. The model provider is a deterministic structured
output simulator; Slack delivery and interactive approval are recorded by a local Slack
simulator; the Kubernetes rollback adapter is a local simulator that restores the
Compose Payment Service configuration and verifies its readiness endpoint. The demo
does **not** call an external LLM, Slack workspace, Kubernetes API, or cluster. These
simulators are labeled in console output and in the generated report. No real credentials
or external accounts are needed.

## Inspect the result

The report is written to:

```text
.opsmind/demo/latest-report.md
```

Prometheus, Grafana, Jaeger, and the OpsMind API are also available from the usual local
Compose ports documented in the README. The final report includes the incident UUID so
you can look it up through the API/database if desired. The run does not automatically
tear down the Compose stack; use `docker compose down` when finished.

After resolution, the incident is under **Historical incidents** in the console, or
choose **All** on the dashboard because its default filter shows active incidents only.
The console displays incident records and evidence; Prometheus/Grafana display metrics,
and Jaeger displays request traces. They are separate views of the same demo run.

## Limits

This is an end-to-end local demonstration of OpsMind's interfaces and control flow. The
simulators make it reproducible without credentials or a Kubernetes cluster, but do not
validate external Slack delivery, a production LLM's analysis quality, or Kubernetes
RBAC and API behavior. Use the dedicated Kubernetes adapter integration procedure in
the remediation documentation to validate a real local cluster.
