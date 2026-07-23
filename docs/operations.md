# Operations runbook

## Start and migrate

1. Install the pinned Python with `mise install` and run `mise exec -- uv sync --frozen`.
2. Copy variable names from `.env.example`; keep secrets in 1Password and run commands through
   `op run --env-file=.env -- <cmd>`.
3. Start with `docker compose up -d --build --wait`.
4. Confirm the finite `migrate` service exited successfully; API and worker start only after it has
   upgraded the database once.
5. Confirm `docker compose ps`, `curl --fail http://127.0.0.1:18000/health/ready`, and
   `mise exec -- uv run alembic current` report healthy dependencies and `20260717_0006 (head)`.
6. Run `mise exec -- uv run python scripts/smoke_test.py` with a host-addressed `DATABASE_URL`.

`scripts/demo.py` performs this flow with process-local ephemeral secrets and isolated Compose
volumes. It leaves the stack intact for inspection and never writes those secrets to disk.

## Observe

- Scrape `GET /metrics`. Alert on the rate of HTTP 5xx and
  `enterprise_rag_generation_runs_total{status="failed"}` independently because SSE provider
  failures occur after an HTTP 200 response.
- Watch retrieval p95 from `enterprise_rag_retrieval_duration_seconds`; use the load report for the
  client-observed release gate.
- Watch TTFT and provider duration separately. Token metrics include a `source` label of `provider`
  or `estimated`; cost is only meaningful after versioned rate variables are configured.
- Set `OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP/HTTP `/v1/traces` receiver when external traces are
  required. No secret headers are stored by this project.
- Given a generation trace UUID from the SSE `meta` event, call
  `GET /api/v1/traces/{generation_trace_id}` with the original tenant credentials. The response
  reconstructs retrieval candidates and scores, optional rerank span/version, provider span,
  terminal state, usage, cost, citations, and configuration versions.

## Recover

- Worker restart: `enterprise_rag_worker.enqueue_pending` re-enqueues pending jobs and running jobs
  whose lease is missing or expired. Schema migration is owned by the finite Compose `migrate`
  service; chunk replacement and job success remain one transaction. Docker Compose does not act as
  a production process supervisor, so an operator or external supervisor must restart a killed
  worker before this recovery path runs.
- Redis outage: uploads remain authoritative in PostgreSQL even if queue publication fails. Restore
  Redis, then restart the worker or run `python -m enterprise_rag_worker.enqueue_pending` in the
  worker environment. Verify the job reaches `succeeded` and has one chunk set.
- Provider 429/5xx: retries are bounded by `PROVIDER_MAX_ATTEMPTS`; tune base/max backoff without
  changing business logic. Retries stop as soon as output begins.
- MinIO cleanup: run `mise exec -- uv run python scripts/cleanup_orphans.py --dry-run` before an
  actual cleanup. Database records remain authoritative.

The destructive removal of persistent Compose volumes is not part of normal recovery. Obtain
explicit approval before deleting production data or volumes.
