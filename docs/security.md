# OpsMind security review

## Scope and result

This review covered the repository's API and service boundaries, incident investigation, remediation, Kubernetes adapter and manifests, Slack integration, browser client, configuration, event/logging paths, and database access. High and medium severity findings discovered in those paths were fixed and regression tests were added. The automated tests do not replace a deployment-specific threat assessment or penetration test.

The core safety invariant is enforced server-side: an AI investigation has only a fixed set of read-only evidence tools. It cannot call shell commands or Kubernetes APIs. AI output can propose actions, but remediation execution requires an authenticated human approval and a policy-approved action. The Kubernetes adapter accepts only typed remediation operations and uses a namespace-scoped service account with no shell, Secret, or cluster-wide permissions.

## Findings fixed

| Area | Finding | Change |
|---|---|---|
| LLM data handling | Incident fields and evidence persisted from external systems could include credential-like values before they reached the provider. | Redact incident title, description, service, and environment; redact evidence, warnings, and historical context again at the final provider boundary. |
| Outbound URLs / SSRF exposure | Configured LLM and Slack decision endpoints accepted HTTP, embedded credentials, query strings, or fragments. | Validate URL scheme and structure; require HTTPS except when `environment=local`; reject embedded credentials, query, and fragment. Network egress still requires deployment controls. |
| Slack payload rendering | Untrusted event fields were inserted into Slack `mrkdwn`, allowing mention/link/format control injection. | Escape Slack control characters and render event sections as `plain_text`; approval buttons remain fixed server-generated controls. |
| Browser token storage | Browser requests could include ambient cookies without needing cookie auth. | Keep bearer tokens in memory only and remove ambient credential inclusion. Reload clears the token. |
| Browser hardening | Static UI responses lacked baseline content and framing protections. | Add CSP, `nosniff`, frame denial, referrer policy, and a restrictive permissions policy at Nginx. |
| ShopStack pod isolation | ShopStack pods relied on image defaults and mounted service account credentials unnecessarily. | Require non-root UID, RuntimeDefault seccomp, no privilege escalation, read-only root filesystem, dropped Linux capabilities, and disabled service account token automount. |
| Secret configuration | Internal correlation and investigation bearer configuration was stored as plain string settings. | Use Pydantic `SecretStr`; endpoint comparisons explicitly unwrap only for constant-time comparison. |
| Error logging | An unexpected correlation failure logged an exception traceback that could include provider or database details. | Log only a sanitized error type and status; no exception text or traceback is emitted there. |

## Verified controls

### Authentication and authorization

- API authentication and authorization are represented as server-side interfaces with fail-closed defaults. Missing internal correlation/investigation credentials disable those internal endpoints rather than making them public.
- Local Compose and the Kubernetes `dev` overlay can opt into a single-tenant demo bearer credential using `OPSMIND_LOCAL_DEMO_AUTH_ENABLED` plus a high-entropy token. This is not an identity provider; staging and production leave it disabled and retain the deny-all default.
- Remediation routes validate the authenticated actor and permissions server-side. AI-originated proposals do not satisfy the human approval requirement; the AI cannot approve or execute its own action.
- Approval is bound to an action, target, risk, approver, expiry, and audit record. Duplicate, expired, or unauthorized decisions are rejected by the remediation service.
- Slack identity is mapped to an existing OpsMind credential only after a valid Slack signature and timestamp check. Slack's supplied user identity is never treated as authorization by itself.

### AI and prompt injection

- The model receives named function tools from a fixed allowlist; no dynamic function lookup, arbitrary SQL, shell, Python, or Kubernetes tool exists.
- Tool arguments are validated against strict Pydantic schemas, and repeated queries, call count, wall time, per-tool time, and context size are bounded.
- Tool output and incident text are untrusted data. The system prompt separates evidence from instructions; final claims must cite IDs actually returned by tools. Historical evidence remains a distinct context channel and cannot establish current symptoms.
- Evidence summaries are redacted again immediately before provider serialization. This is pattern-based credential filtering, not a comprehensive DLP system.
- Provider credentials are sent only to the explicitly configured provider endpoint using HTTPS outside local development. Provider data retention and model-training terms depend on the configured provider and account.

### Remediation and Kubernetes

- Remediation types and parameters are allowlisted and strictly validated before policy evaluation. Dry-run mode is supported. The AI has no executor dependency.
- Kubernetes operations use the official Kubernetes API client and fixed methods; core functionality does not shell out to `kubectl`.
- `infra/kubernetes/opsmind/target-rbac.yaml` defines a namespaced `Role` in the configured monitored-workload namespace and binds it to the remediation ServiceAccount in the OpsMind namespace. It grants only `get`/`patch` on deployments, `patch` on deployment scale, `list` on ReplicaSets and pods, `get` on pod logs, and `list` on events. It grants no Secret access, `pods/exec`, delete, create, cluster role, or cluster-admin access. Use a dedicated target namespace because these verbs cover all matching resources within that namespace.
- ShopStack application pods do not need Kubernetes credentials, so their service account token automount is disabled.

### Injection, database, audit, and logs

- Database reads and writes use SQLAlchemy expressions or bound parameters; externally supplied values are not interpolated into SQL strings.
- ChaosLab commands map static experiment names to fixed argument vectors. AI-facing investigation tools cannot invoke that CLI or arbitrary commands.
- Incident mutations and remediation decisions write audit records through application paths that expose no update/delete interface. The database owner can still alter stored rows, so these records are not tamper-proof; protect audit storage with database access control, encryption at rest, backup policy, and retention appropriate to the operator's environment.
- Structured logs avoid authorization headers and provider credentials. The exception path identified in this review no longer logs exception text. User-supplied incident text can still contain sensitive business data in persisted records and should be access-controlled and retention-limited.

## Deployment requirements and remaining limits

1. Configure a real authentication provider and a least-privilege authorization policy before exposing OpsMind outside a trusted local development environment. The repository's replaceable auth interfaces are not a substitute for an identity provider.
2. Use HTTPS for all externally reachable endpoints and set `OPSMIND_ENVIRONMENT` to `staging` or `production` for those releases. The explicit HTTP exception exists only in local and isolated dev environments.
3. Restrict network egress from services. URL validation prevents URL userinfo/query abuse and enforces transport, but operator-controlled hostnames can still resolve to internal addresses; use an egress proxy/firewall or destination allowlist to prevent SSRF-style access to cloud metadata and private control planes.
4. Store production credentials in a secret manager and inject them at runtime. Do not commit `.env`; `.env.example` contains placeholders only. Rotate credentials if they have been exposed.
5. Use a dedicated Kubernetes namespace and apply the supplied narrow RBAC. Do not bind the remediation service account to broader roles. Keep Kubernetes API network access limited to the remediation service.
6. Review data sharing with the configured LLM provider. Redaction covers common bearer, password, token, API-key, URL credential, and JWT shapes; it cannot detect every proprietary secret, PII value, or encoded credential.
7. Configure log/audit access, encryption, backup, and retention. Audit rows can include action rationale and other user-provided content; avoid putting secrets in incident descriptions or approval reasons.
8. Protect the Nginx-served UI and API with TLS at the ingress/load balancer. The UI holds its bearer token only in memory, but an active XSS vulnerability could still use that token while the page is open.
9. Keep dependencies and base images patched, scan built images and lockfiles, and review Kubernetes manifests for the target cluster's admission policies.

## Security regression tests

`tests/unit/test_security_posture.py` verifies secret-safe settings, namespace-scoped remediation RBAC, hardened ShopStack pod specs, and browser token/header protections. Provider and investigation tests verify outbound URL constraints and redaction at the LLM boundary. Slack tests verify escaped text rendering and safe API URLs. Existing remediation, investigation, and Slack suites cover unauthorized execution, expired approval, unknown tools, invalid tool arguments, fabricated evidence, and signature failures.
