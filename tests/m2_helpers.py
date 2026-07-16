from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core.models import KnowledgeBase, Membership, Role, Tenant, User
from enterprise_rag_core.security import hash_password


@dataclass(frozen=True)
class M2Identity:
    tenant_id: UUID
    user_id: UUID
    knowledge_base_id: UUID
    email: str
    password: str


async def seed_m2_identity(session: AsyncSession, suffix: str = "default") -> M2Identity:
    tenant = Tenant(name=f"M2 Tenant {suffix}", slug=f"m2-{suffix}")
    user = User(
        email=f"m2-{suffix}@example.com",
        password_hash=hash_password("m2-test-password"),
        is_active=True,
    )
    session.add_all([tenant, user])
    await session.flush()
    session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=Role.OWNER))
    knowledge_base = KnowledgeBase(tenant_id=tenant.id, name="Documents")
    session.add(knowledge_base)
    await session.commit()
    return M2Identity(
        tenant_id=tenant.id,
        user_id=user.id,
        knowledge_base_id=knowledge_base.id,
        email=user.email,
        password="m2-test-password",
    )


async def auth_headers(client: AsyncClient, identity: M2Identity) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": identity.email, "password": identity.password},
    )
    assert response.status_code == 200
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-Tenant-ID": str(identity.tenant_id),
    }


async def upload_document(
    client: AsyncClient,
    identity: M2Identity,
    *,
    filename: str,
    content: bytes,
    idempotency_key: str,
) -> Response:
    headers = await auth_headers(client, identity)
    headers["Idempotency-Key"] = idempotency_key
    return await client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/documents",
        headers=headers,
        files={"file": (filename, content, "application/octet-stream")},
    )
