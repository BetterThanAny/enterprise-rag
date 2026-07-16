# ADR 0005: Reconstructable traces, bounded recovery, and evaluation boundary

- Status: accepted
- Date: 2026-07-16

## Context

M5 requires operational metrics, a single retrieval/rerank/provider trace, reproducible load
evidence, and integration with the independent `llm-eval-platform` without turning this service
into another evaluation product. Provider usage can be exact or absent depending on the upstream
OpenAI-compatible implementation, while retrying a partially streamed response can duplicate text.

## Decision

- Instrument request, retrieval, rerank, question-answer, and provider boundaries with
  OpenTelemetry spans. Explicit parent spans are used instead of holding a ContextVar across SSE
  yields. PostgreSQL stores the 32-hex trace ID and relevant 16-hex span IDs beside versioned
  retrieval/generation data, so trace reconstruction does not depend on an external collector.
  An optional OTLP/HTTP endpoint exports the same spans.
- Expose Prometheus counters/histograms without tenant, user, query, document, request, or trace
  labels. Request/status, retrieval mode/rerank, provider/model, terminal status, token source, and
  bounded retry reason are the only labels, preventing sensitive data and unbounded cardinality.
- Request provider usage with OpenAI-compatible `stream_options.include_usage`. Persist exact
  provider usage when returned; otherwise use a documented UTF-8 byte approximation and mark
  `usage_source=estimated`. Cost uses deployment-supplied per-million-token rates; zero means free
  or unavailable and never silently imports volatile vendor pricing.
- Retry 429 and 5xx only before any output is accepted, with a bounded attempt count, bounded
  exponential backoff, and `Retry-After` support. Once output starts, the stream is never replayed.
- Preserve PostgreSQL as the indexing-job source of truth. Queue publication failure leaves a
  pending row; worker startup scans pending and expired leases and re-enqueues idempotently. A real
  Redis stop/start plus worker restart smoke proves this path.
- Provide an authenticated HTTP/JSON evaluation target endpoint matching the independent
  platform's generic `HttpTargetAdapter`: request data lives under `input`; deterministic output,
  usage, and trace metadata are returned separately. No source dependency or shared database is
  introduced between the two projects.
- Generate the 50,000-chunk load corpus deterministically, keep original per-document payloads in
  MinIO, insert real pgvector rows, issue hybrid+rerank API calls at 20 concurrent requests, and
  fail the command when client-observed p95 exceeds 500 ms. The API pool has at least 20 base
  connections for that concurrency, and dense SQL orders only by cosine distance so PostgreSQL can
  use HNSW; a secondary UUID order would force a full scan/sort and is intentionally excluded.

## Consequences

Every completed or failed traced question can be reconstructed through a tenant-scoped endpoint,
and external telemetry remains optional. Token counts explicitly distinguish exact from estimated
values. Provider retries cannot duplicate already streamed output. The load corpus is operational
evidence, not a semantic-quality dataset. The evaluation endpoint is intentionally narrow and does
not add dataset, dashboard, or gate ownership to Enterprise RAG.

Compose runs Alembic in one finite `migrate` service. API and worker wait for that service to
succeed instead of racing to create or upgrade the schema when a database is empty.
