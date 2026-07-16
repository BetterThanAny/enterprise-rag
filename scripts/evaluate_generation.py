from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import delete, select

from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.generation import GenerationService
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
from enterprise_rag_core.providers import DeterministicGenerationStub
from enterprise_rag_core.reranking import DeterministicCrossEncoderStub
from enterprise_rag_core.retrieval import RetrievalRepository, RetrievalService
from enterprise_rag_core.security import hash_password
from enterprise_rag_core.services import TenantContext
from enterprise_rag_core.storage import MinioObjectStorage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_NAMESPACE = uuid5(NAMESPACE_URL, "enterprise-rag:m4-generation-evaluation")


@dataclass(frozen=True)
class CorpusItem:
    key: str
    content: str


@dataclass(frozen=True)
class QueryItem:
    query_id: str
    kind: str
    query: str
    relevant_document_keys: frozenset[str]


@dataclass(frozen=True)
class GenerationDataset:
    version: str
    kind: str
    description: str
    corpus: list[CorpusItem]
    queries: list[QueryItem]


def load_dataset(path: Path) -> GenerationDataset:
    records = [
        cast(dict[str, object], json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    metadata = records[0]
    corpus = [
        CorpusItem(str(record["document_key"]), str(record["content"]))
        for record in records
        if record.get("type") == "corpus"
    ]
    queries = [
        QueryItem(
            query_id=str(record["query_id"]),
            kind=str(record["kind"]),
            query=str(record["query"]),
            relevant_document_keys=frozenset(
                str(item) for item in cast(list[object], record["relevant_document_keys"])
            ),
        )
        for record in records
        if record.get("type") == "query"
    ]
    if len(corpus) < 20 or sum(item.kind == "answer" for item in queries) < 20:
        raise RuntimeError("generation dataset must contain at least 20 corpus and answer samples")
    if sum(item.kind == "abstain" for item in queries) < 20:
        raise RuntimeError("generation dataset must contain at least 20 abstention samples")
    return GenerationDataset(
        version=str(metadata["version"]),
        kind=str(metadata["kind"]),
        description=str(metadata["description"]),
        corpus=corpus,
        queries=queries,
    )


def stable_id(version: str, resource: str) -> UUID:
    return uuid5(EVALUATION_NAMESPACE, f"{version}:{resource}")


async def seed_corpus(
    settings: Settings,
    dataset: GenerationDataset,
) -> tuple[TenantContext, UUID, dict[UUID, str]]:
    engine, session_factory = create_database_resources(settings.database_url)
    storage = MinioObjectStorage(settings)
    tenant_id = stable_id(dataset.version, "tenant")
    user_id = stable_id(dataset.version, "user")
    knowledge_base_id = stable_id(dataset.version, "knowledge-base")
    document_key_by_id: dict[UUID, str] = {}
    try:
        async with session_factory() as session:
            previous = await session.scalar(
                select(Tenant.id).where(Tenant.slug == f"generation-eval-{dataset.version}")
            )
            if previous is not None:
                await session.execute(delete(Tenant).where(Tenant.id == previous))
                await session.commit()
                await storage.remove_prefix(f"tenants/{previous}/")
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
            session.add_all(
                [
                    Tenant(
                        id=tenant_id,
                        name=f"Generation evaluation {dataset.version}",
                        slug=f"generation-eval-{dataset.version}",
                    ),
                    User(
                        id=user_id,
                        email=f"generation-eval-{dataset.version}@example.com",
                        password_hash=hash_password(secrets.token_urlsafe(32)),
                        is_active=True,
                    ),
                ]
            )
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
            embedder = DeterministicEmbeddingStub(dimensions=settings.embedding_dimensions)
            for item in dataset.corpus:
                document_id = stable_id(dataset.version, f"document:{item.key}")
                version_id = stable_id(dataset.version, f"version:{item.key}")
                chunk_id = stable_id(dataset.version, f"chunk:{item.key}:0")
                payload = item.content.encode()
                checksum = hashlib.sha256(payload).hexdigest()
                object_key = f"tenants/{tenant_id}/evaluation/{document_id}/{checksum}.txt"
                await storage.put_if_absent(
                    object_key,
                    payload,
                    checksum=checksum,
                    content_type="text/plain",
                )
                session.add_all(
                    [
                        Document(
                            id=document_id,
                            tenant_id=tenant_id,
                            knowledge_base_id=knowledge_base_id,
                            filename=f"{item.key}.txt",
                            object_key=object_key,
                            checksum=checksum,
                            status=DocumentStatus.READY,
                        ),
                        DocumentVersion(
                            id=version_id,
                            tenant_id=tenant_id,
                            document_id=document_id,
                            version_number=1,
                            filename=f"{item.key}.txt",
                            object_key=object_key,
                            checksum=checksum,
                            status=DocumentVersionStatus.READY,
                            is_current=True,
                        ),
                    ]
                )
                await session.flush()
                session.add(
                    Chunk(
                        id=chunk_id,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        version_id=version_id,
                        ordinal=0,
                        content=item.content,
                        content_checksum=checksum,
                        embedding=embedder.embed([item.content])[0],
                    )
                )
                document_key_by_id[document_id] = item.key
            await session.commit()
        return (
            TenantContext(user_id=user_id, tenant_id=tenant_id, role=Role.OWNER),
            knowledge_base_id,
            document_key_by_id,
        )
    finally:
        await engine.dispose()


async def evaluate(path: Path) -> dict[str, object]:
    dataset = load_dataset(path)
    dataset_bytes = await asyncio.to_thread(path.read_bytes)
    settings = Settings().model_copy(  # type: ignore[call-arg]
        update={"generation_dataset_version": dataset.version}
    )
    context, knowledge_base_id, document_key_by_id = await seed_corpus(settings, dataset)
    engine, session_factory = create_database_resources(settings.database_url)
    correct_answers = 0
    answer_samples = 0
    correct_abstentions = 0
    abstention_samples = 0
    try:
        async with session_factory() as session:
            retrieval = RetrievalService(
                RetrievalRepository(session), settings, DeterministicCrossEncoderStub()
            )
            provider = DeterministicGenerationStub()
            service = GenerationService(
                session,
                retrieval,
                settings,
                {provider.definition.name: provider},
            )
            for query in dataset.queries:
                events = [
                    event
                    async for event in service.stream(
                        context=context,
                        knowledge_base_id=knowledge_base_id,
                        query=query.query,
                        mode=RetrievalMode.HYBRID,
                        top_k=5,
                        candidate_k=20,
                        rerank=False,
                        provider_name=provider.definition.name,
                        cancel_event=asyncio.Event(),
                    )
                ]
                done = next((event for event in events if event.event == "done"), None)
                citations = [event.data for event in events if event.event == "citation"]
                if query.kind == "answer":
                    answer_samples += 1
                    cited_keys = {
                        document_key_by_id[UUID(str(citation["document_id"]))]
                        for citation in citations
                    }
                    if (
                        done is not None
                        and done.data.get("status") == "succeeded"
                        and citations
                        and cited_keys <= query.relevant_document_keys
                        and all(citation.get("excerpt") for citation in citations)
                    ):
                        correct_answers += 1
                else:
                    abstention_samples += 1
                    if (
                        done is not None
                        and done.data.get("status") == "abstained"
                        and not citations
                    ):
                        correct_abstentions += 1
    finally:
        await engine.dispose()
    citation_accuracy = correct_answers / answer_samples
    abstention_accuracy = correct_abstentions / abstention_samples
    return {
        "run_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "version": dataset.version,
            "kind": dataset.kind,
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "corpus_documents": len(dataset.corpus),
            "answer_samples": answer_samples,
            "abstention_samples": abstention_samples,
            "human_labeled": False,
            "llm_judge": False,
        },
        "configuration": {
            "provider": "deterministic",
            "model": "deterministic-grounded-answer-v1",
            "prompt_version": settings.generation_prompt_version,
            "retriever_version": settings.retrieval_config_version,
            "embedding_version": settings.embedding_model_version,
            "reranker_version": None,
        },
        "metrics": {
            "citation_correct_samples": correct_answers,
            "citation_accuracy": citation_accuracy,
            "correct_abstentions": correct_abstentions,
            "abstention_accuracy": abstention_accuracy,
        },
        "quality_gate": {
            "minimum": 0.9,
            "citation_status": "passed" if citation_accuracy >= 0.9 else "failed",
            "abstention_status": "passed" if abstention_accuracy >= 0.9 else "failed",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate M4 citations and abstention")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    dataset_path = arguments.dataset.resolve()
    report = asyncio.run(evaluate(dataset_path))
    version = str(cast(dict[str, object], report["dataset"])["version"])
    output = arguments.output or PROJECT_ROOT / "data/eval/reports" / f"{version}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    gate = cast(dict[str, object], report["quality_gate"])
    if gate["citation_status"] != "passed" or gate["abstention_status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
