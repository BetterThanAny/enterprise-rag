# Reproducible demos

## Credential-free deterministic end-to-end demo

Prerequisites are Git, Docker, and mise. The script generates process-local secrets, starts a new
Compose project, migrates an empty database, and exercises auth, upload, async indexing,
retrieval/rerank, SSE generation with a server-validated citation, trace reconstruction, metrics,
evaluation target, and deletion.

```bash
mise install
mise exec -- uv sync --frozen
mise exec -- uv run python scripts/demo.py
```

The deterministic embedding/generation components used here are explicitly test providers. They
validate backend behavior, not semantic quality.

## Real semantic PostgreSQL demo

```bash
mise exec -- uv run python scripts/demo.py --semantic
```

This downloads `BAAI/bge-small-en-v1.5` into the API and worker container caches, writes only the
384-dimensional semantic vector column, and requires the retrieval response to report
`fastembed:BAAI/bge-small-en-v1.5`. Cold start is slower than the deterministic demo.

Each invocation should use the default random project name, or an unused explicit `--project` name.
The script deliberately leaves containers and volumes available for inspection.

## Human-labeled public retrieval evaluation

```bash
mise exec -- uv run python scripts/evaluate_public_retrieval.py
```

The script downloads the pinned-checksum SciFact archive and compares the real semantic model with
the deterministic hash baseline. It writes a versioned JSON report under `data/eval/reports/`.

## Real local generation provider

The optional profile avoids a host Ollama installation:

```bash
docker compose --profile local-llm up -d ollama --wait
docker compose --profile local-llm run --rm ollama-init
mise exec -- uv run python scripts/provider_smoke.py
```

Compose still requires the normal secret variable names to be supplied. This smoke requires actual
streamed text and does not substitute an HTTP mock. The pinned Ollama image is large; the default
`qwen2.5:0.5b` model is roughly 400 MB.
