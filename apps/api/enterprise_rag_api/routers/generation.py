from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from enterprise_rag_api.dependencies import (
    ConfiguredProviders,
    ConfiguredReranker,
    CurrentTenant,
    DatabaseSession,
    get_settings,
)
from enterprise_rag_core.config import Settings
from enterprise_rag_core.generation import GenerationService, encode_sse
from enterprise_rag_core.logging import request_id_context
from enterprise_rag_core.retrieval import RetrievalRepository, RetrievalService
from enterprise_rag_core.schemas import GenerationRequest

router = APIRouter(prefix="/api/v1", tags=["generation"])


class DisconnectAware(Protocol):
    async def is_disconnected(self) -> bool: ...


async def stream_until_disconnect(
    request: DisconnectAware,
    stream: AsyncGenerator[str, None],
    cancel_event: asyncio.Event,
) -> AsyncGenerator[str, None]:
    try:
        while True:
            if await request.is_disconnected():
                cancel_event.set()
                break
            try:
                item = await anext(stream)
            except StopAsyncIteration:
                break
            yield item
    except asyncio.CancelledError:
        cancel_event.set()
        raise
    finally:
        cancel_event.set()
        await stream.aclose()


@router.post("/knowledge-bases/{knowledge_base_id}/answers/stream")
async def answer_stream(
    request: Request,
    knowledge_base_id: UUID,
    payload: GenerationRequest,
    session: DatabaseSession,
    context: CurrentTenant,
    reranker: ConfiguredReranker,
    providers: ConfiguredProviders,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    service = GenerationService(
        session,
        RetrievalService(RetrievalRepository(session), settings, reranker),
        settings,
        providers,
    )
    cancel_event = asyncio.Event()
    captured_request_id = request_id_context.get()

    async def encoded_events() -> AsyncGenerator[str, None]:
        async for event in service.stream(
            context=context,
            knowledge_base_id=knowledge_base_id,
            query=payload.query,
            mode=payload.mode,
            top_k=payload.top_k,
            candidate_k=payload.candidate_k,
            rerank=payload.rerank,
            provider_name=payload.provider,
            cancel_event=cancel_event,
            request_id=captured_request_id,
        ):
            yield encode_sse(event.event, event.data)

    return StreamingResponse(
        stream_until_disconnect(request, encoded_events(), cancel_event),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
