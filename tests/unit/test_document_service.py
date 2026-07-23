from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core import documents as documents_module
from enterprise_rag_core.dispatching import JobDispatcher
from enterprise_rag_core.documents import DocumentService, JobSubmission
from enterprise_rag_core.errors import ConflictError, NotFoundError, ValidationDomainError
from enterprise_rag_core.models import IndexJobStatus


class QueryResult:
    def __init__(self, scalar: object = None, scalars: list[object] | None = None) -> None:
        self.scalar = scalar
        self.scalar_values = scalars or []

    def scalar_one_or_none(self) -> object:
        return self.scalar

    def scalars(self) -> list[object]:
        return self.scalar_values


class FakeStorage:
    def __init__(self) -> None:
        self.put_if_absent = AsyncMock()
        self.remove_many = AsyncMock()


class FakeSession:
    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.scalar = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.delete = AsyncMock()
        self.refresh = AsyncMock()
        self.get = AsyncMock()
        self.add = Mock()
        self.add_all = Mock()


def build_service(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DocumentService, FakeSession, FakeStorage, Mock]:
    storage = FakeStorage()
    session = FakeSession()
    dispatcher = Mock()

    def build_storage(settings: object) -> FakeStorage:
        del settings
        return storage

    monkeypatch.setattr(documents_module, "MinioObjectStorage", build_storage)
    service = DocumentService(
        cast(AsyncSession, session),
        object(),  # type: ignore[arg-type]
        cast(JobDispatcher, dispatcher),
    )
    return service, session, storage, dispatcher


@pytest.mark.parametrize("filename", ["policy.docx", "archive", "policy.exe"])
def test_filename_rejects_unsupported_types(filename: str) -> None:
    with pytest.raises(ValidationDomainError, match="Only PDF, TXT, and Markdown"):
        documents_module.validate_filename(filename)


@pytest.mark.parametrize("value", ["", " ", "x" * 201])
def test_idempotency_key_rejects_empty_or_oversized_values(value: str) -> None:
    with pytest.raises(ValidationDomainError, match="between 1 and 200"):
        documents_module.validate_idempotency_key(value)


@pytest.mark.asyncio
async def test_upload_persists_object_job_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, storage, dispatcher = build_service(monkeypatch)
    tenant_id = uuid4()
    knowledge_base_id = uuid4()
    session.execute.return_value = QueryResult(SimpleNamespace(id=knowledge_base_id))
    service._existing_submission = AsyncMock(return_value=None)  # type: ignore[method-assign]

    submission = await service.upload(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        filename="policy.md",
        content=b"# Policy",
        idempotency_key=" upload-policy ",
    )

    assert submission.status is IndexJobStatus.PENDING
    storage.put_if_absent.assert_awaited_once()
    session.add_all.assert_called_once()
    session.commit.assert_awaited_once()
    dispatcher.enqueue.assert_called_once_with(submission.task_id)


@pytest.mark.asyncio
async def test_upload_reuses_existing_submission_and_rejects_missing_knowledge_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, _storage, dispatcher = build_service(monkeypatch)
    existing = JobSubmission(uuid4(), uuid4(), uuid4(), IndexJobStatus.PENDING)
    service._existing_submission = AsyncMock(return_value=existing)  # type: ignore[method-assign]

    reused = await service.upload(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        filename="policy.txt",
        content=b"policy",
        idempotency_key="same-upload",
    )
    assert reused == existing
    dispatcher.enqueue.assert_called_once_with(existing.task_id)

    service._existing_submission = AsyncMock(return_value=None)  # type: ignore[method-assign]
    session.execute.return_value = QueryResult()
    with pytest.raises(NotFoundError, match="Knowledge base not found"):
        await service.upload(
            tenant_id=uuid4(),
            knowledge_base_id=uuid4(),
            filename="missing.txt",
            content=b"missing",
            idempotency_key="missing-kb",
        )


@pytest.mark.asyncio
async def test_upload_recovers_from_idempotency_commit_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, _storage, dispatcher = build_service(monkeypatch)
    existing = JobSubmission(uuid4(), uuid4(), uuid4(), IndexJobStatus.PENDING)
    service._existing_submission = AsyncMock(  # type: ignore[method-assign]
        side_effect=[None, existing]
    )
    session.execute.return_value = QueryResult(SimpleNamespace(id=uuid4()))
    session.commit.side_effect = IntegrityError("insert", {}, RuntimeError("duplicate"))

    submission = await service.upload(
        tenant_id=uuid4(),
        knowledge_base_id=uuid4(),
        filename="race.txt",
        content=b"race",
        idempotency_key="raced-upload",
    )

    assert submission == existing
    session.rollback.assert_awaited_once()
    dispatcher.enqueue.assert_called_once_with(existing.task_id)


@pytest.mark.asyncio
async def test_update_creates_next_version_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    service, session, storage, dispatcher = build_service(monkeypatch)
    document = SimpleNamespace(status=None)
    service._get_document = AsyncMock(return_value=document)  # type: ignore[method-assign]
    service._existing_submission = AsyncMock(return_value=None)  # type: ignore[method-assign]
    session.scalar.return_value = 2

    submission = await service.update(
        tenant_id=uuid4(),
        document_id=uuid4(),
        filename="policy-v3.pdf",
        content=b"%PDF version three",
        idempotency_key="update-v3",
    )

    assert submission.status is IndexJobStatus.PENDING
    storage.put_if_absent.assert_awaited_once()
    session.add_all.assert_called_once()
    session.commit.assert_awaited_once()
    dispatcher.enqueue.assert_called_once_with(submission.task_id)


@pytest.mark.asyncio
async def test_rebuild_requires_current_version_and_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, _storage, dispatcher = build_service(monkeypatch)
    service._get_document = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]
    session.execute.return_value = QueryResult()

    with pytest.raises(ConflictError, match="no successfully indexed version"):
        await service.rebuild(
            tenant_id=uuid4(),
            document_id=uuid4(),
            idempotency_key="missing-current",
        )

    version = SimpleNamespace(id=uuid4(), checksum="abc")
    session.execute.return_value = QueryResult(version)
    service._existing_submission = AsyncMock(return_value=None)  # type: ignore[method-assign]
    submission = await service.rebuild(
        tenant_id=uuid4(),
        document_id=uuid4(),
        idempotency_key="rebuild-current",
    )

    assert submission.status is IndexJobStatus.PENDING
    session.add.assert_called_once()
    session.commit.assert_awaited_once()
    dispatcher.enqueue.assert_called_once_with(submission.task_id)


@pytest.mark.asyncio
async def test_delete_commits_before_deduplicated_object_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session, storage, _dispatcher = build_service(monkeypatch)
    document = SimpleNamespace()
    service._get_document = AsyncMock(return_value=document)  # type: ignore[method-assign]
    session.execute.return_value = QueryResult(scalars=["one", "one", "two"])

    await service.delete(tenant_id=uuid4(), document_id=uuid4())

    session.delete.assert_awaited_once_with(document)
    session.commit.assert_awaited_once()
    storage.remove_many.assert_awaited_once_with(["one", "two"])
