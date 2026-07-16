from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from minio import Minio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag_core.config import Settings
from enterprise_rag_core.errors import AuthenticationError, NotFoundError
from enterprise_rag_core.models import KnowledgeBase, Role
from enterprise_rag_core.repositories import IdentityRepository, KnowledgeBaseRepository
from enterprise_rag_core.security import create_access_token, verify_password


@dataclass(frozen=True)
class TenantContext:
    user_id: UUID
    tenant_id: UUID
    role: Role


class AuthenticationService:
    def __init__(self, repository: IdentityRepository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    async def login(self, email: str, password: str) -> str:
        user = await self.repository.get_user_by_email(email.strip().lower())
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid email or password", "invalid_credentials")
        return create_access_token(user.id, self.settings)


class KnowledgeBaseService:
    def __init__(self, repository: KnowledgeBaseRepository) -> None:
        self.repository = repository

    async def create(
        self,
        *,
        context: TenantContext,
        name: str,
        description: str | None,
    ) -> KnowledgeBase:
        return await self.repository.create(
            tenant_id=context.tenant_id,
            name=name,
            description=description,
        )

    async def list(self, context: TenantContext) -> list[KnowledgeBase]:
        return await self.repository.list_for_tenant(context.tenant_id)

    async def get(self, context: TenantContext, knowledge_base_id: UUID) -> KnowledgeBase:
        knowledge_base = await self.repository.get_for_tenant(
            tenant_id=context.tenant_id,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base is None:
            raise NotFoundError(
                code="knowledge_base_not_found",
                message="Knowledge base not found",
            )
        return knowledge_base


class ReadinessService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings

    async def check(self) -> tuple[bool, dict[str, str]]:
        checks = {
            "database": await self._check_database(),
            "minio": await self._check_minio(),
            "redis": await self._check_redis(),
        }
        return all(value == "ok" for value in checks.values()), checks

    async def _check_database(self) -> str:
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
            return "ok"
        except Exception:
            return "unavailable"

    async def _check_redis(self) -> str:
        client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            self.settings.redis_url
        )
        try:
            ping_result = await client.ping()  # pyright: ignore[reportUnknownMemberType]
            return "ok" if bool(ping_result) else "unavailable"
        except Exception:
            return "unavailable"
        finally:
            await client.aclose()

    async def _check_minio(self) -> str:
        client = Minio(
            self.settings.minio_endpoint,
            access_key=self.settings.minio_access_key,
            secret_key=self.settings.minio_secret_key.get_secret_value(),
            secure=self.settings.minio_secure,
        )
        try:
            exists = await asyncio.to_thread(client.bucket_exists, self.settings.minio_bucket)
            return "ok" if exists else "unavailable"
        except Exception:
            return "unavailable"
