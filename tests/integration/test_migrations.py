from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from enterprise_rag_core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv executable is required to run migration tests")
    return executable


UV = resolve_uv()
EXPECTED_TABLES = {
    "alembic_version",
    "document_acl",
    "document_versions",
    "documents",
    "generation_traces",
    "chunks",
    "index_jobs",
    "knowledge_bases",
    "memberships",
    "retrieval_traces",
    "tenants",
    "users",
}


@pytest.mark.integration
async def test_empty_database_migrates_to_head(integration_settings: Settings) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = integration_settings.database_url

    downgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "downgrade",
        "base",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await downgrade.wait() == 0
    upgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "upgrade",
        "head",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await upgrade.wait() == 0

    engine = create_async_engine(integration_settings.database_url)
    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    await engine.dispose()

    assert table_names == EXPECTED_TABLES


@pytest.mark.integration
async def test_m1_schema_upgrades_to_head_without_losing_documents_or_jobs(
    integration_settings: Settings,
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = integration_settings.database_url
    downgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "downgrade",
        "20260713_0001",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await downgrade.wait() == 0
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    document_id = uuid4()
    job_id = uuid4()
    engine = create_async_engine(integration_settings.database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants (id, name, slug) VALUES "
                "(:tenant_id, 'Migration Tenant', :slug)"
            ),
            {"tenant_id": tenant_id, "slug": f"migration-{tenant_id}"},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name) VALUES "
                "(:knowledge_base_id, :tenant_id, 'Migration KB')"
            ),
            {"knowledge_base_id": knowledge_base_id, "tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, knowledge_base_id, filename, object_key, checksum, status) "
                "VALUES (:document_id, :tenant_id, :knowledge_base_id, 'old.txt', "
                ":object_key, :checksum, 'pending')"
            ),
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "object_key": f"tenants/{tenant_id}/old.txt",
                "checksum": "a" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO index_jobs "
                "(id, tenant_id, document_id, idempotency_key, status) "
                "VALUES (:job_id, :tenant_id, :document_id, 'old-job', 'pending')"
            ),
            {"job_id": job_id, "tenant_id": tenant_id, "document_id": document_id},
        )
    upgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "upgrade",
        "head",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await upgrade.wait() == 0
    async with engine.connect() as connection:
        version = (
            await connection.execute(
                text(
                    "SELECT id, document_id, version_number FROM document_versions "
                    "WHERE document_id = :document_id"
                ),
                {"document_id": document_id},
            )
        ).one()
        migrated_job_version = await connection.scalar(
            text("SELECT version_id FROM index_jobs WHERE id = :job_id"),
            {"job_id": job_id},
        )
    await engine.dispose()
    assert version.id == document_id
    assert version.document_id == document_id
    assert version.version_number == 1
    assert migrated_job_version == document_id


@pytest.mark.integration
async def test_m2_schema_upgrades_to_m3_without_losing_chunks(
    integration_settings: Settings,
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = integration_settings.database_url
    downgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "downgrade",
        "20260713_0002",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await downgrade.wait() == 0
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    chunk_id = uuid4()
    engine = create_async_engine(integration_settings.database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO tenants (id, name, slug) VALUES "
                "(:tenant_id, 'M3 Migration Tenant', :slug)"
            ),
            {"tenant_id": tenant_id, "slug": f"m3-migration-{tenant_id}"},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name) VALUES "
                "(:knowledge_base_id, :tenant_id, 'M3 Migration KB')"
            ),
            {"knowledge_base_id": knowledge_base_id, "tenant_id": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, knowledge_base_id, filename, object_key, checksum, status) "
                "VALUES (:document_id, :tenant_id, :knowledge_base_id, 'm3.txt', "
                ":object_key, :checksum, 'ready')"
            ),
            {
                "document_id": document_id,
                "tenant_id": tenant_id,
                "knowledge_base_id": knowledge_base_id,
                "object_key": f"tenants/{tenant_id}/m3.txt",
                "checksum": "b" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO document_versions "
                "(id, tenant_id, document_id, version_number, filename, object_key, "
                "checksum, status, is_current) VALUES "
                "(:version_id, :tenant_id, :document_id, 1, 'm3.txt', :object_key, "
                ":checksum, 'ready', true)"
            ),
            {
                "version_id": version_id,
                "tenant_id": tenant_id,
                "document_id": document_id,
                "object_key": f"tenants/{tenant_id}/m3-version.txt",
                "checksum": "b" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO chunks "
                "(id, tenant_id, document_id, version_id, ordinal, content, "
                "content_checksum, embedding) VALUES "
                "(:chunk_id, :tenant_id, :document_id, :version_id, 0, "
                "'migrationtoken searchable policy', :checksum, "
                "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1]'::vector)"
            ),
            {
                "chunk_id": chunk_id,
                "tenant_id": tenant_id,
                "document_id": document_id,
                "version_id": version_id,
                "checksum": "c" * 64,
            },
        )
    upgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "upgrade",
        "head",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await upgrade.wait() == 0
    async with engine.connect() as connection:
        migrated_chunk = (
            await connection.execute(
                text(
                    "SELECT content, search_vector::text AS search_vector "
                    "FROM chunks WHERE id = :chunk_id"
                ),
                {"chunk_id": chunk_id},
            )
        ).one()
        chunk_indexes = set(
            (
                await connection.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'")
                )
            ).scalars()
        )
    await engine.dispose()
    assert migrated_chunk.content == "migrationtoken searchable policy"
    assert "migrationtoken" in migrated_chunk.search_vector
    assert {"ix_chunks_search_vector", "ix_chunks_embedding_hnsw"} <= chunk_indexes


@pytest.mark.integration
async def test_m3_schema_upgrades_to_m4_without_losing_chunk_content(
    integration_settings: Settings,
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = integration_settings.database_url
    downgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "downgrade",
        "20260713_0003",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await downgrade.wait() == 0
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    document_id = uuid4()
    version_id = uuid4()
    chunk_id = uuid4()
    engine = create_async_engine(integration_settings.database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'M4 Tenant', :slug)"),
            {"id": tenant_id, "slug": f"m4-{tenant_id}"},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name) "
                "VALUES (:id, :tenant, 'M4 KB')"
            ),
            {"id": knowledge_base_id, "tenant": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, knowledge_base_id, filename, object_key, checksum, status) "
                "VALUES (:id, :tenant, :kb, 'legacy.txt', :object_key, :checksum, 'ready')"
            ),
            {
                "id": document_id,
                "tenant": tenant_id,
                "kb": knowledge_base_id,
                "object_key": f"tenants/{tenant_id}/legacy.txt",
                "checksum": "d" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO document_versions "
                "(id, tenant_id, document_id, version_number, filename, object_key, "
                "checksum, status, is_current) VALUES "
                "(:id, :tenant, :document, 1, 'legacy.txt', :object_key, :checksum, "
                "'ready', true)"
            ),
            {
                "id": version_id,
                "tenant": tenant_id,
                "document": document_id,
                "object_key": f"tenants/{tenant_id}/legacy-version.txt",
                "checksum": "d" * 64,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO chunks "
                "(id, tenant_id, document_id, version_id, ordinal, content, "
                "content_checksum, embedding) VALUES "
                "(:id, :tenant, :document, :version, 0, 'legacy chunk', :checksum, "
                "'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1]'::vector)"
            ),
            {
                "id": chunk_id,
                "tenant": tenant_id,
                "document": document_id,
                "version": version_id,
                "checksum": "e" * 64,
            },
        )
    upgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "upgrade",
        "head",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await upgrade.wait() == 0
    async with engine.connect() as connection:
        migrated = (
            await connection.execute(
                text(
                    "SELECT content, page_number, heading_path FROM chunks WHERE id = :id"
                ),
                {"id": chunk_id},
            )
        ).one()
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    await engine.dispose()
    assert migrated == ("legacy chunk", None, None)
    assert "generation_traces" in tables


@pytest.mark.integration
async def test_m4_schema_upgrades_to_m5_and_backfills_reconstructable_trace_ids(
    integration_settings: Settings,
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = integration_settings.database_url
    downgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "downgrade",
        "20260715_0004",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await downgrade.wait() == 0
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    retrieval_trace_id = uuid4()
    generation_trace_id = uuid4()
    engine = create_async_engine(integration_settings.database_url)
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, 'M5 Tenant', :slug)"),
            {"id": tenant_id, "slug": f"m5-{tenant_id}"},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name) "
                "VALUES (:id, :tenant, 'M5 KB')"
            ),
            {"id": knowledge_base_id, "tenant": tenant_id},
        )
        await connection.execute(
            text(
                "INSERT INTO retrieval_traces "
                "(id, tenant_id, knowledge_base_id, query, mode, top_k, candidate_k, "
                "rerank, retriever_version, embedding_version, duration_ms, candidates) "
                "VALUES (:id, :tenant, :kb, 'legacy trace', 'hybrid', 5, 20, true, "
                "'retriever-v1', 'embedding-v1', 1.5, '[]'::jsonb)"
            ),
            {"id": retrieval_trace_id, "tenant": tenant_id, "kb": knowledge_base_id},
        )
        await connection.execute(
            text(
                "INSERT INTO generation_traces "
                "(id, tenant_id, knowledge_base_id, retrieval_trace_id, query, "
                "rendered_prompt, answer, status, citations, provider, model, "
                "provider_config_version, prompt_version, retriever_version, "
                "embedding_version) VALUES "
                "(:id, :tenant, :kb, :retrieval, 'legacy trace', 'prompt', 'answer', "
                "'succeeded', '[]'::jsonb, 'deterministic', 'legacy-model', "
                "'provider-v1', 'prompt-v1', 'retriever-v1', 'embedding-v1')"
            ),
            {
                "id": generation_trace_id,
                "tenant": tenant_id,
                "kb": knowledge_base_id,
                "retrieval": retrieval_trace_id,
            },
        )
    upgrade = await asyncio.create_subprocess_exec(
        UV,
        "run",
        "alembic",
        "upgrade",
        "head",
        cwd=PROJECT_ROOT,
        env=env,
    )
    assert await upgrade.wait() == 0
    async with engine.connect() as connection:
        migrated = (
            await connection.execute(
                text(
                    "SELECT generation.answer, generation.trace_id, generation.span_id, "
                    "generation.request_id, generation.provider_attempts, retrieval.trace_id "
                    "AS retrieval_trace_id, retrieval.span_id AS retrieval_span_id "
                    "FROM generation_traces AS generation JOIN retrieval_traces AS retrieval "
                    "ON retrieval.id = generation.retrieval_trace_id "
                    "WHERE generation.id = :id"
                ),
                {"id": generation_trace_id},
            )
        ).one()
    await engine.dispose()
    assert migrated.answer == "answer"
    assert migrated.trace_id == migrated.retrieval_trace_id
    assert len(migrated.trace_id) == 32
    assert len(migrated.span_id) == len(migrated.retrieval_span_id) == 16
    assert migrated.request_id.startswith("migrated-")
    assert migrated.provider_attempts == 1
