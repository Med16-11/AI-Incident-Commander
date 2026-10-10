# Service Topology Engine

OpsMind models each service dependency as a directed edge from the caller to the
service it invokes. For example, `Order -> Payment` means Order depends on
Payment. Edges are tenant scoped and can only connect services in the same
environment. The graph must be acyclic; dependency writes that would create a
cycle return `409 dependency_cycle`.

## API

All routes require the configured authentication and authorization interfaces.
The default API authentication backend denies requests until an identity backend
is injected.

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/services` | Register a service in the caller's tenant |
| `GET` | `/services` | List services with environment/name filters and pagination |
| `GET` | `/services/{service_id}` | Read one tenant-scoped service |
| `POST` | `/services/{service_id}/dependencies` | Add a dependency (`target_service_id`, optional `dependency_type`) |
| `DELETE` | `/services/{service_id}/dependencies/{dependency_id}` | Remove a dependency |
| `GET` | `/services/{service_id}/dependencies` | Read direct dependencies, transitive upstream/downstream services, depths, and critical path |
| `GET` | `/services/{service_id}/blast-radius` | Calculate likely affected callers and dependency context |

Service registration requires `services:write`, reads require `services:read`,
and dependency changes require `services:dependencies:write`. Mutations are
written to the append-only audit log in the same database transaction.

## Graph query semantics

- **Downstream services** follow dependency edges: services this service relies on.
- **Upstream services** follow reverse edges: callers that rely on this service.
- **Dependency depth** is the longest path from the service in the requested direction.
- **Critical path** is the longest downstream dependency chain, including the starting service.
- **Likely blast radius** is the affected service plus all transitive upstream callers.
  Downstream dependencies are returned separately as impact context.

Queries build adjacency maps from tenant-scoped service and dependency rows, then
use breadth-first traversal and DAG dynamic programming in `O(V + E)` time, plus
sorting of returned nodes. Edge creation locks tenant service rows while checking
cycles so concurrent writes cannot introduce a cycle between validation and
commit.

## Incident enrichment

Creating an incident with a service, or assigning a service to an incident,
stores a source-backed `service_topology` evidence snapshot. Adding or removing a
dependency refreshes snapshots for open incidents in the affected graph region.
Snapshots include the affected service, upstream and downstream services,
dependency depths, critical path, and likely blast radius. Historical snapshots
remain available through `GET /incidents/{incident_id}/evidence` so topology
changes do not rewrite what the response team previously saw.
