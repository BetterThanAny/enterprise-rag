from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from enterprise_rag_api.dependencies import (
    ConfiguredReranker,
    CurrentTenant,
    DatabaseSession,
    get_settings,
)
from enterprise_rag_core.config import Settings
from enterprise_rag_core.retrieval import RetrievalRepository, RetrievalService
from enterprise_rag_core.schemas import (
    RetrievalCandidateResponse,
    RetrievalRequest,
    RetrievalResponse,
)

router = APIRouter(prefix="/api/v1", tags=["retrieval"])


@router.post(
    "/knowledge-bases/{knowledge_base_id}/retrieve",
    response_model=RetrievalResponse,
)
async def retrieve(
    knowledge_base_id: UUID,
    payload: RetrievalRequest,
    session: DatabaseSession,
    context: CurrentTenant,
    reranker: ConfiguredReranker,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RetrievalResponse:
    result = await RetrievalService(
        RetrievalRepository(session),
        settings,
        reranker,
    ).retrieve(
        context=context,
        knowledge_base_id=knowledge_base_id,
        query=payload.query,
        mode=payload.mode,
        top_k=payload.top_k,
        candidate_k=payload.candidate_k,
        rerank=payload.rerank,
    )
    return RetrievalResponse(
        trace_id=result.trace_id,
        mode=result.mode,
        retriever_version=result.retriever_version,
        embedding_version=result.embedding_version,
        reranker_version=result.reranker_version,
        duration_ms=result.duration_ms,
        results=[
            RetrievalCandidateResponse(
                rank=rank,
                chunk_id=candidate.chunk_id,
                document_id=candidate.document_id,
                version_id=candidate.version_id,
                filename=candidate.filename,
                content=candidate.content,
                page_number=candidate.page_number,
                heading_path=candidate.heading_path,
                lexical_score=candidate.lexical_score,
                dense_score=candidate.dense_score,
                fused_score=candidate.fused_score,
                rerank_score=candidate.rerank_score,
            )
            for rank, candidate in enumerate(result.candidates, start=1)
        ],
    )
