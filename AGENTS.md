# AGENTS.md

## Scope

These instructions apply to the entire `enterprise-rag` project.

The project is an enterprise-style, multi-tenant RAG knowledge base. Optimize for demonstrable backend correctness, data isolation, retrieval quality, failure recovery, and observability. Do not expand it into a generic no-code AI platform.

## Source of truth

- `PLAN.md` defines the approved scope, milestones, acceptance criteria, test matrix, and risks.
- Update `PLAN.md` when a milestone, acceptance command, architectural decision, or installed tool changes.
- Do not claim a milestone complete until its exit conditions have been exercised with real commands.
- Record major design choices under `docs/adr/` once that directory exists.

## Required workflow

1. Inspect the repository and `git status --short` before changing files.
2. Choose the smallest unfinished `PLAN.md` milestone that can be completed coherently.
3. Add or update regression tests before changing retrieval, ACL, indexing, or job-state behavior.
4. Implement the minimal change.
5. Run the narrow test first, then lint, type checking, relevant integration tests, and a smoke path.
6. Report verified results separately from unverified behavior and non-findings.

For a clearly multi-step milestone, keep `PLAN.md` current. For a one-line or single-file correction, edit directly without adding process files.

## Architecture invariants

- Every tenant-owned database record must have an explicit tenant relationship.
- Tenant and ACL filtering must be part of the retrieval query. Never retrieve across tenants and discard unauthorized chunks only after ranking.
- PostgreSQL is the source of truth for users, tenants, documents, versions, chunks, ACLs, and jobs.
- Original documents live in S3-compatible object storage; store immutable object identifiers rather than local absolute paths.
- Indexing jobs must be idempotent and have explicit terminal states.
- A retry must not create duplicate chunks, embeddings, or object records.
- Provider-specific SDK types must stay behind an OpenAI-compatible provider interface.
- A generated citation is valid only if the server can map it to a retrieved, authorized chunk.
- Lack of evidence must lead to an explicit abstention path.
- Retrieval and generation configurations must be versioned in traces and evaluation output.

## Project layout

Prefer this shape unless an accepted ADR says otherwise:

```text
apps/api/
apps/worker/
packages/core/
tests/unit/
tests/integration/
tests/security/
data/eval/
scripts/
docs/adr/
```

Keep domain and retrieval logic out of FastAPI route handlers. Route handlers validate transport concerns; services own use cases; repositories own persistence.

## Environment and dependencies

- Use `mise` to pin Python and project tool versions.
- Use `uv add` for Python project dependencies and `uv run` for project commands.
- Stateful services such as PostgreSQL, Redis, and MinIO must run in Docker/OrbStack, not as Homebrew services.
- Do not install global packages without asking the user.
- Do not use `direnv`; use `.mise.toml` and `.env` loading.
- Never commit secrets. Use environment variables or `op://...` references and document `op run --env-file=.env -- <cmd>` where needed.
- Use `curl` in scripts; interactive HTTP debugging may use `xh`.

## Data and migration rules

- All schema changes require an Alembic migration and migration tests.
- Migrations must support a new empty database and the previous committed schema.
- Destructive migrations or irreversible data rewrites require explicit approval.
- Enforce idempotency with database constraints where possible, not only application checks.
- Deletion must define behavior for objects, chunks, vectors, ACLs, conversations, and audit records.
- Evaluation datasets are immutable once used by a recorded run; create a new version instead of editing in place.

## Retrieval and evaluation rules

- Maintain a deterministic retrieval evaluation path independent of LLM generation.
- Always compare changes against a fixed baseline dataset.
- Report Recall@K, MRR/NDCG, latency, and dataset/version metadata together.
- Do not assert that Hybrid Search or Rerank improved quality unless the measured delta meets the acceptance threshold.
- Preserve non-findings and regressions in benchmark reports.
- LLM-as-judge metrics require a human-labeled calibration subset and cannot be the only release gate.

## Testing and verification

When available, the expected full verification sequence is:

```bash
mise exec -- uv run ruff check .
mise exec -- uv run pyright
mise exec -- uv run pytest -q
mise exec -- uv run pytest -q tests/integration
mise exec -- uv run pytest -q tests/security
mise exec -- uv run python scripts/smoke_test.py
```

Additional requirements:

- Stub paid LLM and embedding providers in the default test suite.
- Integration tests must exercise real PostgreSQL/pgvector, Redis, and MinIO containers.
- Security tests must cover cross-tenant API access and cross-tenant retrieval.
- Fault tests must terminate workers during parse, embedding, and database-write stages.
- UI work, when present, requires desktop and mobile smoke testing of upload, progress, question, citation, and failure states.

## Privacy and Git hygiene

- Do not hardcode user home paths in source or reusable configuration.
- Do not use personal email addresses in fixtures, examples, documentation, commits, or generated artifacts. Use `user@example.com`.
- Never add AI co-author or AI attribution trailers.
- Run `gitleaks detect` before commits or exports when secrets could plausibly have entered fixtures, logs, or `.env` files.
- Ask before force-pushes, destructive deletion, CI permission changes, publishing, or public comments.
