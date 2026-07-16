from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.m2_helpers import auth_headers, seed_m2_identity


@pytest.mark.security
@pytest.mark.parametrize("filename", ["../secret.txt", "..\\secret.txt", "bad\x00name.txt"])
async def test_malicious_upload_filename_is_rejected(
    api_client: AsyncClient,
    db_session: AsyncSession,
    filename: str,
) -> None:
    identity = await seed_m2_identity(db_session, "malicious")
    headers = await auth_headers(api_client, identity)
    headers["Idempotency-Key"] = "malicious-filename"

    response = await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/documents",
        headers=headers,
        files={"file": (filename, b"content", "text/plain")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_filename"


@pytest.mark.security
async def test_oversized_upload_is_rejected_before_storage(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: object,
) -> None:
    del integration_settings
    identity = await seed_m2_identity(db_session, "oversized")
    headers = await auth_headers(api_client, identity)
    headers["Idempotency-Key"] = "oversized-file"

    response = await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/documents",
        headers=headers,
        files={"file": ("large.txt", b"x" * (10 * 1024 * 1024 + 1), "text/plain")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"


@pytest.mark.security
async def test_document_and_job_endpoints_do_not_cross_tenant_boundaries(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    owner = await seed_m2_identity(db_session, "tenant-owner")
    other = await seed_m2_identity(db_session, "tenant-other")
    owner_headers = await auth_headers(api_client, owner)
    owner_headers["Idempotency-Key"] = "owner-upload"
    uploaded = await api_client.post(
        f"/api/v1/knowledge-bases/{owner.knowledge_base_id}/documents",
        headers=owner_headers,
        files={"file": ("private.txt", b"tenant-private content", "text/plain")},
    )
    assert uploaded.status_code == 202

    other_headers = await auth_headers(api_client, other)
    job_response = await api_client.get(
        f"/api/v1/index-jobs/{uploaded.json()['task_id']}",
        headers=other_headers,
    )
    other_headers["Idempotency-Key"] = "cross-tenant-update"
    update_response = await api_client.put(
        f"/api/v1/documents/{uploaded.json()['document_id']}",
        headers=other_headers,
        files={"file": ("stolen.txt", b"cross tenant update", "text/plain")},
    )
    delete_response = await api_client.delete(
        f"/api/v1/documents/{uploaded.json()['document_id']}",
        headers=other_headers,
    )
    other_headers["Idempotency-Key"] = "cross-tenant-upload"
    upload_response = await api_client.post(
        f"/api/v1/knowledge-bases/{owner.knowledge_base_id}/documents",
        headers=other_headers,
        files={"file": ("stolen.txt", b"cross tenant upload", "text/plain")},
    )

    for response in (job_response, update_response, delete_response, upload_response):
        assert response.status_code == 404
