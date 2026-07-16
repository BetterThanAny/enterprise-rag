# Troubleshooting guide

## Readiness is 503

Inspect the `checks` object from `/health/ready` before restarting anything. A single unavailable
dependency is reported as `database`, `redis`, or `minio`. Check the corresponding container health
and logs. Do not infer API failure from liveness alone.

## Upload stays pending

Query the job endpoint and inspect `status`, `stage`, `attempts`, `error_code`, and `available_at`.
If Redis was unavailable, restore it and restart the worker so the startup scanner republishes the
pending database row. If a running lease has not expired, a second worker correctly does nothing.
Run `scripts/recovery_test.py` in a disposable Compose environment to reproduce the complete
stop-Redis/upload/start-Redis/restart-worker path.

## Generation ends with an SSE error

- `provider_credentials_missing`: inject the selected remote key at runtime; there is no fallback.
- `generation_timeout`: inspect provider duration and upstream reachability, then change the timeout
  only with latency evidence.
- `provider_error`: check provider retry metrics and the trace. Repeated 429/5xx exhaust the bounded
  retry policy; malformed or partially emitted streams are not replayed.
- `citation_validation_failed`: the provider produced no authorized chunk marker. Inspect the
  retrieval candidates and prompt/model versions through the trace; do not bypass validation.

## Trace lookup is 404

Trace lookup is tenant-filtered. Confirm the bearer token and `X-Tenant-ID` belong to the generation
tenant and use the generation trace UUID, not the root 32-hex OpenTelemetry trace ID. Cross-tenant
lookups intentionally return 404.

## Retrieval p95 exceeds 500 ms

Confirm the report used 50,000 chunks, 20 concurrency, hybrid mode, rerank enabled, and excluded LLM
time. Compare client and server p95. A large client-only delta points to API/container scheduling;
a large server value points to SQL, pgvector/FTS, rerank, or trace-write cost. Run `ANALYZE chunks`,
verify GIN/HNSW indexes and tenant predicates, then rerun the fixed load generator. Do not weaken the
gate or call a smaller corpus equivalent.

## Metrics show zero cost

Zero is expected for the deterministic/Ollama paths and whenever deployment cost rates are zero.
Check token `source`; estimated counts are deliberately distinct from provider-reported usage.
