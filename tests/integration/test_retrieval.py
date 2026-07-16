from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.m2_helpers import M2Identity, auth_headers, seed_m2_identity, upload_document

from enterprise_rag_core.config import Settings
from enterprise_rag_core.indexing import IndexingPipeline
from enterprise_rag_core.models import IndexJobStatus, RetrievalTrace


async def index_text(
    client: AsyncClient,
    identity: M2Identity,
    settings: Settings,
    *,
    filename: str,
    content: str,
    idempotency_key: str,
) -> UUID:
    response = await upload_document(
        client,
        identity,
        filename=filename,
        content=content.encode(),
        idempotency_key=idempotency_key,
    )
    assert response.status_code == 202
    result = await IndexingPipeline(settings).process(UUID(response.json()["task_id"]))
    assert result.status is IndexJobStatus.SUCCEEDED
    return UUID(response.json()["document_id"])


@pytest.mark.integration
async def test_lexical_dense_hybrid_and_rerank_persist_complete_traces(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "retrieval-modes")
    retention_id = await index_text(
        api_client,
        identity,
        integration_settings,
        filename="retention.txt",
        content="retention policy policyalpha0001 keeps audit records for seven years",
        idempotency_key="retrieval-retention",
    )
    await index_text(
        api_client,
        identity,
        integration_settings,
        filename="incident.txt",
        content="incident response policybeta0002 defines severity and escalation",
        idempotency_key="retrieval-incident",
    )
    await index_text(
        api_client,
        identity,
        integration_settings,
        filename="travel.txt",
        content="travel expense policygamma0003 requires itemized receipts",
        idempotency_key="retrieval-travel",
    )
    headers = await auth_headers(api_client, identity)

    responses: dict[str, Response] = {}
    for mode in ("lexical", "dense", "hybrid"):
        responses[mode] = await api_client.post(
            f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/retrieve",
            headers=headers,
            json={
                "query": "retention policy policyalpha0001",
                "mode": mode,
                "top_k": 3,
                "candidate_k": 3,
                "rerank": False,
            },
        )
        assert responses[mode].status_code == 200
        assert responses[mode].json()["mode"] == mode
        assert responses[mode].json()["results"]

    reranked = await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/retrieve",
        headers=headers,
        json={
            "query": "retention policy policyalpha0001",
            "mode": "hybrid",
            "top_k": 3,
            "candidate_k": 3,
            "rerank": True,
        },
    )
    assert reranked.status_code == 200
    assert UUID(reranked.json()["results"][0]["document_id"]) == retention_id
    assert reranked.json()["results"][0]["rerank_score"] is not None

    trace_count = await db_session.scalar(select(func.count()).select_from(RetrievalTrace))
    traces = list((await db_session.execute(select(RetrievalTrace))).scalars())
    assert trace_count == 4
    assert all(trace.candidates for trace in traces)
    assert all(trace.retriever_version and trace.embedding_version for trace in traces)
    assert all(trace.duration_ms >= 0 for trace in traces)
    assert traces[-1].reranker_version is not None


@pytest.mark.integration
async def test_retrieval_rejects_empty_queries_and_invalid_candidate_windows(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    identity = await seed_m2_identity(db_session, "retrieval-validation")
    headers = await auth_headers(api_client, identity)
    endpoint = f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/retrieve"

    empty_query = await api_client.post(
        endpoint,
        headers=headers,
        json={"query": "   ", "mode": "lexical", "top_k": 1, "candidate_k": 1},
    )
    invalid_window = await api_client.post(
        endpoint,
        headers=headers,
        json={"query": "policy", "mode": "hybrid", "top_k": 5, "candidate_k": 4},
    )

    assert empty_query.status_code == 422
    assert empty_query.json()["error"]["code"] == "empty_query"
    assert invalid_window.status_code == 422
    assert invalid_window.json()["error"]["code"] == "validation_error"
