# ADR 0002: Reliable document indexing and recovery

- Status: accepted
- Date: 2026-07-13

## Context

M2 must accept PDF, TXT, and Markdown documents asynchronously while preventing duplicate chunks
under repeated delivery or worker termination. PostgreSQL remains the source of truth, MinIO stores
immutable originals, and Redis is a delivery mechanism rather than authoritative job state.

## Decision

- Use Dramatiq with its Redis broker as the only task queue implementation. API requests persist an
  `index_jobs` row before dispatch; a startup scanner re-enqueues pending work and recovers expired
  leases, so a temporary broker outage cannot erase accepted work.
- Claim jobs with a PostgreSQL row lock and a time-bounded lease. Each attempt refreshes the lease at
  stage boundaries. Redelivery during a live lease is harmless, and a worker killed during parse,
  embedding, or database write can be retried after lease expiry.
- Store document versions and object keys immutably. A successful indexing transaction atomically
  replaces the version's chunk set, marks exactly one version current, and updates the document
  projection. Database uniqueness constraints protect job idempotency and current-version state.
- Parse PDF with PyMuPDF and decode TXT/Markdown strictly as UTF-8. Deterministic parse errors fail
  immediately; transient provider or infrastructure errors use bounded exponential backoff.
- Keep embedding providers behind the `EmbeddingProvider` protocol. M2 uses an explicitly named,
  deterministic 16-dimensional test/development stub so the default suite incurs no paid provider
  calls; it is not represented as a semantic production model.
- Delete authoritative PostgreSQL state before removing its MinIO objects. A crash after the commit
  can leave only discoverable orphan objects, which the cleanup command can remove; it cannot leave
  live database rows pointing to an object already removed by that request.
- Scope object keys, API lookups, document-version relations, chunks, and jobs by tenant. Cross-tenant
  IDs are filtered in database queries and return 404 without revealing resource existence.

## Consequences

Redis queues may contain duplicate deliveries, but they cannot create duplicate authoritative data.
Old successful versions remain available for audit while only one version is current. Indexing can
recover from process termination without a distributed transaction across PostgreSQL, Redis, and
MinIO. Orphan cleanup is an explicit operational responsibility, and a production embedding model
must later implement the existing provider interface without changing indexing state semantics.
