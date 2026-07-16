from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, UploadFile, status

from enterprise_rag_api.dependencies import (
    BackgroundDispatcher,
    CurrentTenant,
    DatabaseSession,
    get_settings,
    require_roles,
)
from enterprise_rag_core.config import Settings
from enterprise_rag_core.documents import DocumentService, JobSubmission
from enterprise_rag_core.errors import PayloadTooLargeError, ValidationDomainError
from enterprise_rag_core.models import Role
from enterprise_rag_core.schemas import IndexJobResponse, JobSubmissionResponse
from enterprise_rag_core.services import TenantContext

router = APIRouter(prefix="/api/v1", tags=["documents"])
WriteTenant = Annotated[
    TenantContext,
    Depends(require_roles(Role.OWNER, Role.ADMIN)),
]


async def read_upload(file: UploadFile, settings: Settings) -> tuple[str, bytes]:
    if file.filename is None:
        raise ValidationDomainError(code="invalid_filename", message="Filename is required")
    content = await file.read(settings.max_upload_bytes + 1)
    await file.close()
    if len(content) > settings.max_upload_bytes:
        raise PayloadTooLargeError()
    return file.filename, content


def submission_response(submission: JobSubmission) -> JobSubmissionResponse:
    return JobSubmissionResponse.model_validate(submission, from_attributes=True)


@router.post(
    "/knowledge-bases/{knowledge_base_id}/documents",
    response_model=JobSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    knowledge_base_id: UUID,
    file: UploadFile,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    session: DatabaseSession,
    dispatcher: BackgroundDispatcher,
    context: WriteTenant,
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobSubmissionResponse:
    filename, content = await read_upload(file, settings)
    service = DocumentService(session, settings, dispatcher)
    submission = await service.upload(
        tenant_id=context.tenant_id,
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        content=content,
        idempotency_key=idempotency_key,
    )
    return submission_response(submission)


@router.put(
    "/documents/{document_id}",
    response_model=JobSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def update_document(
    document_id: UUID,
    file: UploadFile,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    session: DatabaseSession,
    dispatcher: BackgroundDispatcher,
    context: WriteTenant,
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobSubmissionResponse:
    filename, content = await read_upload(file, settings)
    submission = await DocumentService(session, settings, dispatcher).update(
        tenant_id=context.tenant_id,
        document_id=document_id,
        filename=filename,
        content=content,
        idempotency_key=idempotency_key,
    )
    return submission_response(submission)


@router.post(
    "/documents/{document_id}/rebuild",
    response_model=JobSubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rebuild_document(
    document_id: UUID,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    session: DatabaseSession,
    dispatcher: BackgroundDispatcher,
    context: WriteTenant,
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobSubmissionResponse:
    submission = await DocumentService(session, settings, dispatcher).rebuild(
        tenant_id=context.tenant_id,
        document_id=document_id,
        idempotency_key=idempotency_key,
    )
    return submission_response(submission)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    session: DatabaseSession,
    dispatcher: BackgroundDispatcher,
    context: WriteTenant,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await DocumentService(session, settings, dispatcher).delete(
        tenant_id=context.tenant_id,
        document_id=document_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/index-jobs/{job_id}", response_model=IndexJobResponse)
async def get_index_job(
    job_id: UUID,
    session: DatabaseSession,
    dispatcher: BackgroundDispatcher,
    context: CurrentTenant,
    settings: Annotated[Settings, Depends(get_settings)],
) -> IndexJobResponse:
    job = await DocumentService(session, settings, dispatcher).get_job(
        tenant_id=context.tenant_id,
        job_id=job_id,
    )
    return IndexJobResponse.model_validate(job)


@router.post("/index-jobs/{job_id}/cancel", response_model=IndexJobResponse)
async def cancel_index_job(
    job_id: UUID,
    session: DatabaseSession,
    dispatcher: BackgroundDispatcher,
    context: WriteTenant,
    settings: Annotated[Settings, Depends(get_settings)],
) -> IndexJobResponse:
    job = await DocumentService(session, settings, dispatcher).cancel_job(
        tenant_id=context.tenant_id,
        job_id=job_id,
    )
    return IndexJobResponse.model_validate(job)
