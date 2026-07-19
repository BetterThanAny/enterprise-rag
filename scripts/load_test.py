from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import secrets
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from sqlalchemy import delete, func, insert, select, text

from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.indexing import DeterministicEmbeddingStub
from enterprise_rag_core.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    KnowledgeBase,
    Membership,
    Role,
    Tenant,
    User,
)
from enterprise_rag_core.security import hash_password
from enterprise_rag_core.storage import MinioObjectStorage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOAD_NAMESPACE = uuid5(NAMESPACE_URL, "enterprise-rag:m5-load-v1")
DATASET_VERSION = "m5-deterministic-load-v1"


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def stable_id(resource: str) -> UUID:
    return uuid5(LOAD_NAMESPACE, resource)


def chunk_content(index: int) -> str:
    return (
        f"loadtoken{index % 200:03d} enterprise recovery observability policy "
        f"deterministic performance chunk {index:05d}"
    )


async def replace_load_corpus(
    settings: Settings,
    *,
    chunks: int,
) -> tuple[UUID, str, str, float]:
    started = time.perf_counter()
    engine, session_factory = create_database_resources(settings.database_url)
    storage = MinioObjectStorage(settings)
    tenant_id = stable_id("tenant")
    user_id = stable_id("user")
    knowledge_base_id = stable_id("knowledge-base")
    password = secrets.token_urlsafe(24)
    document_count = min(100, chunks)
    try:
        async with session_factory() as session:
            if await session.get(Tenant, tenant_id) is not None:
                await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
                await session.commit()
                await storage.remove_prefix(f"tenants/{tenant_id}/")
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
            session.add_all(
                [
                    Tenant(
                        id=tenant_id,
                        name="M5 deterministic load tenant",
                        slug=f"m5-load-{DATASET_VERSION}",
                    ),
                    User(
                        id=user_id,
                        email="m5-load@example.com",
                        password_hash=hash_password(password),
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
                        name="M5 load corpus",
                        description=f"{DATASET_VERSION}; {chunks} deterministic chunks",
                    ),
                ]
            )
            await session.flush()

            per_document = math.ceil(chunks / document_count)
            document_ranges: list[tuple[int, int, UUID, UUID]] = []
            for document_index in range(document_count):
                start = document_index * per_document
                end = min(chunks, start + per_document)
                if start >= end:
                    break
                document_id = stable_id(f"document:{chunks}:{document_index}")
                version_id = stable_id(f"version:{chunks}:{document_index}")
                content = "\n".join(chunk_content(index) for index in range(start, end)).encode()
                checksum = hashlib.sha256(content).hexdigest()
                object_key = (
                    f"tenants/{tenant_id}/load/{DATASET_VERSION}/{document_id}/{checksum}.txt"
                )
                await storage.put_if_absent(
                    object_key,
                    content,
                    checksum=checksum,
                    content_type="text/plain",
                )
                filename = f"load-{document_index:03d}.txt"
                session.add_all(
                    [
                        Document(
                            id=document_id,
                            tenant_id=tenant_id,
                            knowledge_base_id=knowledge_base_id,
                            filename=filename,
                            object_key=object_key,
                            checksum=checksum,
                            status=DocumentStatus.READY,
                        ),
                        DocumentVersion(
                            id=version_id,
                            tenant_id=tenant_id,
                            document_id=document_id,
                            version_number=1,
                            filename=filename,
                            object_key=object_key,
                            checksum=checksum,
                            status=DocumentVersionStatus.READY,
                            is_current=True,
                        ),
                    ]
                )
                document_ranges.append((start, end, document_id, version_id))
            await session.flush()

            embedder = DeterministicEmbeddingStub(dimensions=settings.embedding_dimensions)
            inserted = 0
            for start, end, document_id, version_id in document_ranges:
                for batch_start in range(start, end, 500):
                    batch_end = min(end, batch_start + 500)
                    contents = [chunk_content(index) for index in range(batch_start, batch_end)]
                    embeddings = await asyncio.to_thread(
                        embedder.embed_documents, contents
                    )
                    rows = [
                        {
                            "id": stable_id(f"chunk:{chunks}:{index}"),
                            "tenant_id": tenant_id,
                            "document_id": document_id,
                            "version_id": version_id,
                            "ordinal": index - start,
                            "content": content,
                            "content_checksum": hashlib.sha256(content.encode()).hexdigest(),
                            "development_embedding": embedding,
                        }
                        for index, content, embedding in zip(
                            range(batch_start, batch_end),
                            contents,
                            embeddings,
                            strict=True,
                        )
                    ]
                    await session.execute(insert(Chunk), rows)
                    inserted += len(rows)
            await session.commit()
            actual = await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.tenant_id == tenant_id)
            )
            if actual != chunks or inserted != chunks:
                raise RuntimeError(f"load corpus mismatch: expected={chunks} actual={actual}")
            await session.execute(text("ANALYZE chunks"))
            await session.commit()
        return tenant_id, str(knowledge_base_id), password, time.perf_counter() - started
    finally:
        await engine.dispose()


async def authenticate(api_url: str, password: str) -> str:
    async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
        response = await client.post(
            "/api/v1/auth/login",
            data={"username": "m5-load@example.com", "password": password},
        )
        response.raise_for_status()
        return str(response.json()["access_token"])


async def run_requests(
    *,
    api_url: str,
    tenant_id: UUID,
    knowledge_base_id: str,
    token: str,
    concurrency: int,
    requests: int,
) -> tuple[list[float], list[float]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": str(tenant_id),
    }
    semaphore = asyncio.Semaphore(concurrency)
    client_latencies: list[float] = []
    server_latencies: list[float] = []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=api_url,
        headers=headers,
        timeout=30,
        limits=limits,
    ) as client:

        async def invoke(index: int, *, measured: bool) -> None:
            async with semaphore:
                started = time.perf_counter()
                response = await client.post(
                    f"/api/v1/knowledge-bases/{knowledge_base_id}/retrieve",
                    json={
                        "query": f"loadtoken{index % 200:03d}",
                        "mode": "hybrid",
                        "top_k": 5,
                        "candidate_k": 20,
                        "rerank": True,
                    },
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                response.raise_for_status()
                payload = response.json()
                if not payload["results"] or not payload["trace_id"]:
                    raise RuntimeError("retrieval load response omitted results or trace")
                if measured:
                    client_latencies.append(elapsed_ms)
                    server_latencies.append(float(payload["duration_ms"]))

        await asyncio.gather(*(invoke(index, measured=False) for index in range(concurrency)))
        await asyncio.gather(*(invoke(index, measured=True) for index in range(requests)))
    return client_latencies, server_latencies


async def execute(arguments: argparse.Namespace) -> dict[str, object]:
    settings = Settings()  # type: ignore[call-arg]
    tenant_id, knowledge_base_id, password, seed_seconds = await replace_load_corpus(
        settings,
        chunks=arguments.chunks,
    )
    token = await authenticate(arguments.api_url, password)
    client, server = await run_requests(
        api_url=arguments.api_url,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        token=token,
        concurrency=arguments.concurrency,
        requests=arguments.requests,
    )
    client_p95 = percentile(client, 0.95)
    report: dict[str, object] = {
        "run_at": datetime.now(UTC).isoformat(),
        "dataset": {
            "version": DATASET_VERSION,
            "generator_sha256": hashlib.sha256(chunk_content(0).encode()).hexdigest(),
            "chunks": arguments.chunks,
        },
        "configuration": {
            "concurrency": arguments.concurrency,
            "requests": arguments.requests,
            "mode": "hybrid",
            "rerank": True,
            "llm_included": False,
            "retriever_version": settings.retrieval_config_version,
            "embedding_version": settings.embedding_model_version,
        },
        "seeding": {"seconds": round(seed_seconds, 3)},
        "client_latency_ms": {
            "p50": round(percentile(client, 0.50), 3),
            "p95": round(client_p95, 3),
            "p99": round(percentile(client, 0.99), 3),
        },
        "server_retrieval_latency_ms": {
            "p50": round(percentile(server, 0.50), 3),
            "p95": round(percentile(server, 0.95), 3),
            "p99": round(percentile(server, 0.99), 3),
        },
        "quality_gate": {
            "maximum_p95_ms": arguments.max_p95_ms,
            "observed_p95_ms": round(client_p95, 3),
            "status": "passed" if client_p95 <= arguments.max_p95_ms else "failed",
        },
    }
    return report


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load-test the tenant-filtered retrieval API")
    parser.add_argument("--chunks", type=int, default=50_000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--max-p95-ms", type=float, default=500)
    parser.add_argument("--api-url", default="http://127.0.0.1:18000")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.chunks < 1 or arguments.concurrency < 1 or arguments.requests < 1:
        parser.error("chunks, concurrency, and requests must be positive")
    return arguments


def main() -> None:
    arguments = parse_arguments()
    report = asyncio.run(execute(arguments))
    output = arguments.output or (
        PROJECT_ROOT / "data/eval/reports" / f"m5-load-{arguments.chunks}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["quality_gate"]["status"] != "passed":  # type: ignore[index]
        raise SystemExit(1)


if __name__ == "__main__":
    main()
