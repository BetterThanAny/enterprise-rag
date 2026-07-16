from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from uuid import UUID

from opentelemetry.trace import Span
from sqlalchemy import and_, exists, func, literal_column, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from enterprise_rag_core.config import Settings
from enterprise_rag_core.errors import NotFoundError, ValidationDomainError
from enterprise_rag_core.indexing import DeterministicEmbeddingStub, EmbeddingProvider
from enterprise_rag_core.models import (
    AclPermission,
    Chunk,
    Document,
    DocumentAcl,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    KnowledgeBase,
    RetrievalMode,
    RetrievalTrace,
    Role,
)
from enterprise_rag_core.observability import RETRIEVAL_DURATION, span_identifiers, start_span
from enterprise_rag_core.reranking import CrossEncoderReranker
from enterprise_rag_core.services import TenantContext

LEXICAL_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


@dataclass
class RetrievalCandidate:
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    filename: str
    content: str
    page_number: int | None = None
    heading_path: str | None = None
    lexical_score: float | None = None
    dense_score: float | None = None
    fused_score: float | None = None
    rerank_score: float | None = None

    def trace_payload(self, rank: int) -> dict[str, object]:
        return {
            "rank": rank,
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "filename": self.filename,
            "page_number": self.page_number,
            "heading_path": self.heading_path,
            "lexical_score": self.lexical_score,
            "dense_score": self.dense_score,
            "fused_score": self.fused_score,
            "rerank_score": self.rerank_score,
        }


@dataclass(frozen=True)
class RetrievalResult:
    trace_id: UUID
    mode: RetrievalMode
    retriever_version: str
    embedding_version: str
    reranker_version: str | None
    duration_ms: float
    candidates: list[RetrievalCandidate]


def reciprocal_rank_fusion(
    rankings: list[list[UUID]],
    *,
    rank_constant: int,
) -> list[tuple[UUID, float]]:
    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")
    scores: dict[UUID, float] = {}
    first_seen: dict[UUID, int] = {}
    sequence = 0
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            if item_id not in first_seen:
                first_seen[item_id] = sequence
                sequence += 1
            scores[item_id] = scores.get(item_id, 0.0) + (1.0 / (rank_constant + rank))
    return sorted(scores.items(), key=lambda item: (-item[1], first_seen[item[0]], str(item[0])))


def lexical_websearch_query(query: str) -> str:
    """Build a parse-safe disjunction while PostgreSQL still owns token normalization."""
    return " OR ".join(LEXICAL_TOKEN_PATTERN.findall(query.casefold()))


class RetrievalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_knowledge_base(self, tenant_id: UUID, knowledge_base_id: UUID) -> None:
        found = await self.session.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.id == knowledge_base_id,
            )
        )
        if found is None:
            raise NotFoundError(
                code="knowledge_base_not_found",
                message="Knowledge base not found",
            )

    def _acl_predicate(self, context: TenantContext) -> ColumnElement[bool]:
        if context.role in {Role.OWNER, Role.ADMIN}:
            return true()
        any_acl = exists(
            select(DocumentAcl.id).where(
                DocumentAcl.tenant_id == context.tenant_id,
                DocumentAcl.document_id == Document.id,
            )
        ).correlate(Document)
        user_acl = exists(
            select(DocumentAcl.id).where(
                DocumentAcl.tenant_id == context.tenant_id,
                DocumentAcl.document_id == Document.id,
                DocumentAcl.user_id == context.user_id,
                DocumentAcl.permission.in_([AclPermission.READ, AclPermission.WRITE]),
            )
        ).correlate(Document)
        return or_(not_(any_acl), user_acl)

    def _base_predicates(
        self,
        context: TenantContext,
        knowledge_base_id: UUID,
    ) -> tuple[ColumnElement[bool], ...]:
        return (
            Chunk.tenant_id == context.tenant_id,
            DocumentVersion.tenant_id == context.tenant_id,
            Document.tenant_id == context.tenant_id,
            Document.knowledge_base_id == knowledge_base_id,
            Document.status == DocumentStatus.READY,
            DocumentVersion.status == DocumentVersionStatus.READY,
            DocumentVersion.is_current.is_(True),
            self._acl_predicate(context),
        )

    @staticmethod
    def _joins() -> tuple[ColumnElement[bool], ColumnElement[bool]]:
        version_join = and_(
            DocumentVersion.tenant_id == Chunk.tenant_id,
            DocumentVersion.document_id == Chunk.document_id,
            DocumentVersion.id == Chunk.version_id,
        )
        document_join = and_(
            Document.tenant_id == Chunk.tenant_id,
            Document.id == Chunk.document_id,
        )
        return version_join, document_join

    async def lexical(
        self,
        *,
        context: TenantContext,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
    ) -> list[RetrievalCandidate]:
        websearch_query = lexical_websearch_query(query)
        if not websearch_query:
            return []
        ts_query = func.websearch_to_tsquery(literal_column("'simple'::regconfig"), websearch_query)
        score = func.ts_rank_cd(Chunk.search_vector, ts_query, 32).label("lexical_score")
        version_join, document_join = self._joins()
        statement = (
            select(Chunk, Document.filename, score)
            .join(DocumentVersion, version_join)
            .join(Document, document_join)
            .where(
                *self._base_predicates(context, knowledge_base_id),
                Chunk.search_vector.bool_op("@@")(ts_query),
            )
            .order_by(score.desc(), Chunk.id)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).tuples().all()
        return [
            RetrievalCandidate(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                version_id=chunk.version_id,
                filename=filename,
                content=chunk.content,
                page_number=chunk.page_number,
                heading_path=chunk.heading_path,
                lexical_score=float(raw_score),
            )
            for chunk, filename, raw_score in rows
        ]

    async def dense(
        self,
        *,
        context: TenantContext,
        knowledge_base_id: UUID,
        query_vector: list[float],
        limit: int,
    ) -> list[RetrievalCandidate]:
        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        version_join, document_join = self._joins()
        statement = (
            select(Chunk, Document.filename, distance)
            .join(DocumentVersion, version_join)
            .join(Document, document_join)
            .where(*self._base_predicates(context, knowledge_base_id))
            # pgvector HNSW can satisfy a pure distance order. Adding a secondary
            # UUID sort forces PostgreSQL to scan and sort the filtered corpus.
            .order_by(distance)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).tuples().all()
        return [
            RetrievalCandidate(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                version_id=chunk.version_id,
                filename=filename,
                content=chunk.content,
                page_number=chunk.page_number,
                heading_path=chunk.heading_path,
                dense_score=1.0 - float(raw_distance),
            )
            for chunk, filename, raw_distance in rows
        ]

    async def save_trace(self, trace: RetrievalTrace) -> RetrievalTrace:
        self.session.add(trace)
        await self.session.commit()
        await self.session.refresh(trace)
        return trace


class RetrievalService:
    def __init__(
        self,
        repository: RetrievalRepository,
        settings: Settings,
        reranker: CrossEncoderReranker,
        *,
        embedding: EmbeddingProvider | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.reranker = reranker
        self.embedding = embedding or DeterministicEmbeddingStub(
            dimensions=settings.embedding_dimensions
        )

    async def retrieve(
        self,
        *,
        context: TenantContext,
        knowledge_base_id: UUID,
        query: str,
        mode: RetrievalMode,
        top_k: int,
        candidate_k: int,
        rerank: bool,
        dataset_version: str | None = None,
        parent_span: Span | None = None,
    ) -> RetrievalResult:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValidationDomainError(code="empty_query", message="Query must not be empty")
        if candidate_k < top_k:
            raise ValidationDomainError(
                code="invalid_candidate_k",
                message="candidate_k must be greater than or equal to top_k",
            )
        await self.repository.ensure_knowledge_base(context.tenant_id, knowledge_base_id)
        with start_span(
            "rag.retrieval",
            {
                "rag.retrieval.mode": mode.value,
                "rag.retrieval.rerank": rerank,
                "rag.retrieval.top_k": top_k,
                "rag.retrieval.candidate_k": candidate_k,
            },
            parent=parent_span,
        ) as retrieval_span:
            return await self._retrieve_with_span(
                context=context,
                knowledge_base_id=knowledge_base_id,
                normalized_query=normalized_query,
                mode=mode,
                top_k=top_k,
                candidate_k=candidate_k,
                rerank=rerank,
                dataset_version=dataset_version,
                retrieval_span=retrieval_span,
            )

    async def _retrieve_with_span(
        self,
        *,
        context: TenantContext,
        knowledge_base_id: UUID,
        normalized_query: str,
        mode: RetrievalMode,
        top_k: int,
        candidate_k: int,
        rerank: bool,
        dataset_version: str | None,
        retrieval_span: Span,
    ) -> RetrievalResult:
        started = time.perf_counter()
        lexical: list[RetrievalCandidate] = []
        dense: list[RetrievalCandidate] = []
        if mode in {RetrievalMode.LEXICAL, RetrievalMode.HYBRID}:
            lexical = await self.repository.lexical(
                context=context,
                knowledge_base_id=knowledge_base_id,
                query=normalized_query,
                limit=candidate_k,
            )
        if mode in {RetrievalMode.DENSE, RetrievalMode.HYBRID}:
            query_vector = (await asyncio.to_thread(self.embedding.embed, [normalized_query]))[0]
            dense = await self.repository.dense(
                context=context,
                knowledge_base_id=knowledge_base_id,
                query_vector=query_vector,
                limit=candidate_k,
            )
        candidates = self._combine(mode, lexical, dense, candidate_k)
        reranker_version: str | None = None
        rerank_span_id: str | None = None
        if rerank and candidates:
            with start_span(
                "rag.rerank",
                {"rag.reranker.version": self.reranker.version},
                parent=retrieval_span,
            ) as rerank_span:
                _, rerank_span_id = span_identifiers(rerank_span)
                scores = await asyncio.to_thread(
                    self.reranker.score,
                    normalized_query,
                    [candidate.content for candidate in candidates],
                )
            if len(scores) != len(candidates):
                raise RuntimeError("Reranker returned an unexpected score count")
            for candidate, score in zip(candidates, scores, strict=True):
                candidate.rerank_score = score
            previous_order = {
                candidate.chunk_id: index for index, candidate in enumerate(candidates)
            }
            candidates.sort(
                key=lambda candidate: (
                    -(candidate.rerank_score or 0.0),
                    previous_order[candidate.chunk_id],
                )
            )
            reranker_version = self.reranker.version
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        trace_id, retrieval_span_id = span_identifiers(retrieval_span)
        RETRIEVAL_DURATION.labels(mode=mode.value, rerank=str(rerank).lower()).observe(
            duration_ms / 1000
        )
        trace = await self.repository.save_trace(
            RetrievalTrace(
                tenant_id=context.tenant_id,
                knowledge_base_id=knowledge_base_id,
                user_id=context.user_id,
                query=normalized_query,
                mode=mode,
                top_k=top_k,
                candidate_k=candidate_k,
                rerank=rerank,
                retriever_version=self.settings.retrieval_config_version,
                embedding_version=self.settings.embedding_model_version,
                reranker_version=reranker_version,
                dataset_version=dataset_version,
                duration_ms=duration_ms,
                trace_id=trace_id,
                span_id=retrieval_span_id,
                rerank_span_id=rerank_span_id,
                candidates=[
                    candidate.trace_payload(rank)
                    for rank, candidate in enumerate(candidates, start=1)
                ],
            )
        )
        return RetrievalResult(
            trace_id=trace.id,
            mode=mode,
            retriever_version=trace.retriever_version,
            embedding_version=trace.embedding_version,
            reranker_version=trace.reranker_version,
            duration_ms=trace.duration_ms,
            candidates=candidates[:top_k],
        )

    def _combine(
        self,
        mode: RetrievalMode,
        lexical: list[RetrievalCandidate],
        dense: list[RetrievalCandidate],
        candidate_k: int,
    ) -> list[RetrievalCandidate]:
        if mode is RetrievalMode.LEXICAL:
            return lexical[:candidate_k]
        if mode is RetrievalMode.DENSE:
            return dense[:candidate_k]
        by_id = {candidate.chunk_id: candidate for candidate in lexical}
        for candidate in dense:
            existing = by_id.get(candidate.chunk_id)
            if existing is None:
                by_id[candidate.chunk_id] = candidate
            else:
                existing.dense_score = candidate.dense_score
        fused = reciprocal_rank_fusion(
            [
                [candidate.chunk_id for candidate in lexical],
                [candidate.chunk_id for candidate in dense],
            ],
            rank_constant=self.settings.retrieval_rrf_rank_constant,
        )
        combined: list[RetrievalCandidate] = []
        for chunk_id, score in fused[:candidate_k]:
            candidate = by_id[chunk_id]
            candidate.fused_score = score
            combined.append(candidate)
        return combined
