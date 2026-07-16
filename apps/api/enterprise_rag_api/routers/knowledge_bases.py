from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from enterprise_rag_api.dependencies import CurrentTenant, DatabaseSession, require_roles
from enterprise_rag_core.models import Role
from enterprise_rag_core.repositories import KnowledgeBaseRepository
from enterprise_rag_core.schemas import (
    ErrorResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
)
from enterprise_rag_core.services import KnowledgeBaseService, TenantContext

router = APIRouter(prefix="/api/v1/knowledge-bases", tags=["knowledge-bases"])


@router.post(
    "",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    session: DatabaseSession,
    context: Annotated[
        TenantContext,
        Depends(require_roles(Role.OWNER, Role.ADMIN)),
    ],
) -> KnowledgeBaseResponse:
    service = KnowledgeBaseService(KnowledgeBaseRepository(session))
    model = await service.create(
        context=context,
        name=payload.name,
        description=payload.description,
    )
    return KnowledgeBaseResponse.model_validate(model)


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    session: DatabaseSession,
    context: CurrentTenant,
) -> list[KnowledgeBaseResponse]:
    service = KnowledgeBaseService(KnowledgeBaseRepository(session))
    models = await service.list(context)
    return [KnowledgeBaseResponse.model_validate(model) for model in models]


@router.get(
    "/{knowledge_base_id}",
    response_model=KnowledgeBaseResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_knowledge_base(
    knowledge_base_id: UUID,
    session: DatabaseSession,
    context: CurrentTenant,
) -> KnowledgeBaseResponse:
    service = KnowledgeBaseService(KnowledgeBaseRepository(session))
    model = await service.get(context, knowledge_base_id)
    return KnowledgeBaseResponse.model_validate(model)
