from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise_rag_core.models import (
    IndexJobAction,
    IndexJobStage,
    IndexJobStatus,
    RetrievalMode,
)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = Field(default="bearer")
    expires_in: int


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)


class KnowledgeBaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class JobSubmissionResponse(BaseModel):
    task_id: UUID
    document_id: UUID
    version_id: UUID
    status: IndexJobStatus


class IndexJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    version_id: UUID
    action: IndexJobAction
    status: IndexJobStatus
    stage: IndexJobStage
    attempts: int
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: RetrievalMode = RetrievalMode.HYBRID
    top_k: int = Field(default=5, ge=1, le=50)
    candidate_k: int = Field(default=20, ge=1, le=200)
    rerank: bool = False

    @model_validator(mode="after")
    def validate_candidate_count(self) -> RetrievalRequest:
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be greater than or equal to top_k")
        return self


class RetrievalCandidateResponse(BaseModel):
    rank: int
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    filename: str
    content: str
    page_number: int | None
    heading_path: str | None
    lexical_score: float | None
    dense_score: float | None
    fused_score: float | None
    rerank_score: float | None


class RetrievalResponse(BaseModel):
    trace_id: UUID
    mode: RetrievalMode
    retriever_version: str
    embedding_version: str
    reranker_version: str | None
    duration_ms: float
    results: list[RetrievalCandidateResponse]


class GenerationRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: RetrievalMode = RetrievalMode.HYBRID
    top_k: int = Field(default=5, ge=1, le=20)
    candidate_k: int = Field(default=20, ge=1, le=100)
    rerank: bool = True
    provider: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_candidate_count(self) -> GenerationRequest:
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be greater than or equal to top_k")
        return self


class EvaluationTargetInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    mode: RetrievalMode = RetrievalMode.HYBRID
    top_k: int = Field(default=5, ge=1, le=20)
    candidate_k: int = Field(default=20, ge=1, le=100)
    rerank: bool = True
    provider: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_candidate_count(self) -> EvaluationTargetInput:
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be greater than or equal to top_k")
        return self


class EvaluationTargetRequest(BaseModel):
    input: EvaluationTargetInput | str

    def normalized_input(self) -> EvaluationTargetInput:
        if isinstance(self.input, str):
            return EvaluationTargetInput(query=self.input)
        return self.input


class EvaluationTargetResponse(BaseModel):
    output: dict[str, Any]
    usage: dict[str, Any]
    metadata: dict[str, Any]


class RetrievalTracePath(BaseModel):
    trace_id: UUID
    span_id: str
    mode: RetrievalMode
    duration_ms: float
    retriever_version: str
    embedding_version: str
    candidates: list[dict[str, object]]


class RerankTracePath(BaseModel):
    enabled: bool
    span_id: str | None
    version: str | None


class GenerationTracePath(BaseModel):
    trace_id: UUID
    span_id: str
    provider_span_id: str | None
    provider: str
    model: str
    status: str
    provider_config_version: str
    prompt_version: str
    ttft_ms: float | None
    duration_ms: float | None
    input_tokens: int
    output_tokens: int
    usage_source: str
    estimated_cost_usd: float
    provider_attempts: int
    citations: list[dict[str, object]]
    error_code: str | None
    error_message: str | None


class QuestionAnswerTraceResponse(BaseModel):
    trace_id: str
    request_id: str
    retrieval: RetrievalTracePath
    rerank: RerankTracePath
    generation: GenerationTracePath
