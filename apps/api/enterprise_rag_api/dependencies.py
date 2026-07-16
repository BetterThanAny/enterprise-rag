from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core.config import Settings
from enterprise_rag_core.dispatching import JobDispatcher
from enterprise_rag_core.errors import AuthenticationError, AuthorizationError
from enterprise_rag_core.models import Role, User
from enterprise_rag_core.providers import GenerationProvider
from enterprise_rag_core.repositories import IdentityRepository
from enterprise_rag_core.reranking import CrossEncoderReranker
from enterprise_rag_core.security import decode_access_token
from enterprise_rag_core.services import TenantContext

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_dispatcher(request: Request) -> JobDispatcher:
    dispatcher: JobDispatcher = request.app.state.dispatcher
    return dispatcher


def get_reranker(request: Request) -> CrossEncoderReranker:
    reranker: CrossEncoderReranker = request.app.state.reranker
    return reranker


def get_providers(request: Request) -> Mapping[str, GenerationProvider]:
    providers: Mapping[str, GenerationProvider] = request.app.state.providers
    return providers


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if not token:
        raise AuthenticationError()
    user_id = decode_access_token(token, settings)
    user = await IdentityRepository(session).get_user(user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Invalid or expired access token", "invalid_access_token")
    return user


async def get_tenant_context(
    tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> TenantContext:
    membership = await IdentityRepository(session).get_membership(user.id, tenant_id)
    if membership is None:
        raise AuthorizationError(
            code="tenant_access_denied",
            message="User is not a member of this tenant",
        )
    return TenantContext(user_id=user.id, tenant_id=tenant_id, role=membership.role)


def require_roles(
    *allowed_roles: Role,
) -> Callable[[TenantContext], Awaitable[TenantContext]]:
    async def dependency(
        context: Annotated[TenantContext, Depends(get_tenant_context)],
    ) -> TenantContext:
        if context.role not in allowed_roles:
            raise AuthorizationError(
                code="insufficient_role",
                message="The tenant role does not permit this action",
            )
        return context

    return dependency


DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
CurrentTenant = Annotated[TenantContext, Depends(get_tenant_context)]
BackgroundDispatcher = Annotated[JobDispatcher, Depends(get_dispatcher)]
ConfiguredReranker = Annotated[CrossEncoderReranker, Depends(get_reranker)]
ConfiguredProviders = Annotated[Mapping[str, GenerationProvider], Depends(get_providers)]
