# ADR 0006: Separate development and semantic vector columns

- Status: accepted
- Date: 2026-07-17

## Context

M1--M5 used a deterministic 16-dimensional hash embedding to exercise indexing, retry, tenant
filtering, and retrieval without network/model dependencies. Replacing that column in place with a
384-dimensional semantic model would either rewrite existing rows during migration or make old
vectors appear to have semantic meaning they never had.

CI must remain deterministic and credential-free, while the portfolio also needs a real semantic
path that reaches PostgreSQL/pgvector rather than only an offline benchmark.

## Decision

- Keep the original database column as `development_embedding vector(16)` in the ORM mapping.
- Add `semantic_embedding vector(384)` with its own HNSW index through a non-destructive migration.
- Enforce that every chunk has exactly one vector kind with a database check constraint.
- Make each embedding provider expose its dimensions, version, semantic classification, and
  separate document/query encoding methods.
- Select and filter the matching vector column inside the dense SQL query before ranking.
- Use FastEmbed `BAAI/bge-small-en-v1.5` as the real local semantic provider; retain the hash
  provider as an explicitly named development/test dependency.
- Require an explicit document rebuild when changing provider kind. Do not synthesize or pad old
  vectors during migration.

## Consequences

- Migration preserves old rows and their retrieval behavior without an external model download.
- Semantic and development vectors cannot be accidentally compared in one query.
- Switching providers requires storage for a new document version/rebuild rather than an invisible
  background rewrite.
- Supporting another semantic dimension later requires a deliberate schema/migration decision; a
  generic unbounded provider registry remains out of scope.
