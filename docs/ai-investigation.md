# AI investigation engine

The incident engine exposes an authenticated internal investigation endpoint:

```http
POST /incidents/{incident_id}/investigate
Authorization: Bearer $OPSMIND_INVESTIGATION_API_TOKEN
```

The handler loads a minimal incident context and hands it to a bounded investigation
orchestrator. The LLM provider receives no SQLAlchemy session, database client, or general
purpose execution interface. It can request only these read tools:

| Tool | Data returned |
| --- | --- |
| `search_incident_timeline` | Persisted incident timeline and evidence |
| `search_logs` | Tenant-scoped stored logs and bounded Loki search |
| `query_metrics` | Predefined Prometheus service metrics |
| `get_service_dependencies` | Registered direct and transitive service dependencies |
| `get_recent_deployments` | Recent persisted deployments |
| `get_service_health` | Current Prometheus target health |
| `get_previous_incidents` | Similar resolved incidents as historical context, in the same tenant |
| `get_runbooks` | Runbooks explicitly registered in service metadata |

Tool names and arguments are validated against a fixed registry. Metric selection is an enum;
runbooks are read from configured metadata rather than arbitrary URLs. Tool outputs are normalized
Current tool outputs are normalized and persisted as incident evidence before their IDs are shown
to the provider. `get_previous_incidents` returns a separate historical-context collection and
never writes its records to current incident evidence. A tool warning (for example, an incomplete
timeline or unavailable source) is carried into `missing_information`.

## Historical incident retrieval

When an incident enters `RESOLVED`, the API transaction creates or updates a historical snapshot
with the analysis summary, symptoms, root-cause statement, affected services, evidence references,
remediation recommendations and outcomes, resolution duration, and searchable tags. Reopening a
resolved incident removes that snapshot; resolving it again refreshes the snapshot. The data is
tenant scoped and PostgreSQL uses a GIN index over an English `tsvector` for full-text retrieval.
SQLite uses a small lexical fallback for local tests. Vector retrieval is represented by the
`HistoricalIncidentRetriever` and `HistoricalVectorSearchBackend` interfaces; the vector adapter
hydrates result IDs through tenant-scoped snapshot rows.

The investigation context labels its sources as `CURRENT_EVIDENCE` and
`HISTORICAL_EVIDENCE_CONTEXT_ONLY`. Current factual claims must cite current evidence IDs.
Historical analogies use `historical_context_only` and `historical_incident_ids`, while
`supporting_evidence` remains limited to current evidence. A historical incident can inform what
to investigate or what previously worked, but it cannot establish that a symptom, service impact,
or root cause is happening now. Historical-only claims are rejected in current summary, severity,
symptoms, affected services, and root cause fields; analogies belong in alternative hypotheses.
If current evidence does not establish a root cause, the analysis must say so and keep confidence
low.

The response uses the strict `IncidentAnalysis` schema. Factual claims, including the severity
rationale, must cite evidence IDs returned during that investigation. Fabricated or otherwise
unknown IDs reject the response. Unsupported root causes must explicitly say “Insufficient evidence”
and use confidence at or below 0.4. The model-authored progress fields are explicitly tentative;
only the final evidence-cited schema is treated as an analysis. Server-recorded investigation steps
replace model-authored steps. The engine only recommends actions; it has no remediation tool and
cannot execute actions. Retrieved logs and runbooks are treated as untrusted input and cannot extend
the tool allowlist.

## Agent state and execution bounds

Each request creates an `investigation_runs` row before contacting a provider. State transitions and
execution events are appended to `investigation_run_steps`; the current state and bounded agent
memory are updated atomically with each trace event. The phases are:

```text
INITIAL_TRIAGE → HYPOTHESIS_FORMED → EVIDENCE_COLLECTION → HYPOTHESIS_UPDATED
                                                               ↓
                                        ROOT_CAUSE_IDENTIFIED → REMEDIATION_PROPOSED
```

`REMEDIATION_PROPOSED` records that recommendations are present; it never executes them. A partial
run can finish at `HYPOTHESIS_UPDATED` with missing information. The response includes the run ID
in `X-Investigation-Run-ID` for database-side inspection. Apply the new schema locally with
`make migrate` after starting the Compose stack.

The engine defaults to 12 tool calls, a 120-second wall-clock budget, a 15-second per-tool deadline,
a 48,000-character serialized-context budget (messages plus tool/output schemas), and a 0.75 root-cause confidence threshold. It skips
repeated tool/argument pairs after schema normalization. Context management retains incident
context, recent tool exchanges, and a compact evidence ledger; older exchanges/evidence are dropped
from model context when needed, while citation validation still checks all evidence collected for
the run. Synchronous provider and read-tool calls run behind a process-wide bounded worker pool;
the caller stops waiting at the deadline, while underlying network/database client timeouts remain
responsible for releasing an already-running read operation.

Each state transition, provider call, and tool call is recorded as a persisted trace step and an
OpenTelemetry span. Trace attributes include state, step/tool name, outcome, elapsed time, context
size, evidence count/IDs, and a one-way query fingerprint; raw tool arguments and provider content
are not stored in the trace.

## Provider configuration

Set the following values in `.env` for the incident-engine container:

```dotenv
OPSMIND_INVESTIGATION_API_TOKEN=<high-entropy internal token>
OPSMIND_LLM_BASE_URL=https://provider.example/v1
OPSMIND_LLM_API_KEY=<provider secret>
OPSMIND_LLM_MODEL=<model name>
OPSMIND_LLM_TIMEOUT_SECONDS=30
OPSMIND_INVESTIGATION_MAX_TOOL_CALLS=12
OPSMIND_INVESTIGATION_MAX_EXECUTION_SECONDS=120
OPSMIND_INVESTIGATION_TOOL_TIMEOUT_SECONDS=15
OPSMIND_INVESTIGATION_MAX_CONTEXT_CHARS=48000
OPSMIND_INVESTIGATION_CONFIDENCE_THRESHOLD=0.75
```

The provider adapter uses an OpenAI-compatible Chat Completions API with structured JSON output.
The provider is injected through the `LLMProvider` protocol, so another adapter can be substituted
without changing the investigation workflow. The API key is a secret setting and is passed only to
the incident-engine service. No credentials are committed to the repository. Without a provider
configuration or internal token, the endpoint fails closed with `503`.

## Limits and failure behavior

- Tool calls and provider turns are bounded; exhausting either limit fails the investigation.
- Invalid arguments and unknown tool names are rejected and reported as coverage gaps.
- Provider/network errors and malformed structured output return a sanitized `502` from the
  internal endpoint; sensitive provider responses and credentials are not logged.
- Evidence is tenant-scoped through the incident and service registry. The model cannot choose a
  tenant ID, execute SQL, or invoke remediation.
- An unavailable source can yield a partial result only when the provider marks the missing
  information; this engine does not treat absence of evidence as proof that a symptom is absent.

Unit coverage is in `tests/unit/test_investigation.py` and
`tests/unit/test_investigation_store.py`, including state progression, deadlines, tool failure,
repeat prevention, context limits, unsupported evidence IDs, contradictory facts, incomplete
evidence, schema validation, persistence, and malicious log content.
