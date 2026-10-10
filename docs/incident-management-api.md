# Incident Management API

The FastAPI application is served by `apps/api`. Interactive OpenAPI is
available at `/docs`, the ReDoc view at `/redoc`, and the machine-readable
schema at `/openapi.json`.

## Identity integration

`create_app` accepts an `AuthenticationBackend` and an `AuthorizationPolicy`.
The default authentication backend denies every request, so deployments must
inject an implementation that validates their identity provider's credentials
and returns a `Principal` containing a tenant ID, subject ID, and permission
claims. No credentials or development identity are hard-coded. The default
authorization policy checks explicit permission names such as
`incidents:create`, `incidents:read`, `incidents:update`,
`incidents:events:write`, and `incidents:close`; replace it when resource-level
RBAC or ABAC rules are needed.

All incident queries are scoped to the authenticated principal's tenant. A
cross-tenant incident lookup returns 404 to avoid disclosing its existence.
Every response includes `X-Correlation-ID`; callers may supply a validated
value or let the API generate one. Errors use a stable `error` object containing
the code, message, correlation ID, and optional details.

## Writes

`POST /incidents` requires an `Idempotency-Key` header. A replay with the same
key and body returns the original incident; reuse with a different body returns
409. `PATCH` and `POST /incidents/{incident_id}/close` require the current
`expected_version` for optimistic concurrency. Incident state changes use the
domain transition rules. Event ingestion also requires `Idempotency-Key` and
deduplicates in tenant and source scope.

Successful incident creation, updates, event ingestion, and closure write an
`AuditLog` record in the same transaction as the incident data. These changes
also append an incident timeline entry. List and nested collection endpoints
support `limit` and `offset` pagination; limits are capped at 100.
