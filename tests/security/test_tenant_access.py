from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core.models import Membership, Role, Tenant, User
from enterprise_rag_core.security import hash_password


@dataclass(frozen=True)
class SeededIdentity:
    email: str
    password: str
    tenant_id: UUID | None


async def seed_identity(
    session: AsyncSession,
    *,
    email: str,
    role: Role | None,
    tenant: Tenant | None,
) -> SeededIdentity:
    password = "test-" + uuid4().hex
    user = User(email=email, password_hash=hash_password(password), is_active=True)
    session.add(user)
    await session.flush()
    if role is not None and tenant is not None:
        session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=role))
    await session.commit()
    return SeededIdentity(email=email, password=password, tenant_id=tenant.id if tenant else None)


async def login(client: AsyncClient, identity: SeededIdentity) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        data={"username": identity.email, "password": identity.password},
    )
    assert response.status_code == 200
    return str(response.json()["access_token"])


def tenant_headers(token: str, tenant_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-ID": str(tenant_id)}


@pytest.fixture
async def identities(
    db_session: AsyncSession,
) -> tuple[SeededIdentity, SeededIdentity, SeededIdentity, SeededIdentity]:
    tenant_a = Tenant(name="Shared Display Name", slug="tenant-a")
    tenant_b = Tenant(name="Shared Display Name", slug="tenant-b")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()
    admin_a = await seed_identity(
        db_session,
        email="admin-a@example.com",
        role=Role.ADMIN,
        tenant=tenant_a,
    )
    admin_b = await seed_identity(
        db_session,
        email="admin-b@example.com",
        role=Role.ADMIN,
        tenant=tenant_b,
    )
    viewer_a = await seed_identity(
        db_session,
        email="viewer-a@example.com",
        role=Role.VIEWER,
        tenant=tenant_a,
    )
    outsider = await seed_identity(
        db_session,
        email="outsider@example.com",
        role=None,
        tenant=None,
    )
    return admin_a, admin_b, viewer_a, outsider


@pytest.mark.security
async def test_authentication_and_membership_failures_are_stable(
    api_client: AsyncClient,
    identities: tuple[SeededIdentity, SeededIdentity, SeededIdentity, SeededIdentity],
) -> None:
    admin_a, admin_b, _, outsider = identities
    assert admin_a.tenant_id is not None
    assert admin_b.tenant_id is not None

    missing = await api_client.get(
        "/api/v1/knowledge-bases",
        headers={"X-Tenant-ID": str(admin_a.tenant_id)},
    )
    outsider_token = await login(api_client, outsider)
    not_a_member = await api_client.get(
        "/api/v1/knowledge-bases",
        headers=tenant_headers(outsider_token, admin_a.tenant_id),
    )
    admin_a_token = await login(api_client, admin_a)
    forged_tenant = await api_client.get(
        "/api/v1/knowledge-bases",
        headers=tenant_headers(admin_a_token, admin_b.tenant_id),
    )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "authentication_required"
    assert not_a_member.status_code == 403
    assert not_a_member.json()["error"]["code"] == "tenant_access_denied"
    assert forged_tenant.status_code == 403
    assert forged_tenant.json()["error"]["code"] == "tenant_access_denied"


@pytest.mark.security
async def test_role_and_tenant_boundaries_are_enforced_in_queries(
    api_client: AsyncClient,
    identities: tuple[SeededIdentity, SeededIdentity, SeededIdentity, SeededIdentity],
) -> None:
    admin_a, admin_b, viewer_a, _ = identities
    assert admin_a.tenant_id is not None
    assert admin_b.tenant_id is not None
    assert viewer_a.tenant_id is not None
    admin_a_token = await login(api_client, admin_a)
    admin_b_token = await login(api_client, admin_b)
    viewer_token = await login(api_client, viewer_a)

    viewer_create = await api_client.post(
        "/api/v1/knowledge-bases",
        headers=tenant_headers(viewer_token, viewer_a.tenant_id),
        json={"name": "Policies"},
    )
    created_a = await api_client.post(
        "/api/v1/knowledge-bases",
        headers=tenant_headers(admin_a_token, admin_a.tenant_id),
        json={"name": "Policies"},
    )
    created_b = await api_client.post(
        "/api/v1/knowledge-bases",
        headers=tenant_headers(admin_b_token, admin_b.tenant_id),
        json={"name": "Policies"},
    )

    assert viewer_create.status_code == 403
    assert viewer_create.json()["error"]["code"] == "insufficient_role"
    assert created_a.status_code == 201
    assert created_b.status_code == 201
    assert created_a.json()["id"] != created_b.json()["id"]

    list_a = await api_client.get(
        "/api/v1/knowledge-bases",
        headers=tenant_headers(admin_a_token, admin_a.tenant_id),
    )
    list_b = await api_client.get(
        "/api/v1/knowledge-bases",
        headers=tenant_headers(admin_b_token, admin_b.tenant_id),
    )
    cross_tenant = await api_client.get(
        f"/api/v1/knowledge-bases/{created_b.json()['id']}",
        headers=tenant_headers(admin_a_token, admin_a.tenant_id),
    )

    assert [item["id"] for item in list_a.json()] == [created_a.json()["id"]]
    assert [item["id"] for item in list_b.json()] == [created_b.json()["id"]]
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["error"]["code"] == "knowledge_base_not_found"


@pytest.mark.security
async def test_bad_password_is_rejected(
    api_client: AsyncClient,
    identities: tuple[SeededIdentity, SeededIdentity, SeededIdentity, SeededIdentity],
) -> None:
    admin_a, _, _, _ = identities

    response = await api_client.post(
        "/api/v1/auth/login",
        data={"username": admin_a.email, "password": "not-the-password"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.security
async def test_all_four_roles_have_explicit_write_behavior(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(name="Role Tenant", slug="role-tenant")
    db_session.add(tenant)
    await db_session.flush()
    identities = {
        role: await seed_identity(
            db_session,
            email=f"{role.value}@example.com",
            role=role,
            tenant=tenant,
        )
        for role in Role
    }

    for role, identity in identities.items():
        token = await login(api_client, identity)
        assert identity.tenant_id is not None
        response = await api_client.post(
            "/api/v1/knowledge-bases",
            headers=tenant_headers(token, identity.tenant_id),
            json={"name": f"{role.value} knowledge"},
        )
        expected_status = 201 if role in {Role.OWNER, Role.ADMIN} else 403
        assert response.status_code == expected_status
