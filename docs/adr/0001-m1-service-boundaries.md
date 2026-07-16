# ADR 0001: M1 service and tenancy boundaries

- Status: accepted
- Date: 2026-07-13

## Context

M1 needs a demonstrable multi-tenant API foundation without implementing document ingestion or
retrieval ahead of their milestones. Tenant isolation must be enforced in persistence and query
construction, not added after loading records.

## Decision

- Use FastAPI only as the transport layer. Authentication and knowledge-base use cases live in
  core services; SQL construction lives in repositories.
- Use PostgreSQL UUID primary keys. Every tenant-owned table carries `tenant_id`. Composite foreign
  keys ensure a document, ACL grant, or index job cannot reference a parent from another tenant.
- Treat `users` as global identities so one person can belong to more than one tenant. Tenant-owned
  authorization state lives in `memberships`, which always carries `tenant_id`.
- Put only the global user identifier in JWTs. Every tenant-scoped request supplies `X-Tenant-ID`,
  and the API verifies a current membership before executing a tenant-filtered repository query.
- Return 403 when the caller is not a tenant member and 404 when an authorized tenant member asks
  for a resource belonging to another tenant. This avoids exposing cross-tenant resource existence.
- Model document, ACL, and index-job persistence boundaries in M1, but defer upload, processing,
  retry, and retrieval behavior to later milestones.

## Consequences

The same knowledge-base name is legal in different tenants and rejected within one tenant. Database
constraints provide a second isolation layer behind the service queries. Later milestones can add
versioning and indexing behavior without moving transport logic into route handlers.
