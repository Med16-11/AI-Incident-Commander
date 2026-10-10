# Remediation Engine

The Remediation Engine owns the action proposal, policy, approval, execution, and audit boundary.
The AI investigation engine may create a proposal with `remediations:propose`; it has no execution
capability. Proposals do not contain approval identity, and approval and execution reject non-human
principals even if a service account has those permissions.

## Action catalog

| Action | Risk | Validated parameters |
| --- | --- | --- |
| `restart_service` | `LOW_RISK` | `grace_period_seconds` (0–300, defaults to 30) |
| `clear_cache` | `LOW_RISK` | `cache_name`, optional `key_prefix` |
| `scale_service` | `MEDIUM_RISK` | `replicas` (1–100) |
| `rollback_deployment` | `MEDIUM_RISK` | `deployment_id`, `target_version` |
| `disable_feature_flag` | `HIGH_RISK` | `flag_key`, `environment` |

Database modifications are classified as high risk for policy reporting but are outside the
executable catalog. Targets are restricted resource identifiers, parameters are action-specific,
and the policy derives risk instead of accepting it from a caller. The approved payload is hashed;
execution revalidates it and rejects a changed proposal.

## HTTP lifecycle

- `POST /incidents/{incident_id}/remediations` creates an idempotent proposal and a pending
  approval request. Requires `Idempotency-Key` and `remediations:propose`. The request expires
  after `OPSMIND_REMEDIATION_APPROVAL_TTL_SECONDS` (15 minutes by default).
- `GET /incidents/{incident_id}/remediations` lists tenant-scoped actions.
- `GET /remediations/{action_id}` reads one action.
- `POST /remediations/{action_id}/approve` accepts `{"reason": "..."}` and records the approver,
  timestamp, approved action, target, policy risk, and reason. The requester cannot approve their
  own proposal. Approval is single-use and expires; late approval returns `410`.
- `POST /remediations/{action_id}/reject` accepts the same required reason and permanently rejects
  the pending request. Rejected or expired actions cannot execute.
- `POST /remediations/{action_id}/execute` requires `remediations:execute`, a human principal,
  explicit unexpired approval, and an idempotency key. `{"dry_run": true}` records a
  policy-validated plan and never calls an infrastructure adapter. Execution is followed by an
  adapter verification step. Failed verification records the failure and creates a separate
  compensation proposal that still requires human approval.
- `POST /remediations/{action_id}/rollback` creates a separate compensation proposal. It must be
  approved and executed through the same policy path. A rollback executor receives the original
  action and recorded result, and its outcome is verified as well.

## Execution adapters and failure behavior

Infrastructure operations are behind the `RemediationExecutor` interface. Its default implementation
is unavailable and fails closed, so deployment must inject configured Kubernetes, Git/GitLab,
feature-flag, or cache adapters before real actions can run. Adapters must return sanitized result
data and use narrow credentials. The service commits a `RUNNING` attempt and audit record before
calling an adapter, then commits the outcome and a second audit record. A process crash leaves an
in-progress attempt that is not replayed automatically; operators must reconcile the external state
before taking further action.

Every proposal, approval, dry run, execution start, outcome, and rollback proposal is recorded in
the append-only `audit_logs` table with actor, tenant, correlation ID, before/after state, and
outcome. The action row exposes `action_id`, `incident_id`, `action_type`, `target`, `parameters`,
`risk_level`, `requested_by`, `approved_by`, `status`, `execution_start`, `execution_end`, and
`result`; execution attempts are separately retained in `remediation_executions`.
Approval requests and their immutable action snapshots are stored in `remediation_approval_requests`
with a single terminal decision (`approved`, `rejected`, or `expired`), approver identity, decision
timestamp, and decision reason. Database row locking and optimistic versioning prevent concurrent
approval decisions from both succeeding.

The service uses injected authentication and authorization interfaces and denies access by default.
Configure those interfaces from the deployment identity provider; do not trust actor IDs supplied
in request bodies.

## Kubernetes adapter

`KubernetesRemediationAdapter` uses the official Kubernetes Python client and in-cluster
ServiceAccount credentials. It supports deployment restart (patches the pod template to trigger a
rolling restart), scale (patches the `deployments/scale` subresource), and rollback to a retained
ReplicaSet revision. Rollback `target_version` is the Kubernetes
`deployment.kubernetes.io/revision` value. `target` is a Deployment name in the one configured
namespace; namespace input cannot be supplied by an AI proposal. Restart follows the pod template's
`terminationGracePeriodSeconds`; the requested grace value is retained as an OpsMind annotation.

The adapter can inspect deployment rollout state, matching pod readiness/restarts, recent events,
and bounded timestamped pod log tails (maximum ten pods and 1,000 lines per pod). Logs can contain
sensitive application data and must remain behind the authenticated evidence API and normal
retention/redaction controls.

Every API request has a timeout. Transient 429/5xx/network errors use bounded exponential retries;
mutations are idempotent (restart/rollback use the remediation action UUID annotation and scale sets
an absolute replica count). `OPSMIND_KUBERNETES_DRY_RUN=true` asks the API server to validate
mutations without persisting them. The engine's `dry_run` request option remains a plan-only check
and does not call the adapter.

To enable the adapter, set `OPSMIND_KUBERNETES_ENABLED=true` and configure the namespace, request
timeout, retry count/backoff, rollout timeout, and optional API dry run via the
`OPSMIND_KUBERNETES_*` settings. Apply [`../infra/kubernetes/remediation-rbac.yaml`](../infra/kubernetes/remediation-rbac.yaml)
and run the remediation workload as `opsmind-system/opsmind-remediation`. The RoleBinding grants
permissions only inside `shopstack`; do not replace it with a ClusterRole or cluster-admin binding.

The exact permissions are:

| API group | Resource | Verbs | Use |
| --- | --- | --- | --- |
| `apps` | `deployments` | `get`, `patch` | Inspect status, trigger restart, restore a ReplicaSet template |
| `apps` | `deployments/scale` | `patch` | Set the desired replica count |
| `apps` | `replicasets` | `list` | Find the requested rollout revision |
| core (`""`) | `pods` | `list` | Inspect pods selected by a deployment |
| core (`""`) | `pods/log` | `get` | Retrieve bounded pod log tails |
| core (`""`) | `events` | `list` | Retrieve events for a deployment and its pods |

There is no cluster-scoped role, no permission to create/delete workloads, and no access to Secrets,
Nodes, or arbitrary API resources. Keep the adapter namespace aligned with the namespace in the
RoleBinding; a namespace-wide Role is the upper bound of its authority.

Unit tests use mocked official API clients. The local-cluster integration test is opt-in:
`OPSMIND_KUBERNETES_INTEGRATION=1 OPSMIND_KUBERNETES_TEST_NAMESPACE=shopstack OPSMIND_KUBERNETES_TEST_DEPLOYMENT=shopstack-api-gateway pytest tests/integration/test_kubernetes_adapter_cluster.py`.
It uses the current local kubeconfig and Kubernetes server-side dry run, so the named Deployment is
not modified.
