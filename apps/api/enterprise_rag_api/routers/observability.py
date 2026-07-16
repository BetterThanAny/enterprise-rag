from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from enterprise_rag_api.dependencies import (
    ConfiguredProviders,
    ConfiguredReranker,
    CurrentTenant,
    DatabaseSession,
    get_settings,
)
from enterprise_rag_core.config import Settings
from enterprise_rag_core.generation import GenerationService
from enterprise_rag_core.logging import request_id_context
from enterprise_rag_core.models import GenerationStatus
from enterprise_rag_core.observability import prometheus_payload
from enterprise_rag_core.retrieval import RetrievalRepository, RetrievalService
from enterprise_rag_core.schemas import (
    EvaluationTargetRequest,
    EvaluationTargetResponse,
    GenerationTracePath,
    QuestionAnswerTraceResponse,
    RerankTracePath,
    RetrievalTracePath,
)
from enterprise_rag_core.traces import ReconstructedTrace, TraceService

router = APIRouter(tags=["observability"])


def trace_response(value: ReconstructedTrace) -> QuestionAnswerTraceResponse:
    generation = value.generation
    retrieval = value.retrieval
    return QuestionAnswerTraceResponse(
        trace_id=generation.trace_id,
        request_id=generation.request_id,
        retrieval=RetrievalTracePath(
            trace_id=retrieval.id,
            span_id=retrieval.span_id,
            mode=retrieval.mode,
            duration_ms=retrieval.duration_ms,
            retriever_version=retrieval.retriever_version,
            embedding_version=retrieval.embedding_version,
            candidates=retrieval.candidates,
        ),
        rerank=RerankTracePath(
            enabled=retrieval.rerank,
            span_id=retrieval.rerank_span_id,
            version=retrieval.reranker_version,
        ),
        generation=GenerationTracePath(
            trace_id=generation.id,
            span_id=generation.span_id,
            provider_span_id=generation.provider_span_id,
            provider=generation.provider,
            model=generation.model,
            status=generation.status.value,
            provider_config_version=generation.provider_config_version,
            prompt_version=generation.prompt_version,
            ttft_ms=generation.ttft_ms,
            duration_ms=generation.duration_ms,
            input_tokens=generation.input_tokens,
            output_tokens=generation.output_tokens,
            usage_source=generation.usage_source,
            estimated_cost_usd=float(generation.estimated_cost_usd),
            provider_attempts=generation.provider_attempts,
            citations=generation.citations,
            error_code=generation.error_code,
            error_message=generation.error_message,
        ),
    )


@router.get("/metrics")
async def metrics() -> Response:
    body, content_type = prometheus_payload()
    return Response(content=body, media_type=content_type)


@router.get("/api/v1/traces/{generation_trace_id}", response_model=QuestionAnswerTraceResponse)
async def get_trace(
    generation_trace_id: UUID,
    session: DatabaseSession,
    context: CurrentTenant,
) -> QuestionAnswerTraceResponse:
    value = await TraceService(session).get(
        context=context,
        generation_trace_id=generation_trace_id,
    )
    return trace_response(value)


@router.post(
    "/api/v1/knowledge-bases/{knowledge_base_id}/evaluations",
    response_model=EvaluationTargetResponse,
)
async def evaluate_target(
    request: Request,
    knowledge_base_id: UUID,
    payload: EvaluationTargetRequest,
    session: DatabaseSession,
    context: CurrentTenant,
    reranker: ConfiguredReranker,
    providers: ConfiguredProviders,
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvaluationTargetResponse:
    target = payload.normalized_input()
    service = GenerationService(
        session,
        RetrievalService(RetrievalRepository(session), settings, reranker),
        settings,
        providers,
    )
    answer_parts: list[str] = []
    citation_ids: list[str] = []
    meta: dict[str, object] | None = None
    terminal_status = "failed"
    async for event in service.stream(
        context=context,
        knowledge_base_id=knowledge_base_id,
        query=target.query,
        mode=target.mode,
        top_k=target.top_k,
        candidate_k=target.candidate_k,
        rerank=target.rerank,
        provider_name=target.provider,
        cancel_event=request.state.cancel_event,
        request_id=request_id_context.get(),
    ):
        if event.event == "meta":
            meta = event.data
        elif event.event == "token":
            answer_parts.append(str(event.data["text"]))
        elif event.event == "citation":
            citation_ids.append(str(event.data["chunk_id"]))
        elif event.event == "done":
            terminal_status = str(event.data["status"])
    if meta is None:
        return EvaluationTargetResponse(
            output={"answer": "", "citation_ids": [], "refused": False, "status": "failed"},
            usage={},
            metadata={"error": "generation_failed_before_trace"},
        )
    reconstructed = await TraceService(session).get(
        context=context,
        generation_trace_id=UUID(str(meta["generation_trace_id"])),
    )
    generation = reconstructed.generation
    retrieval = reconstructed.retrieval
    return EvaluationTargetResponse(
        output={
            "answer": "".join(answer_parts),
            "citation_ids": citation_ids,
            "refused": generation.status is GenerationStatus.ABSTAINED,
            "status": terminal_status,
        },
        usage={
            "input_tokens": generation.input_tokens,
            "output_tokens": generation.output_tokens,
            "usage_source": generation.usage_source,
            "estimated_cost_usd": float(generation.estimated_cost_usd),
        },
        metadata={
            "generation_trace_id": str(generation.id),
            "retrieval_trace_id": str(retrieval.id),
            "trace_id": generation.trace_id,
            "retrieved_ids": [
                str(candidate["document_id"]) for candidate in retrieval.candidates
            ],
            "provider_attempts": generation.provider_attempts,
        },
    )
