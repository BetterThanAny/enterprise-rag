from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core.config import Settings
from enterprise_rag_core.dispatching import JobDispatcher
from enterprise_rag_core.errors import ConflictError, NotFoundError, ValidationDomainError
from enterprise_rag_core.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    IndexJob,
    IndexJobAction,
    IndexJobStage,
    IndexJobStatus,
    KnowledgeBase,
)
from enterprise_rag_core.storage import MinioObjectStorage

logger = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md"}


@dataclass(frozen=True)
class JobSubmission:
    task_id: UUID
    document_id: UUID
    version_id: UUID
    status: IndexJobStatus


def validate_filename(filename: str) -> str:
    if (
        not filename
        or "\x00" in filename
        or "%00" in filename.casefold()
        or any(ord(character) < 32 for character in filename)
        or PurePosixPath(filename).name != filename
        or PureWindowsPath(filename).name != filename
    ):
        raise ValidationDomainError(
            code="invalid_filename",
            message="Filename must not contain path components or null bytes",
        )
    if PurePosixPath(filename).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValidationDomainError(
            code="unsupported_file_type",
            message="Only PDF, TXT, and Markdown files are supported",
        )
    return filename


def validate_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValidationDomainError(
            code="invalid_idempotency_key",
            message="Idempotency-Key must contain between 1 and 200 characters",
        )
    return normalized


class DocumentService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        dispatcher: JobDispatcher,
    ) -> None:
        self.session = session
        self.settings = settings
        self.dispatcher = dispatcher
        self.storage = MinioObjectStorage(settings)

    async def upload(
        self,
        *,
        tenant_id: UUID,
        knowledge_base_id: UUID,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> JobSubmission:
        filename = validate_filename(filename)
        idempotency_key = validate_idempotency_key(idempotency_key)
        checksum = hashlib.sha256(content).hexdigest()
        existing = await self._existing_submission(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            checksum=checksum,
            action=IndexJobAction.INDEX,
        )
        if existing is not None:
            self._dispatch(existing.task_id)
            return existing
        knowledge_base = (
            await self.session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.id == knowledge_base_id,
                    KnowledgeBase.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if knowledge_base is None:
            raise NotFoundError(
                code="knowledge_base_not_found",
                message="Knowledge base not found",
            )
        document_id = self._stable_id(tenant_id, idempotency_key, "document")
        version_id = self._stable_id(tenant_id, idempotency_key, "version")
        job_id = self._stable_id(tenant_id, idempotency_key, "job")
        object_key = self._object_key(
            tenant_id,
            document_id,
            1,
            checksum,
            filename,
        )
        await self.storage.put_if_absent(
            object_key,
            content,
            checksum=checksum,
            content_type=self._content_type(filename),
        )
        document = Document(
            id=document_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            filename=filename,
            object_key=object_key,
            checksum=checksum,
            status=DocumentStatus.PENDING,
        )
        version = DocumentVersion(
            id=version_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_number=1,
            filename=filename,
            object_key=object_key,
            checksum=checksum,
            status=DocumentVersionStatus.PENDING,
            is_current=False,
        )
        job = self._new_job(
            job_id=job_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
            checksum=checksum,
            action=IndexJobAction.INDEX,
        )
        self.session.add_all([document, version, job])
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._existing_submission(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                checksum=checksum,
                action=IndexJobAction.INDEX,
            )
            if existing is None:
                raise
            self._dispatch(existing.task_id)
            return existing
        submission = self._submission(job)
        self._dispatch(job.id)
        return submission

    async def update(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        filename: str,
        content: bytes,
        idempotency_key: str,
    ) -> JobSubmission:
        filename = validate_filename(filename)
        idempotency_key = validate_idempotency_key(idempotency_key)
        checksum = hashlib.sha256(content).hexdigest()
        document = await self._get_document(tenant_id, document_id, for_update=True)
        existing = await self._existing_submission(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            checksum=checksum,
            action=IndexJobAction.INDEX,
            document_id=document_id,
        )
        if existing is not None:
            self._dispatch(existing.task_id)
            return existing
        max_version = await self.session.scalar(
            select(func.max(DocumentVersion.version_number)).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.document_id == document_id,
            )
        )
        version_number = (max_version or 0) + 1
        version_id = self._stable_id(tenant_id, idempotency_key, "version")
        job_id = self._stable_id(tenant_id, idempotency_key, "job")
        object_key = self._object_key(
            tenant_id,
            document_id,
            version_number,
            checksum,
            filename,
        )
        await self.storage.put_if_absent(
            object_key,
            content,
            checksum=checksum,
            content_type=self._content_type(filename),
        )
        version = DocumentVersion(
            id=version_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_number=version_number,
            filename=filename,
            object_key=object_key,
            checksum=checksum,
            status=DocumentVersionStatus.PENDING,
            is_current=False,
        )
        job = self._new_job(
            job_id=job_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
            checksum=checksum,
            action=IndexJobAction.INDEX,
        )
        document.status = DocumentStatus.PENDING
        self.session.add_all([version, job])
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._existing_submission(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                checksum=checksum,
                action=IndexJobAction.INDEX,
                document_id=document_id,
            )
            if existing is None:
                raise
            self._dispatch(existing.task_id)
            return existing
        submission = self._submission(job)
        self._dispatch(job.id)
        return submission

    async def rebuild(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        idempotency_key: str,
    ) -> JobSubmission:
        idempotency_key = validate_idempotency_key(idempotency_key)
        await self._get_document(tenant_id, document_id)
        version = (
            await self.session.execute(
                select(DocumentVersion).where(
                    DocumentVersion.tenant_id == tenant_id,
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.is_current.is_(True),
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise ConflictError(
                code="document_has_no_current_version",
                message="Document has no successfully indexed version to rebuild",
            )
        existing = await self._existing_submission(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            checksum=version.checksum,
            action=IndexJobAction.REBUILD,
            document_id=document_id,
        )
        if existing is not None:
            self._dispatch(existing.task_id)
            return existing
        job = self._new_job(
            job_id=self._stable_id(tenant_id, idempotency_key, "job"),
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version.id,
            idempotency_key=idempotency_key,
            checksum=version.checksum,
            action=IndexJobAction.REBUILD,
        )
        self.session.add(job)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            existing = await self._existing_submission(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                checksum=version.checksum,
                action=IndexJobAction.REBUILD,
                document_id=document_id,
            )
            if existing is None:
                raise
            self._dispatch(existing.task_id)
            return existing
        submission = self._submission(job)
        self._dispatch(job.id)
        return submission

    async def delete(self, *, tenant_id: UUID, document_id: UUID) -> None:
        document = await self._get_document(tenant_id, document_id, for_update=True)
        keys = list(
            (
                await self.session.execute(
                    select(DocumentVersion.object_key).where(
                        DocumentVersion.tenant_id == tenant_id,
                        DocumentVersion.document_id == document_id,
                    )
                )
            ).scalars()
        )
        await self.session.delete(document)
        await self.session.commit()
        # Database state is authoritative. If the process dies during object cleanup,
        # the cleanup command can safely discover and remove the remaining orphans.
        await self.storage.remove_many(list(dict.fromkeys(keys)))

    async def get_job(self, *, tenant_id: UUID, job_id: UUID) -> IndexJob:
        job = (
            await self.session.execute(
                select(IndexJob).where(IndexJob.id == job_id, IndexJob.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if job is None:
            raise NotFoundError(code="index_job_not_found", message="Index job not found")
        return job

    async def cancel_job(self, *, tenant_id: UUID, job_id: UUID) -> IndexJob:
        job = (
            await self.session.execute(
                select(IndexJob)
                .where(IndexJob.id == job_id, IndexJob.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            raise NotFoundError(code="index_job_not_found", message="Index job not found")
        if job.status in {IndexJobStatus.PENDING, IndexJobStatus.RUNNING}:
            job.status = IndexJobStatus.CANCELLED
            job.error_code = "cancelled_by_user"
            job.error_message = "Index job was cancelled by the user"
            job.lease_until = None
            job.finished_at = datetime.now(UTC)
            version = await self.session.get(DocumentVersion, job.version_id)
            if version is not None and not version.is_current:
                version.status = DocumentVersionStatus.FAILED
            document = await self.session.get(Document, job.document_id)
            current = (
                await self.session.execute(
                    select(DocumentVersion.id).where(
                        DocumentVersion.tenant_id == tenant_id,
                        DocumentVersion.document_id == job.document_id,
                        DocumentVersion.is_current.is_(True),
                        DocumentVersion.status == DocumentVersionStatus.READY,
                    )
                )
            ).scalar_one_or_none()
            if document is not None:
                document.status = (
                    DocumentStatus.READY if current is not None else DocumentStatus.FAILED
                )
            await self.session.commit()
            await self.session.refresh(job)
        return job

    async def _get_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        for_update: bool = False,
    ) -> Document:
        statement = select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
        )
        if for_update:
            statement = statement.with_for_update()
        document = (await self.session.execute(statement)).scalar_one_or_none()
        if document is None:
            raise NotFoundError(code="document_not_found", message="Document not found")
        return document

    async def _existing_submission(
        self,
        *,
        tenant_id: UUID,
        idempotency_key: str,
        checksum: str,
        action: IndexJobAction,
        document_id: UUID | None = None,
    ) -> JobSubmission | None:
        job = (
            await self.session.execute(
                select(IndexJob).where(
                    IndexJob.tenant_id == tenant_id,
                    IndexJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        if (
            job.payload_checksum != checksum
            or job.action is not action
            or (document_id is not None and job.document_id != document_id)
        ):
            raise ConflictError(
                code="idempotency_key_conflict",
                message="Idempotency-Key was already used for a different operation",
            )
        return self._submission(job)

    def _dispatch(self, job_id: UUID) -> None:
        try:
            self.dispatcher.enqueue(job_id)
        except Exception:
            logger.exception("index_job_dispatch_failed", extra={"job_id": str(job_id)})

    @staticmethod
    def _new_job(
        *,
        job_id: UUID,
        tenant_id: UUID,
        document_id: UUID,
        version_id: UUID,
        idempotency_key: str,
        checksum: str,
        action: IndexJobAction,
    ) -> IndexJob:
        return IndexJob(
            id=job_id,
            tenant_id=tenant_id,
            document_id=document_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
            payload_checksum=checksum,
            action=action,
            status=IndexJobStatus.PENDING,
            stage=IndexJobStage.QUEUED,
            attempts=0,
        )

    @staticmethod
    def _stable_id(tenant_id: UUID, idempotency_key: str, kind: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"enterprise-rag:{tenant_id}:{idempotency_key}:{kind}")

    @staticmethod
    def _object_key(
        tenant_id: UUID,
        document_id: UUID,
        version_number: int,
        checksum: str,
        filename: str,
    ) -> str:
        return (
            f"tenants/{tenant_id}/documents/{document_id}/versions/"
            f"{version_number}/{checksum}-{filename}"
        )

    @staticmethod
    def _content_type(filename: str) -> str:
        suffix = PurePosixPath(filename).suffix.lower()
        return {
            ".pdf": "application/pdf",
            ".md": "text/markdown; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
        }[suffix]

    @staticmethod
    def _submission(job: IndexJob) -> JobSubmission:
        return JobSubmission(
            task_id=job.id,
            document_id=job.document_id,
            version_id=job.version_id,
            status=job.status,
        )
