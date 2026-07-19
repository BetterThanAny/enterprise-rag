from __future__ import annotations

import argparse
import hashlib
import json
import math
import tarfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import TypedDict, cast

import numpy as np

from enterprise_rag_core.evaluation import (
    CorpusDocument,
    RetrievalDataset,
    RetrievalQuery,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
)
from enterprise_rag_core.indexing import (
    DeterministicEmbeddingStub,
    EmbeddingProvider,
    FastEmbedEmbeddingProvider,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCIFACT_URL = "https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz"
SCIFACT_SHA256 = "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be"
SCIFACT_VERSION = "scifact-dev-2020"


class RetrievalMetrics(TypedDict):
    queries: int
    recall_at_5: float
    mrr_at_10: float
    ndcg_at_10: float
    document_embedding_seconds: float
    query_embedding_seconds: float
    search_latency_ms_p50: float
    search_latency_ms_p95: float


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare real semantic embeddings with a hash baseline on SciFact dev."
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="Pinned SciFact data.tar.gz; downloaded to the user cache when omitted.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "data/eval/reports/m6-scifact-bge-small-en-v1.5.json",
    )
    parser.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    parser.add_argument(
        "--model-cache",
        type=Path,
        default=Path("~/.cache/enterprise-rag/fastembed").expanduser(),
    )
    parser.add_argument("--batch-size", type=int, default=64)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_archive(path: Path | None) -> Path:
    destination = path or Path("~/.cache/enterprise-rag/scifact/data.tar.gz").expanduser()
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(SCIFACT_URL, timeout=60) as response:  # noqa: S310
            destination.write_bytes(response.read())
    return destination


def _jsonl_records(archive: tarfile.TarFile, member: str) -> list[dict[str, object]]:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ValueError(f"SciFact archive is missing {member}")
    records: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(extracted, start=1):
        parsed = json.loads(raw_line)
        if not isinstance(parsed, dict):
            raise ValueError(f"{member}:{line_number} must contain a JSON object")
        records.append(cast(dict[str, object], parsed))
    return records


def load_scifact_archive(path: Path, *, expected_sha256: str) -> RetrievalDataset:
    observed_sha256 = sha256_file(path)
    if observed_sha256 != expected_sha256:
        raise ValueError(
            f"SciFact archive checksum mismatch: expected {expected_sha256}, "
            f"observed {observed_sha256}"
        )
    with tarfile.open(path, "r:gz") as archive:
        corpus_records = _jsonl_records(archive, "data/corpus.jsonl")
        claim_records = _jsonl_records(archive, "data/claims_dev.jsonl")

    corpus = tuple(
        CorpusDocument(
            document_key=str(record["doc_id"]),
            title=str(record["title"]),
            content=" ".join(str(item) for item in cast(list[object], record["abstract"])),
        )
        for record in corpus_records
    )
    document_keys = {document.document_key for document in corpus}
    queries: list[RetrievalQuery] = []
    for record in claim_records:
        evidence = record.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            continue
        typed_evidence = cast(dict[str, object], evidence)
        relevant = frozenset(
            str(key) for key in typed_evidence if str(key) in document_keys
        )
        if not relevant:
            continue
        queries.append(
            RetrievalQuery(
                query_id=str(record["id"]),
                query=str(record["claim"]),
                relevant_document_keys=relevant,
            )
        )
    return RetrievalDataset(
        version=SCIFACT_VERSION,
        kind="public_human_labeled_scientific_retrieval",
        description=(
            "SciFact dev claims with public human evidence annotations; scientific-domain "
            "evidence, not enterprise-policy production truth."
        ),
        corpus=corpus,
        queries=tuple(queries),
    )


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _normalized_matrix(
    vectors: list[list[float]],
) -> np.ndarray[tuple[int, int], np.dtype[np.float32]]:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise ValueError("Embedding provider returned an empty or invalid matrix")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return cast(np.ndarray[tuple[int, int], np.dtype[np.float32]], matrix / norms)


def evaluate_provider(
    dataset: RetrievalDataset,
    provider: EmbeddingProvider,
) -> RetrievalMetrics:
    document_texts = [f"{document.title}\n{document.content}" for document in dataset.corpus]
    started = time.perf_counter()
    document_matrix = _normalized_matrix(provider.embed_documents(document_texts))
    document_embedding_seconds = time.perf_counter() - started
    document_keys = [document.document_key for document in dataset.corpus]

    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    search_latencies: list[float] = []
    query_embedding_seconds = 0.0
    for query in dataset.queries:
        query_started = time.perf_counter()
        query_vector = _normalized_matrix([provider.embed_query(query.query)])[0]
        query_embedding_seconds += time.perf_counter() - query_started
        search_started = time.perf_counter()
        scores = document_matrix @ query_vector
        top_indices = np.argsort(scores)[::-1][:10]
        search_latencies.append((time.perf_counter() - search_started) * 1000)
        ranking = [document_keys[int(index)] for index in top_indices]
        relevant = set(query.relevant_document_keys)
        recalls.append(recall_at_k(ranking, relevant, 5))
        reciprocal_ranks.append(reciprocal_rank_at_k(ranking, relevant, 10))
        ndcgs.append(ndcg_at_k(ranking, relevant, 10))

    return RetrievalMetrics(
        queries=len(dataset.queries),
        recall_at_5=round(fmean(recalls), 6),
        mrr_at_10=round(fmean(reciprocal_ranks), 6),
        ndcg_at_10=round(fmean(ndcgs), 6),
        document_embedding_seconds=round(document_embedding_seconds, 3),
        query_embedding_seconds=round(query_embedding_seconds, 3),
        search_latency_ms_p50=round(percentile(search_latencies, 0.50), 3),
        search_latency_ms_p95=round(percentile(search_latencies, 0.95), 3),
    )


def main() -> None:
    arguments = parse_arguments()
    archive = resolve_archive(arguments.archive)
    dataset = load_scifact_archive(archive, expected_sha256=SCIFACT_SHA256)
    semantic_provider = FastEmbedEmbeddingProvider(
        model_name=arguments.model,
        cache_dir=str(arguments.model_cache),
        batch_size=arguments.batch_size,
    )
    deterministic_provider = DeterministicEmbeddingStub(dimensions=semantic_provider.dimensions)
    deterministic = evaluate_provider(dataset, deterministic_provider)
    semantic = evaluate_provider(dataset, semantic_provider)
    recall_delta = round(semantic["recall_at_5"] - deterministic["recall_at_5"], 6)
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "version": dataset.version,
            "kind": dataset.kind,
            "description": dataset.description,
            "source": "https://github.com/allenai/scifact",
            "archive_url": SCIFACT_URL,
            "archive_sha256": SCIFACT_SHA256,
            "corpus_documents": len(dataset.corpus),
            "human_labeled_queries": len(dataset.queries),
            "claims_and_evidence_license": "CC BY 4.0",
            "abstracts_license": "ODC-By 1.0",
        },
        "providers": {
            "deterministic_hash_baseline": {
                "version": deterministic_provider.version,
                "dimensions": deterministic_provider.dimensions,
                "metrics": deterministic,
            },
            "real_semantic": {
                "version": semantic_provider.version,
                "dimensions": semantic_provider.dimensions,
                "metrics": semantic,
            },
        },
        "comparison": {
            "recall_at_5_absolute_delta": recall_delta,
            "minimum_absolute_delta": 0.20,
            "status": "passed" if recall_delta >= 0.20 else "non_finding_below_threshold",
        },
        "limitations": [
            "SciFact is scientific-domain retrieval, not enterprise-policy production traffic.",
            "The report validates semantic retrieval evidence, not generation quality.",
        ],
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    if recall_delta < 0.20:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
