from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.m2_helpers import auth_headers, seed_m2_identity, upload_document

import enterprise_rag_worker.enqueue_pending as enqueue_pending_module
from enterprise_rag_core.cleanup import OrphanCleanupService
from enterprise_rag_core.config import Settings
from enterprise_rag_core.dispatching import RecordingDispatcher, UnavailableDispatcher
from enterprise_rag_core.indexing import (
    DeterministicEmbeddingStub,
    IndexingPipeline,
    TransientIndexingError,
)
from enterprise_rag_core.models import (
    Chunk,
    Document,
    DocumentAcl,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    IndexJob,
    IndexJobStatus,
)
from enterprise_rag_core.storage import MinioObjectStorage


async def process_job(settings: Settings, task_id: str) -> None:
    result = await IndexingPipeline(settings).process(UUID(task_id))
    assert result.status is IndexJobStatus.SUCCEEDED


@pytest.mark.integration
async def test_same_idempotency_key_submitted_ten_times_creates_one_chunk_set(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
    recording_dispatcher: RecordingDispatcher,
) -> None:
    identity = await seed_m2_identity(db_session, "idempotency")

    responses = [
        await upload_document(
            api_client,
            identity,
            filename="policy.txt",
            content=b"A tenant policy with enough content to index once.",
            idempotency_key="same-key-ten-times",
        )
        for _ in range(10)
    ]

    assert {response.status_code for response in responses} == {202}
    task_ids = {response.json()["task_id"] for response in responses}
    document_ids = {response.json()["document_id"] for response in responses}
    assert len(task_ids) == 1
    assert len(document_ids) == 1
    dispatched_job_id = UUID(task_ids.pop())
    assert recording_dispatcher.job_ids
    assert set(recording_dispatcher.job_ids) == {dispatched_job_id}

    task_id = responses[0].json()["task_id"]
    await process_job(integration_settings, task_id)

    document_count = await db_session.scalar(select(func.count()).select_from(Document))
    version_count = await db_session.scalar(select(func.count()).select_from(DocumentVersion))
    chunk_count = await db_session.scalar(select(func.count()).select_from(Chunk))
    storage = MinioObjectStorage(integration_settings)
    keys = await storage.list_keys(f"tenants/{identity.tenant_id}/")
    assert document_count == 1
    assert version_count == 1
    assert chunk_count == 1
    assert len(keys) == 1


@pytest.mark.integration
async def test_update_only_changes_the_target_document(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "update")
    first = await upload_document(
        api_client,
        identity,
        filename="first.md",
        content=b"# First\n\nOriginal content",
        idempotency_key="upload-first",
    )
    second = await upload_document(
        api_client,
        identity,
        filename="second.txt",
        content=b"Second document remains untouched",
        idempotency_key="upload-second",
    )
    assert first.status_code == second.status_code == 202
    await process_job(integration_settings, first.json()["task_id"])
    await process_job(integration_settings, second.json()["task_id"])

    second_document_id = UUID(second.json()["document_id"])
    second_chunks_before = list(
        (
            await db_session.execute(
                select(Chunk.id, Chunk.content, Chunk.embedding).where(
                    Chunk.document_id == second_document_id
                )
            )
        ).all()
    )
    headers = await auth_headers(api_client, identity)
    headers["Idempotency-Key"] = "update-first-v2"
    updated = await api_client.put(
        f"/api/v1/documents/{first.json()['document_id']}",
        headers=headers,
        files={"file": ("first.md", b"# First\n\nUpdated content", "text/markdown")},
    )
    assert updated.status_code == 202
    await process_job(integration_settings, updated.json()["task_id"])
    db_session.expire_all()

    second_chunks_after = list(
        (
            await db_session.execute(
                select(Chunk.id, Chunk.content, Chunk.embedding).where(
                    Chunk.document_id == second_document_id
                )
            )
        ).all()
    )
    current_versions = await db_session.scalar(
        select(func.count())
        .select_from(DocumentVersion)
        .where(
            DocumentVersion.document_id == UUID(first.json()["document_id"]),
            DocumentVersion.is_current.is_(True),
        )
    )
    target_versions = await db_session.scalar(
        select(func.count())
        .select_from(DocumentVersion)
        .where(DocumentVersion.document_id == UUID(first.json()["document_id"]))
    )
    assert second_chunks_after == second_chunks_before
    assert target_versions == 2
    assert current_versions == 1


class FailOnceEmbedding:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            raise TransientIndexingError("embedding_timeout", "Embedding timed out")
        return DeterministicEmbeddingStub(dimensions=16).embed(texts)


class AlwaysFailEmbedding:
    def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise TransientIndexingError("embedding_unavailable", "Embedding is unavailable")


@pytest.mark.integration
async def test_transient_failure_backs_off_then_succeeds_without_duplicates(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "retry")
    response = await upload_document(
        api_client,
        identity,
        filename="retry.txt",
        content=b"Retryable indexing content",
        idempotency_key="retry-once",
    )
    task_id = UUID(response.json()["task_id"])
    provider = FailOnceEmbedding()
    first_result = await IndexingPipeline(integration_settings, embedding=provider).process(task_id)
    assert first_result.status is IndexJobStatus.PENDING
    job = await db_session.get(IndexJob, task_id)
    assert job is not None
    assert job.attempts == 1
    assert job.available_at > datetime.now(UTC)
    job.available_at = datetime.now(UTC)
    await db_session.commit()

    second_result = await IndexingPipeline(
        integration_settings,
        embedding=provider,
    ).process(task_id)
    chunk_count = await db_session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == job.document_id)
    )
    assert second_result.status is IndexJobStatus.SUCCEEDED
    assert chunk_count == 1


@pytest.mark.integration
async def test_retry_exhaustion_marks_job_version_and_initial_document_failed(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "retry-exhausted")
    response = await upload_document(
        api_client,
        identity,
        filename="retry-exhausted.txt",
        content=b"This embedding request always fails",
        idempotency_key="retry-exhausted",
    )
    task_id = UUID(response.json()["task_id"])
    settings = integration_settings.model_copy(update={"index_job_max_attempts": 2})

    first = await IndexingPipeline(settings, embedding=AlwaysFailEmbedding()).process(task_id)
    assert first.status is IndexJobStatus.PENDING
    job = await db_session.get(IndexJob, task_id)
    assert job is not None
    job.available_at = datetime.now(UTC)
    await db_session.commit()

    second = await IndexingPipeline(settings, embedding=AlwaysFailEmbedding()).process(task_id)
    db_session.expire_all()
    job = await db_session.get(IndexJob, task_id)
    version = await db_session.get(DocumentVersion, UUID(response.json()["version_id"]))
    document = await db_session.get(Document, UUID(response.json()["document_id"]))
    assert second.status is IndexJobStatus.FAILED
    assert job is not None and job.error_code == "retry_exhausted"
    assert job.attempts == 2
    assert version is not None and version.status is DocumentVersionStatus.FAILED
    assert document is not None and document.status is DocumentStatus.FAILED


@pytest.mark.integration
async def test_deterministic_parse_error_fails_without_retry(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "parse-error")
    response = await upload_document(
        api_client,
        identity,
        filename="invalid.txt",
        content=b"\xff\xfe",
        idempotency_key="invalid-utf8",
    )
    task_id = UUID(response.json()["task_id"])

    result = await IndexingPipeline(integration_settings).process(task_id)
    job = await db_session.get(IndexJob, task_id)
    chunk_count = await db_session.scalar(select(func.count()).select_from(Chunk))

    assert result.status is IndexJobStatus.FAILED
    assert job is not None
    assert job.attempts == 1
    assert job.error_code == "invalid_utf8"
    assert chunk_count == 0


@pytest.mark.integration
async def test_delete_removes_objects_chunks_vectors_acl_and_jobs(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "delete")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="delete.pdf",
        content=create_pdf_bytes("Delete this indexed policy"),
        idempotency_key="delete-upload",
    )
    await process_job(integration_settings, uploaded.json()["task_id"])
    document_id = UUID(uploaded.json()["document_id"])
    db_session.add(
        DocumentAcl(
            tenant_id=identity.tenant_id,
            document_id=document_id,
            user_id=identity.user_id,
        )
    )
    await db_session.commit()
    headers = await auth_headers(api_client, identity)

    deleted = await api_client.delete(f"/api/v1/documents/{document_id}", headers=headers)

    assert deleted.status_code == 204
    for model in (Document, DocumentVersion, Chunk, DocumentAcl, IndexJob):
        count = await db_session.scalar(select(func.count()).select_from(model))
        assert count == 0
    storage = MinioObjectStorage(integration_settings)
    assert await storage.list_keys(f"tenants/{identity.tenant_id}/") == []


def create_pdf_bytes(text: str) -> bytes:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)  # pyright: ignore[reportUnknownMemberType]
    content = document.tobytes()  # pyright: ignore[reportUnknownMemberType]
    document.close()
    return content


@pytest.mark.integration
async def test_rebuild_replaces_only_the_current_version_chunk_set(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "rebuild")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="rebuild.txt",
        content=b"Rebuild this exact current version",
        idempotency_key="rebuild-upload",
    )
    await process_job(integration_settings, uploaded.json()["task_id"])
    document_id = UUID(uploaded.json()["document_id"])
    chunk_ids_before = set(
        (
            await db_session.execute(select(Chunk.id).where(Chunk.document_id == document_id))
        ).scalars()
    )
    headers = await auth_headers(api_client, identity)
    headers["Idempotency-Key"] = "rebuild-current"

    rebuilt = await api_client.post(
        f"/api/v1/documents/{document_id}/rebuild",
        headers=headers,
    )
    repeated = await api_client.post(
        f"/api/v1/documents/{document_id}/rebuild",
        headers=headers,
    )
    assert rebuilt.status_code == repeated.status_code == 202
    assert rebuilt.json()["task_id"] == repeated.json()["task_id"]
    assert rebuilt.json()["version_id"] == uploaded.json()["version_id"]
    await process_job(integration_settings, rebuilt.json()["task_id"])
    db_session.expire_all()

    chunk_ids_after = set(
        (
            await db_session.execute(select(Chunk.id).where(Chunk.document_id == document_id))
        ).scalars()
    )
    version_count = await db_session.scalar(
        select(func.count())
        .select_from(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
    )
    assert version_count == 1
    assert len(chunk_ids_before) == len(chunk_ids_after) == 1
    assert chunk_ids_before != chunk_ids_after


@pytest.mark.integration
async def test_pending_job_can_be_cancelled_and_will_not_write_chunks(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "cancel")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="cancel.txt",
        content=b"This job will be cancelled before parsing",
        idempotency_key="cancel-upload",
    )
    headers = await auth_headers(api_client, identity)
    cancelled = await api_client.post(
        f"/api/v1/index-jobs/{uploaded.json()['task_id']}/cancel",
        headers=headers,
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    result = await IndexingPipeline(integration_settings).process(UUID(uploaded.json()["task_id"]))
    chunk_count = await db_session.scalar(select(func.count()).select_from(Chunk))
    db_session.expire_all()
    job = await db_session.get(IndexJob, UUID(uploaded.json()["task_id"]))
    version = await db_session.get(DocumentVersion, UUID(uploaded.json()["version_id"]))
    document = await db_session.get(Document, UUID(uploaded.json()["document_id"]))
    assert result.status is IndexJobStatus.CANCELLED
    assert chunk_count == 0
    assert job is not None and job.finished_at is not None
    assert version is not None and version.status is DocumentVersionStatus.FAILED
    assert document is not None and document.status is DocumentStatus.FAILED


@pytest.mark.integration
async def test_idempotency_key_reuse_with_different_content_is_rejected(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    identity = await seed_m2_identity(db_session, "idempotency-conflict")
    first = await upload_document(
        api_client,
        identity,
        filename="first.txt",
        content=b"first payload",
        idempotency_key="conflicting-key",
    )
    second = await upload_document(
        api_client,
        identity,
        filename="second.txt",
        content=b"different payload",
        idempotency_key="conflicting-key",
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_conflict"


@pytest.mark.integration
async def test_worker_startup_recovers_running_job_with_missing_lease(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = await seed_m2_identity(db_session, "startup-recovery")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="startup.txt",
        content=b"Recover a running job created before leases were introduced",
        idempotency_key="startup-recovery",
    )
    job_id = UUID(uploaded.json()["task_id"])
    job = await db_session.get(IndexJob, job_id)
    assert job is not None
    job.status = IndexJobStatus.RUNNING
    job.lease_until = None
    await db_session.commit()

    captured_job_ids: list[UUID] = []

    class CapturingDispatcher:

        def __init__(self, settings: Settings) -> None:
            del settings

        def enqueue(self, captured_job_id: UUID) -> None:
            captured_job_ids.append(captured_job_id)

    monkeypatch.setattr(
        enqueue_pending_module,
        "DramatiqDispatcher",
        CapturingDispatcher,
    )
    recovered_count = await enqueue_pending_module.enqueue_pending(integration_settings)
    db_session.expire_all()
    recovered = await db_session.get(IndexJob, job_id)

    assert recovered_count == 1
    assert captured_job_ids == [job_id]
    assert recovered is not None and recovered.status is IndexJobStatus.PENDING
    assert recovered.lease_until is None


@pytest.mark.integration
async def test_dispatch_outage_keeps_job_pending_and_recovery_reenqueues_once(
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from enterprise_rag_api.main import create_app

    app = create_app(integration_settings, dispatcher=UnavailableDispatcher())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            identity = await seed_m2_identity(db_session, "dispatch-outage")
            uploaded = await upload_document(
                client,
                identity,
                filename="outage.txt",
                content=b"Redis outage recovery keeps PostgreSQL authoritative",
                idempotency_key="dispatch-outage-upload",
            )
    assert uploaded.status_code == 202
    job_id = UUID(uploaded.json()["task_id"])
    job = await db_session.get(IndexJob, job_id)
    assert job is not None and job.status is IndexJobStatus.PENDING

    recovered_dispatcher = RecordingDispatcher()
    recovered_count = await enqueue_pending_module.enqueue_recoverable_jobs(
        integration_settings,
        recovered_dispatcher,
    )
    assert recovered_count == 1
    assert recovered_dispatcher.job_ids == [job_id]
    await process_job(integration_settings, str(job_id))
    db_session.expire_all()
    recovered_job = await db_session.get(IndexJob, job_id)
    chunk_count = await db_session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == job.document_id)
    )
    assert recovered_job is not None and recovered_job.status is IndexJobStatus.SUCCEEDED
    assert chunk_count == 1


@pytest.mark.integration
async def test_orphan_cleanup_preserves_referenced_objects_and_removes_only_orphans(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "orphan")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="referenced.txt",
        content=b"Referenced object",
        idempotency_key="orphan-reference",
    )
    await process_job(integration_settings, uploaded.json()["task_id"])
    version = await db_session.get(DocumentVersion, UUID(uploaded.json()["version_id"]))
    assert version is not None
    storage = MinioObjectStorage(integration_settings)
    orphan_key = f"tenants/{identity.tenant_id}/orphans/unreferenced.txt"
    orphan_content = b"orphan"
    import hashlib

    await storage.put_if_absent(
        orphan_key,
        orphan_content,
        checksum=hashlib.sha256(orphan_content).hexdigest(),
        content_type="text/plain",
    )

    dry_run = await OrphanCleanupService(integration_settings).run(dry_run=True)
    assert dry_run.orphaned == 1
    assert dry_run.removed == 0
    assert await storage.exists(orphan_key)

    cleaned = await OrphanCleanupService(integration_settings).run()
    assert cleaned.orphaned == 1
    assert cleaned.removed == 1
    assert not await storage.exists(orphan_key)
    assert await storage.exists(version.object_key)
