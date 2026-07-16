from __future__ import annotations

import asyncio
import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import pymupdf
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    IndexJob,
    IndexJobStage,
    IndexJobStatus,
)
from enterprise_rag_core.storage import MinioObjectStorage


class DeterministicIndexingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TransientIndexingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class JobCancelledError(Exception):
    pass


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicEmbeddingStub:
    """Explicit no-cost test/development stub; it is not a semantic production model."""

    def __init__(self, *, dimensions: int) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            values: list[float] = []
            for index in range(self.dimensions):
                digest = hashlib.sha256(f"{index}:{text}".encode()).digest()
                integer = int.from_bytes(digest[:4], "big")
                values.append((integer / 0xFFFFFFFF) * 2 - 1)
            magnitude = math.sqrt(sum(value * value for value in values)) or 1.0
            vectors.append([value / magnitude for value in values])
        return vectors


def clean_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def parse_document(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        try:
            parsed = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeterministicIndexingError("invalid_utf8", "Text files must be UTF-8") from exc
    elif suffix == ".pdf":
        try:
            document = pymupdf.open(stream=content, filetype="pdf")
            try:
                parsed = "\n\n".join(
                    cast(
                        str,
                        page.get_text("text"),  # pyright: ignore[reportUnknownMemberType]
                    )
                    for page in document
                )
            finally:
                document.close()
        except (RuntimeError, ValueError) as exc:
            raise DeterministicIndexingError("invalid_pdf", "PDF parsing failed") from exc
    else:
        raise DeterministicIndexingError(
            "unsupported_file_type",
            "Only PDF, TXT, and Markdown files are supported",
        )
    cleaned = clean_text(parsed)
    if not cleaned:
        raise DeterministicIndexingError("empty_document", "Document contains no indexable text")
    return cleaned


@dataclass(frozen=True)
class ChunkDraft:
    content: str
    page_number: int | None = None
    heading_path: str | None = None


def _markdown_sections(text: str) -> list[ChunkDraft]:
    headings: dict[int, str] = {}
    sections: list[ChunkDraft] = []
    body: list[str] = []

    def flush() -> None:
        cleaned = clean_text("\n".join(body))
        if cleaned:
            path = " > ".join(headings[level] for level in sorted(headings)) or None
            sections.append(ChunkDraft(cleaned, heading_path=path))
        body.clear()

    for line in clean_text(text).splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match is None:
            body.append(line)
            continue
        flush()
        level = len(match.group(1))
        headings[level] = match.group(2)
        for deeper in [existing for existing in headings if existing > level]:
            del headings[deeper]
    flush()
    if not sections and headings:
        path = " > ".join(headings[level] for level in sorted(headings))
        sections.append(ChunkDraft(headings[max(headings)], heading_path=path))
    return sections


def chunk_document(
    filename: str,
    content: bytes,
    *,
    chunk_size: int,
    overlap: int,
) -> list[ChunkDraft]:
    suffix = Path(filename).suffix.lower()
    sections: list[ChunkDraft]
    if suffix == ".pdf":
        try:
            document = pymupdf.open(stream=content, filetype="pdf")
            try:
                sections = []
                page_count = cast(
                    int,
                    document.page_count,  # pyright: ignore[reportUnknownMemberType]
                )
                for page_number in range(1, page_count + 1):
                    page = document.load_page(  # pyright: ignore[reportUnknownMemberType]
                        page_number - 1
                    )
                    page_text = clean_text(
                        cast(
                            str,
                            page.get_text("text"),  # pyright: ignore[reportUnknownMemberType]
                        )
                    )
                    if page_text:
                        sections.append(ChunkDraft(page_text, page_number=page_number))
            finally:
                document.close()
        except (RuntimeError, ValueError) as exc:
            raise DeterministicIndexingError("invalid_pdf", "PDF parsing failed") from exc
    elif suffix in {".txt", ".md"}:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeterministicIndexingError("invalid_utf8", "Text files must be UTF-8") from exc
        cleaned = clean_text(decoded)
        sections = _markdown_sections(cleaned) if suffix == ".md" else [ChunkDraft(cleaned)]
    else:
        raise DeterministicIndexingError(
            "unsupported_file_type",
            "Only PDF, TXT, and Markdown files are supported",
        )
    if not sections:
        raise DeterministicIndexingError("empty_document", "Document contains no indexable text")
    return [
        ChunkDraft(part, section.page_number, section.heading_path)
        for section in sections
        for part in chunk_text(section.content, chunk_size=chunk_size, overlap=overlap)
    ]


def chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size must be positive and overlap smaller than chunk_size")
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def calculate_retry_delay(attempt: int, *, base_seconds: int, max_seconds: int) -> int:
    return min(base_seconds * (2 ** max(attempt - 1, 0)), max_seconds)


@dataclass(frozen=True)
class IndexingResult:
    status: IndexJobStatus
    retry_delay_seconds: int | None = None


class IndexingPipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        embedding: EmbeddingProvider | None = None,
    ) -> None:
        self.settings = settings
        self.embedding = embedding or DeterministicEmbeddingStub(
            dimensions=settings.embedding_dimensions
        )
        self.storage = MinioObjectStorage(settings)
        self.engine, self.session_factory = create_database_resources(settings.database_url)

    async def process(self, job_id: UUID) -> IndexingResult:
        try:
            claimed = await self._claim(job_id)
            if claimed is not None:
                return claimed
            job, version = await self._load_job_and_version(job_id)
            content = await self.storage.read(version.object_key)
            await self._fault_pause("parse")
            chunks = chunk_document(
                version.filename,
                content,
                chunk_size=self.settings.chunk_size_chars,
                overlap=self.settings.chunk_overlap_chars,
            )
            await self._set_stage(job_id, IndexJobStage.EMBEDDING)
            await self._fault_pause("embedding")
            embeddings = await asyncio.to_thread(
                self.embedding.embed, [chunk.content for chunk in chunks]
            )
            if len(embeddings) != len(chunks):
                raise TransientIndexingError(
                    "embedding_count_mismatch",
                    "Embedding provider returned an unexpected vector count",
                )
            await self._set_stage(job_id, IndexJobStage.DATABASE_WRITE)
            await self._commit_chunks(job, version, chunks, embeddings)
            return IndexingResult(IndexJobStatus.SUCCEEDED)
        except JobCancelledError:
            return IndexingResult(IndexJobStatus.CANCELLED)
        except DeterministicIndexingError as exc:
            await self._mark_terminal_failure(job_id, exc.code, exc.message)
            return IndexingResult(IndexJobStatus.FAILED)
        except TransientIndexingError as exc:
            return await self._mark_transient_failure(job_id, exc.code, exc.message)
        except Exception as exc:
            return await self._mark_transient_failure(
                job_id,
                "unexpected_indexing_error",
                str(exc)[:2000],
            )
        finally:
            await self.engine.dispose()

    async def _claim(self, job_id: UUID) -> IndexingResult | None:
        async with self.session_factory() as session, session.begin():
            now = cast(datetime, await session.scalar(select(func.now())))
            statement = select(IndexJob).where(IndexJob.id == job_id).with_for_update()
            job = (await session.execute(statement)).scalar_one_or_none()
            if job is None:
                raise DeterministicIndexingError("job_not_found", "Index job does not exist")
            if job.status in {
                IndexJobStatus.SUCCEEDED,
                IndexJobStatus.FAILED,
                IndexJobStatus.CANCELLED,
            }:
                return IndexingResult(job.status)
            if job.status is IndexJobStatus.RUNNING and job.lease_until and job.lease_until > now:
                return IndexingResult(IndexJobStatus.RUNNING)
            if job.available_at > now:
                retry_after = max(1, math.ceil((job.available_at - now).total_seconds()))
                return IndexingResult(IndexJobStatus.PENDING, retry_after)
            job.status = IndexJobStatus.RUNNING
            job.stage = IndexJobStage.PARSE
            job.attempts += 1
            job.started_at = now
            job.lease_until = now + timedelta(seconds=self.settings.index_job_lease_seconds)
            job.error_code = None
            job.error_message = None
        return None

    async def _load_job_and_version(self, job_id: UUID) -> tuple[IndexJob, DocumentVersion]:
        async with self.session_factory() as session:
            job = await session.get(IndexJob, job_id)
            if job is None:
                raise DeterministicIndexingError("job_not_found", "Index job does not exist")
            version = await session.get(DocumentVersion, job.version_id)
            if version is None or version.tenant_id != job.tenant_id:
                raise DeterministicIndexingError(
                    "version_not_found",
                    "Document version does not exist in the job tenant",
                )
            return job, version

    async def _set_stage(self, job_id: UUID, stage: IndexJobStage) -> None:
        async with self.session_factory() as session, session.begin():
            job = await session.get(IndexJob, job_id, with_for_update=True)
            if job is not None and job.status is IndexJobStatus.CANCELLED:
                raise JobCancelledError
            if job is None or job.status is not IndexJobStatus.RUNNING:
                raise TransientIndexingError("job_lease_lost", "Index job lease was lost")
            job.stage = stage
            job.lease_until = datetime.now(UTC) + timedelta(
                seconds=self.settings.index_job_lease_seconds
            )

    async def _commit_chunks(
        self,
        detached_job: IndexJob,
        detached_version: DocumentVersion,
        chunks: list[ChunkDraft],
        embeddings: list[list[float]],
    ) -> None:
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            job = await session.get(IndexJob, detached_job.id, with_for_update=True)
            version = await session.get(
                DocumentVersion,
                detached_version.id,
                with_for_update=True,
            )
            document = await session.get(Document, detached_job.document_id, with_for_update=True)
            if job is None or version is None or document is None:
                raise TransientIndexingError("index_state_missing", "Indexing state disappeared")
            if job.status is IndexJobStatus.CANCELLED:
                raise JobCancelledError
            if job.status is not IndexJobStatus.RUNNING:
                raise TransientIndexingError("job_lease_lost", "Index job lease was lost")
            await session.execute(delete(Chunk).where(Chunk.version_id == version.id))
            for ordinal, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
                session.add(
                    Chunk(
                        tenant_id=job.tenant_id,
                        document_id=job.document_id,
                        version_id=version.id,
                        ordinal=ordinal,
                        content=chunk.content,
                        page_number=chunk.page_number,
                        heading_path=chunk.heading_path,
                        content_checksum=hashlib.sha256(chunk.content.encode()).hexdigest(),
                        embedding=embedding,
                    )
                )
            await session.flush()
            await self._fault_pause("database_write")
            await session.execute(
                update(DocumentVersion)
                .where(
                    DocumentVersion.tenant_id == job.tenant_id,
                    DocumentVersion.document_id == job.document_id,
                    DocumentVersion.id != version.id,
                    DocumentVersion.is_current.is_(True),
                )
                .values(is_current=False, status=DocumentVersionStatus.SUPERSEDED)
            )
            version.is_current = True
            version.status = DocumentVersionStatus.READY
            document.filename = version.filename
            document.object_key = version.object_key
            document.checksum = version.checksum
            document.status = DocumentStatus.READY
            job.status = IndexJobStatus.SUCCEEDED
            job.stage = IndexJobStage.COMPLETE
            job.lease_until = None
            job.finished_at = now

    async def _mark_terminal_failure(self, job_id: UUID, code: str, message: str) -> None:
        async with self.session_factory() as session, session.begin():
            job = await session.get(IndexJob, job_id, with_for_update=True)
            if job is None or job.status is IndexJobStatus.SUCCEEDED:
                return
            job.status = IndexJobStatus.FAILED
            job.error_code = code
            job.error_message = message[:2000]
            job.lease_until = None
            job.finished_at = datetime.now(UTC)
            await self._mark_related_state_failed(session, job)

    async def _mark_transient_failure(
        self,
        job_id: UUID,
        code: str,
        message: str,
    ) -> IndexingResult:
        async with self.session_factory() as session, session.begin():
            job = await session.get(IndexJob, job_id, with_for_update=True)
            if job is None:
                return IndexingResult(IndexJobStatus.FAILED)
            if job.status in {
                IndexJobStatus.SUCCEEDED,
                IndexJobStatus.FAILED,
                IndexJobStatus.CANCELLED,
            }:
                return IndexingResult(job.status)
            if job.attempts >= self.settings.index_job_max_attempts:
                job.status = IndexJobStatus.FAILED
                job.error_code = "retry_exhausted"
                job.error_message = message[:2000]
                job.lease_until = None
                job.finished_at = datetime.now(UTC)
                await self._mark_related_state_failed(session, job)
                return IndexingResult(IndexJobStatus.FAILED)
            delay = calculate_retry_delay(
                job.attempts,
                base_seconds=self.settings.index_retry_base_seconds,
                max_seconds=self.settings.index_retry_max_seconds,
            )
            job.status = IndexJobStatus.PENDING
            job.error_code = code
            job.error_message = message[:2000]
            job.lease_until = None
            job.available_at = datetime.now(UTC) + timedelta(seconds=delay)
            return IndexingResult(IndexJobStatus.PENDING, delay)

    @staticmethod
    async def _mark_related_state_failed(session: AsyncSession, job: IndexJob) -> None:
        version = await session.get(DocumentVersion, job.version_id)
        if version is not None and not version.is_current:
            version.status = DocumentVersionStatus.FAILED
        document = await session.get(Document, job.document_id)
        current = (
            await session.execute(
                select(DocumentVersion.id).where(
                    DocumentVersion.tenant_id == job.tenant_id,
                    DocumentVersion.document_id == job.document_id,
                    DocumentVersion.is_current.is_(True),
                    DocumentVersion.status == DocumentVersionStatus.READY,
                )
            )
        ).scalar_one_or_none()
        if document is not None:
            document.status = DocumentStatus.READY if current is not None else DocumentStatus.FAILED

    async def _fault_pause(self, stage: str) -> None:
        if self.settings.indexing_fault_pause_stage != stage:
            return
        signal_path = self.settings.indexing_fault_signal_path
        if not signal_path:
            raise RuntimeError("INDEXING_FAULT_SIGNAL_PATH is required for fault injection")
        await asyncio.to_thread(Path(signal_path).write_text, stage, encoding="utf-8")
        await asyncio.sleep(60)
