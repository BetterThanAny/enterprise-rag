from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core.models import Document, KnowledgeBase, Tenant


@pytest.mark.integration
async def test_two_tenants_can_use_same_knowledge_base_name(db_session: AsyncSession) -> None:
    tenant_a = Tenant(name="Acme", slug="acme")
    tenant_b = Tenant(name="Acme", slug="acme-second")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()
    db_session.add_all(
        [
            KnowledgeBase(tenant_id=tenant_a.id, name="Policies"),
            KnowledgeBase(tenant_id=tenant_b.id, name="Policies"),
        ]
    )

    await db_session.commit()


@pytest.mark.integration
async def test_duplicate_knowledge_base_name_within_tenant_is_rejected(
    db_session: AsyncSession,
) -> None:
    tenant = Tenant(name="Acme", slug="acme")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add_all(
        [
            KnowledgeBase(tenant_id=tenant.id, name="Policies"),
            KnowledgeBase(tenant_id=tenant.id, name="Policies"),
        ]
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.integration
async def test_database_rejects_cross_tenant_document_relationship(
    db_session: AsyncSession,
) -> None:
    tenant_a = Tenant(name="Acme", slug="acme")
    tenant_b = Tenant(name="Beta", slug="beta")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()
    knowledge_base = KnowledgeBase(tenant_id=tenant_a.id, name="Policies")
    db_session.add(knowledge_base)
    await db_session.flush()
    db_session.add(
        Document(
            tenant_id=tenant_b.id,
            knowledge_base_id=knowledge_base.id,
            filename="policy.txt",
            object_key=f"tenants/{tenant_b.id}/objects/{uuid4()}",
            checksum="0" * 64,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.commit()
