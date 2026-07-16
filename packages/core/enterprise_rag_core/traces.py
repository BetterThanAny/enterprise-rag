from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core.errors import NotFoundError
from enterprise_rag_core.models import GenerationTrace, RetrievalTrace
from enterprise_rag_core.services import TenantContext


@dataclass(frozen=True)
class ReconstructedTrace:
    generation: GenerationTrace
    retrieval: RetrievalTrace


class TraceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self,
        *,
        context: TenantContext,
        generation_trace_id: UUID,
    ) -> ReconstructedTrace:
        generation = (
            await self.session.execute(
                select(GenerationTrace).where(
                    GenerationTrace.id == generation_trace_id,
                    GenerationTrace.tenant_id == context.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if generation is None:
            raise NotFoundError(
                code="generation_trace_not_found",
                message="Generation trace not found",
            )
        retrieval = (
            await self.session.execute(
                select(RetrievalTrace).where(
                    RetrievalTrace.id == generation.retrieval_trace_id,
                    RetrievalTrace.tenant_id == context.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if retrieval is None:
            raise NotFoundError(
                code="retrieval_trace_not_found",
                message="Retrieval trace not found",
            )
        return ReconstructedTrace(generation=generation, retrieval=retrieval)
