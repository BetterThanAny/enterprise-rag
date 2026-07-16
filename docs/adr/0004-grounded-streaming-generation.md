# ADR 0004: Grounded streaming generation and provider boundary

- Status: accepted
- Date: 2026-07-15

## Context

M4 must stream answers through interchangeable OpenAI-compatible providers while ensuring that a
model can never turn an unauthorized or invented chunk identifier into a server-approved citation.
Provider credentials must remain outside PostgreSQL and source control, and client disconnects must
close upstream streams rather than leave generation work running.

## Decision

- Keep provider HTTP payloads and upstream SSE parsing inside `providers.py`. The business service
  depends only on a provider definition and an asynchronous text-delta stream. The allowlisted
  registry supplies OpenAI and DeepSeek remote adapters, an Ollama local adapter, and an explicitly
  marked deterministic test/development stub.
- Configure base URLs, models, and keys through environment-backed settings. API keys are never
  persisted. Every generation trace records the selected provider, model, and non-secret config
  version so a past answer remains explainable after deployment settings change.
- Retrieve with tenant, knowledge-base, current-version, and ACL predicates before constructing the
  prompt. Treat retrieved text as untrusted data. Hybrid/lexical generation requires a lexical hit;
  dense-only generation requires the configured similarity threshold. Otherwise return an explicit
  abstention without invoking the provider.
- Require inline `[[chunk:<UUID>]]` markers. A streaming parser withholds marker bytes, accepts only
  IDs in the authorized retrieval result, and emits a separate citation event containing filename,
  one-based PDF page, Markdown heading path, and an original excerpt. Unknown IDs are discarded and
  an answer with no valid citation is failed rather than recorded as grounded.
- Store tenant-owned generation traces containing the rendered prompt, answer, validated citations,
  terminal status, retrieval trace ID, and prompt/model/retriever/embedding/reranker/dataset versions.
- Wrap outbound and downstream streams in deterministic cleanup. Timeout, task cancellation, client
  disconnect, and generator close all set cancellation state, close provider resources, and persist
  an explicit failed or cancelled terminal status.

## Consequences

Switching among registered providers changes configuration or the request selector, not generation
business logic. Citation events are server attestations rather than model claims. The default smoke
path incurs no paid calls; real remote credentials and an Ollama installation remain deployment
choices. The synthetic M4 evaluation verifies the controlled contract but is not production answer
quality or faithfulness evidence.
