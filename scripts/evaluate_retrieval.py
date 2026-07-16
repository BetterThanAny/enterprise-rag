from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from typing import TypedDict, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, select

from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.evaluation import (
    RetrievalDataset,
    load_retrieval_dataset,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
)
from enterprise_rag_core.indexing import DeterministicEmbeddingStub
from enterprise_rag_core.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    KnowledgeBase,
    Membership,
    RetrievalMode,
    Role,
    Tenant,
    User,
)
from enterprise_rag_core.reranking import FlashRankCrossEncoder
from enterprise_rag_core.retrieval import RetrievalRepository, RetrievalService
from enterprise_rag_core.security import hash_password
from enterprise_rag_core.services import TenantContext
from enterprise_rag_core.storage import MinioObjectStorage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_NAMESPACE = uuid5(NAMESPACE_URL, "enterprise-rag:m3-retrieval-evaluation")


class Condition(TypedDict):
    name: str
    mode: RetrievalMode
    rerank: bool


class ConditionMetrics(TypedDict):
    recall_at_5: float
    mrr_at_10: float
    ndcg_at_10: float
    latency_ms_p50: float
    latency_ms_p95: float


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic retrieval ablations against PostgreSQL/pgvector."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Versioned JSONL retrieval dataset.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Report path; defaults to data/eval/reports/<dataset-version>.json.",
    )
    return parser.parse_args()


def stable_id(dataset_version: str, resource: str) -> UUID:
    return uuid5(EVALUATION_NAMESPACE, f"{dataset_version}:{resource}")


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def relative_delta(current: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return (current - baseline) / baseline


def finding_status(delta: float | None, threshold: float) -> str:
    if delta is None:
        return "non_finding_zero_baseline"
    if delta >= threshold:
        return "verified_improvement"
    if delta < 0:
        return "regression"
    return "non_finding_below_threshold"


async def replace_evaluation_corpus(
    *,
    settings: Settings,
    dataset: RetrievalDataset,
) -> tuple[TenantContext, UUID, dict[UUID, str]]:
    engine, session_factory = create_database_resources(settings.database_url)
    storage = MinioObjectStorage(settings)
    tenant_id = stable_id(dataset.version, "tenant")
    user_id = stable_id(dataset.version, "user")
    knowledge_base_id = stable_id(dataset.version, "knowledge-base")
    try:
        async with session_factory() as session:
            previous_tenant_id = await session.scalar(
                select(Tenant.id).where(Tenant.slug == f"retrieval-eval-{dataset.version}")
            )
            if previous_tenant_id is not None:
                await session.execute(delete(Tenant).where(Tenant.id == previous_tenant_id))
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
            if previous_tenant_id is not None:
                await storage.remove_prefix(f"tenants/{previous_tenant_id}/")

            tenant = Tenant(
                id=tenant_id,
                name=f"Retrieval evaluation {dataset.version}",
                slug=f"retrieval-eval-{dataset.version}",
            )
            user = User(
                id=user_id,
                email=f"retrieval-eval-{dataset.version}@example.com",
                password_hash=hash_password(secrets.token_urlsafe(32)),
                is_active=True,
            )
            session.add_all([tenant, user])
            await session.flush()
            session.add_all(
                [
                    Membership(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER),
                    KnowledgeBase(
                        id=knowledge_base_id,
                        tenant_id=tenant_id,
                        name=f"Evaluation {dataset.version}",
                        description=dataset.description,
                    ),
                ]
            )
            await session.flush()

            embeddings = DeterministicEmbeddingStub(dimensions=settings.embedding_dimensions)
            document_key_by_id: dict[UUID, str] = {}
            pending_chunks: list[Chunk] = []
            for corpus_document in dataset.corpus:
                document_id = stable_id(dataset.version, f"document:{corpus_document.document_key}")
                version_id = stable_id(dataset.version, f"version:{corpus_document.document_key}")
                content = corpus_document.content.encode()
                checksum = hashlib.sha256(content).hexdigest()
                object_key = (
                    f"tenants/{tenant_id}/evaluation/{dataset.version}/{document_id}/{checksum}.txt"
                )
                await storage.put_if_absent(
                    object_key,
                    content,
                    checksum=checksum,
                    content_type="text/plain",
                )
                session.add_all(
                    [
                        Document(
                            id=document_id,
                            tenant_id=tenant_id,
                            knowledge_base_id=knowledge_base_id,
                            filename=f"{corpus_document.document_key}.txt",
                            object_key=object_key,
                            checksum=checksum,
                            status=DocumentStatus.READY,
                        ),
                        DocumentVersion(
                            id=version_id,
                            tenant_id=tenant_id,
                            document_id=document_id,
                            version_number=1,
                            filename=f"{corpus_document.document_key}.txt",
                            object_key=object_key,
                            checksum=checksum,
                            status=DocumentVersionStatus.READY,
                            is_current=True,
                        ),
                    ]
                )
                pending_chunks.append(
                    Chunk(
                        id=stable_id(dataset.version, f"chunk:{corpus_document.document_key}:0"),
                        tenant_id=tenant_id,
                        document_id=document_id,
                        version_id=version_id,
                        ordinal=0,
                        content=corpus_document.content,
                        content_checksum=checksum,
                        embedding=embeddings.embed([corpus_document.content])[0],
                    )
                )
                document_key_by_id[document_id] = corpus_document.document_key
            await session.flush()
            session.add_all(pending_chunks)
            await session.commit()
        return (
            TenantContext(user_id=user_id, tenant_id=tenant_id, role=Role.OWNER),
            knowledge_base_id,
            document_key_by_id,
        )
    finally:
        await engine.dispose()


async def evaluate_condition(
    *,
    settings: Settings,
    dataset: RetrievalDataset,
    context: TenantContext,
    knowledge_base_id: UUID,
    document_key_by_id: dict[UUID, str],
    condition: Condition,
    reranker: FlashRankCrossEncoder,
    candidate_k: int,
) -> ConditionMetrics:
    engine, session_factory = create_database_resources(settings.database_url)
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    latencies: list[float] = []
    try:
        async with session_factory() as session:
            service = RetrievalService(
                RetrievalRepository(session),
                settings,
                reranker,
            )
            for evaluation_query in dataset.queries:
                result = await service.retrieve(
                    context=context,
                    knowledge_base_id=knowledge_base_id,
                    query=evaluation_query.query,
                    mode=condition["mode"],
                    top_k=10,
                    candidate_k=candidate_k,
                    rerank=condition["rerank"],
                    dataset_version=dataset.version,
                )
                ranking = [
                    document_key_by_id[candidate.document_id] for candidate in result.candidates
                ]
                relevant = set(evaluation_query.relevant_document_keys)
                recalls.append(recall_at_k(ranking, relevant, 5))
                reciprocal_ranks.append(reciprocal_rank_at_k(ranking, relevant, 10))
                ndcgs.append(ndcg_at_k(ranking, relevant, 10))
                latencies.append(result.duration_ms)
        return {
            "recall_at_5": round(fmean(recalls), 6),
            "mrr_at_10": round(fmean(reciprocal_ranks), 6),
            "ndcg_at_10": round(fmean(ndcgs), 6),
            "latency_ms_p50": round(median(latencies), 3),
            "latency_ms_p95": round(percentile(latencies, 0.95), 3),
        }
    finally:
        await engine.dispose()


async def run_evaluation(dataset_path: Path) -> dict[str, object]:
    settings = Settings()  # type: ignore[call-arg]
    dataset = load_retrieval_dataset(dataset_path)
    dataset_bytes = await asyncio.to_thread(dataset_path.read_bytes)
    if len(dataset.queries) < 200:
        raise RuntimeError("retrieval evaluation requires at least 200 labeled queries")
    context, knowledge_base_id, document_key_by_id = await replace_evaluation_corpus(
        settings=settings,
        dataset=dataset,
    )
    reranker = FlashRankCrossEncoder(
        model_name=settings.reranker_model_name,
        cache_dir=settings.reranker_cache_dir,
        max_length=settings.reranker_max_length,
    )
    conditions: tuple[Condition, ...] = (
        {"name": "lexical", "mode": RetrievalMode.LEXICAL, "rerank": False},
        {"name": "dense", "mode": RetrievalMode.DENSE, "rerank": False},
        {"name": "hybrid", "mode": RetrievalMode.HYBRID, "rerank": False},
        {"name": "hybrid_rerank", "mode": RetrievalMode.HYBRID, "rerank": True},
    )
    metrics: dict[str, ConditionMetrics] = {}
    evaluation_candidate_k = min(50, len(dataset.corpus))
    for condition in conditions:
        metrics[condition["name"]] = await evaluate_condition(
            settings=settings,
            dataset=dataset,
            context=context,
            knowledge_base_id=knowledge_base_id,
            document_key_by_id=document_key_by_id,
            condition=condition,
            reranker=reranker,
            candidate_k=evaluation_candidate_k,
        )

    hybrid_dense_delta = relative_delta(
        metrics["hybrid"]["ndcg_at_10"], metrics["dense"]["ndcg_at_10"]
    )
    rerank_delta = relative_delta(
        metrics["hybrid_rerank"]["mrr_at_10"], metrics["hybrid"]["mrr_at_10"]
    )
    release_recall = metrics["hybrid_rerank"]["recall_at_5"]
    return {
        "run_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "version": dataset.version,
            "kind": dataset.kind,
            "description": dataset.description,
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "corpus_documents": len(dataset.corpus),
            "labeled_queries": len(dataset.queries),
            "human_labeled": False,
            "llm_judge": False,
        },
        "configuration": {
            "retriever_version": settings.retrieval_config_version,
            "embedding_version": settings.embedding_model_version,
            "reranker_version": reranker.version,
            "rrf_rank_constant": settings.retrieval_rrf_rank_constant,
            "candidate_k": evaluation_candidate_k,
        },
        "metrics": metrics,
        "comparisons": {
            "hybrid_vs_dense_ndcg_at_10": {
                "relative_delta": hybrid_dense_delta,
                "threshold": 0.05,
                "status": finding_status(hybrid_dense_delta, 0.05),
            },
            "rerank_vs_hybrid_mrr_at_10": {
                "relative_delta": rerank_delta,
                "threshold": 0.05,
                "status": finding_status(rerank_delta, 0.05),
            },
        },
        "quality_gate": {
            "hybrid_rerank_recall_at_5": release_recall,
            "minimum": 0.85,
            "status": "passed" if release_recall >= 0.85 else "failed",
        },
    }


def main() -> None:
    arguments = parse_arguments()
    dataset_path = arguments.dataset.resolve()
    report = asyncio.run(run_evaluation(dataset_path))
    dataset_record = report["dataset"]
    if not isinstance(dataset_record, dict):
        raise RuntimeError("evaluation report dataset metadata is invalid")
    version = str(cast(dict[str, object], dataset_record)["version"])
    output = arguments.output or PROJECT_ROOT / "data" / "eval" / "reports" / f"{version}.json"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    quality_gate = report["quality_gate"]
    if not isinstance(quality_gate, dict):
        raise RuntimeError("evaluation report quality gate is invalid")
    if cast(dict[str, object], quality_gate).get("status") != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
