# ADR 0003: Tenant-filtered hybrid retrieval and fixed evaluation

- Status: accepted
- Date: 2026-07-13

## Context

M3 must compare lexical, dense, hybrid, and reranked retrieval without allowing an unauthorized
chunk into any candidate list. It must also preserve candidate scores, latency, and configuration
versions and evaluate quality independently of answer generation.

## Decision

- Store a generated `tsvector` for each chunk and index it with GIN. Parse user text with
  PostgreSQL's `websearch_to_tsquery` under the `simple` configuration. Terms are combined as a
  safe disjunction so descriptive query words do not turn an otherwise useful lexical match into
  an all-terms-required miss.
- Use pgvector cosine distance with an HNSW `vector_cosine_ops` index for dense retrieval. The
  current 16-dimensional deterministic embedding remains explicitly a development/evaluation
  stub, not a production semantic model.
- Put tenant, knowledge-base, ready/current-version, and ACL predicates in both lexical and dense
  SQL statements before ordering and limiting. Owners and admins can read every document in their
  tenant knowledge base. Members and viewers can read documents with no ACL rows or documents with
  an explicit read/write grant. RRF only combines results already authorized by those SQL queries.
- Fuse chunk rankings with deterministic reciprocal rank fusion. Equal scores preserve first-seen
  order and then chunk ID, making repeated evaluations reproducible.
- Keep learned reranking behind the `CrossEncoderReranker` protocol. FlashRank's
  `ms-marco-TinyBERT-L-2-v2` scores query/passage pairs for the recorded M3 evaluation. The default
  automated suite uses a clearly named deterministic pair-scoring stub and performs no paid calls.
- Persist one tenant-owned `retrieval_traces` row per retrieval with the query, modes, candidate
  snapshot, component scores, latency, and retriever/embedding/reranker/dataset versions. Candidate
  snapshots omit chunk text. Document deletion intentionally retains historical trace IDs and
  scores; knowledge-base or tenant deletion cascades traces, and user deletion nulls the actor ID.
- Use the immutable `m3-controlled-synthetic-v1` JSONL baseline: 50 synthetic policy documents and
  200 explicit relevance labels. The report records the file hash and marks the set as synthetic,
  not human-labeled production truth and not LLM-as-judge evidence. The ablation evaluates a fixed
  50-candidate window and reports Recall@5, MRR@10, NDCG@10, and p50/p95 retrieval latency.

## Consequences

Authorization is enforced before ranking, so neither RRF, reranking, API output, nor traces can
observe a cross-tenant or ACL-denied candidate. PostgreSQL is still the source of truth for
retrieval state and evaluation traces. The controlled dataset makes regressions reproducible but
overweights exact lexical identifiers, so its passing score does not establish production semantic
quality. A future production embedding can replace the existing provider without changing SQL,
fusion, trace, or evaluation contracts. Query text in traces is tenant-scoped operational data and
must follow the deployment's retention policy.
