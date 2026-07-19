from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select

from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.models import Membership, Role, Tenant, User
from enterprise_rag_core.security import hash_password


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for the smoke test")
    return value


def http_timeout(default: float = 10) -> float:
    return float(os.environ.get("SMOKE_HTTP_TIMEOUT_SECONDS", str(default)))


async def seed_smoke_identity(database_url: str, password: str) -> str:
    engine, session_factory = create_database_resources(database_url)
    async with session_factory() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "smoke-tenant"))
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(name="Smoke Tenant", slug="smoke-tenant")
            session.add(tenant)
            await session.flush()

        email = "smoke-user@example.com"
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(email=email, password_hash=hash_password(password), is_active=True)
            session.add(user)
            await session.flush()
        else:
            user.password_hash = hash_password(password)
            user.is_active = True

        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.tenant_id == tenant.id,
                    Membership.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=Role.OWNER))
        else:
            membership.role = Role.OWNER
        await session.commit()
        tenant_id = str(tenant.id)
    await engine.dispose()
    return tenant_id


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
    file: tuple[str, str, bytes, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise RuntimeError("Smoke test only permits HTTP(S) URLs")
    request_headers = dict(headers or {})
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode()
        request_headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    if file is not None:
        field_name, filename, content, content_type = file
        boundary = f"enterprise-rag-{secrets.token_hex(16)}"
        data = b"".join(
            (
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            )
        )
        request_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(  # noqa: S310 -- scheme is restricted above
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=http_timeout()) as response:  # noqa: S310
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


def request_sse(
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str],
) -> tuple[int, str]:
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise RuntimeError("Smoke test only permits HTTP(S) URLs")
    request = urllib.request.Request(  # noqa: S310 -- scheme is restricted above
        url,
        data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=http_timeout(30)) as response:  # noqa: S310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, cast(dict[str, Any], json.loads(data))))
    return events


def request_text(url: str) -> tuple[int, str]:
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise RuntimeError("Smoke test only permits HTTP(S) URLs")
    request = urllib.request.Request(url, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=http_timeout()) as response:  # noqa: S310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def wait_for_job(api_url: str, task_id: str, headers: dict[str, str]) -> dict[str, Any]:
    timeout_seconds = float(os.environ.get("SMOKE_JOB_TIMEOUT_SECONDS", "30"))
    deadline = time.monotonic() + timeout_seconds
    last_job: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status, job = request_json(
            f"{api_url}/api/v1/index-jobs/{task_id}",
            headers=headers,
        )
        if status != 200 or not isinstance(job, dict):
            raise RuntimeError("job status lookup failed")
        typed_job = cast(dict[str, Any], job)
        last_job = typed_job
        if typed_job["status"] in {"succeeded", "failed", "cancelled"}:
            return typed_job
        time.sleep(0.2)
    raise RuntimeError(f"index job did not finish before timeout: {last_job}")


def main() -> None:
    database_url = required_env("DATABASE_URL")
    api_url = os.environ.get("API_URL", "http://localhost:18000").rstrip("/")
    password = secrets.token_urlsafe(32)
    tenant_id = asyncio.run(seed_smoke_identity(database_url, password))

    ready_status, ready = request_json(f"{api_url}/health/ready")
    login_status, login = request_json(
        f"{api_url}/api/v1/auth/login",
        method="POST",
        form={"username": "smoke-user@example.com", "password": password},
    )
    token = str(login["access_token"])
    headers = {"Authorization": f"Bearer {token}", "X-Tenant-ID": tenant_id}
    name = f"Smoke {uuid4()}"
    create_status, created = request_json(
        f"{api_url}/api/v1/knowledge-bases",
        method="POST",
        body={"name": name},
        headers=headers,
    )
    list_status, knowledge_bases = request_json(
        f"{api_url}/api/v1/knowledge-bases",
        headers=headers,
    )

    require(ready_status == 200 and ready["status"] == "ready", "readiness failed")
    require(login_status == 200 and bool(login.get("access_token")), "login failed")
    require(create_status == 201 and created["name"] == name, "create failed")
    require(
        list_status == 200 and created["id"] in {item["id"] for item in knowledge_bases},
        "tenant-scoped list failed",
    )
    idempotency_key = f"smoke-upload-{uuid4()}"
    upload_headers = {**headers, "Idempotency-Key": idempotency_key}
    upload_status, uploaded = request_json(
        f"{api_url}/api/v1/knowledge-bases/{created['id']}/documents",
        method="POST",
        file=(
            "file",
            "smoke-policy.txt",
            b"Enterprise smoke policy: indexing must persist one authorized chunk.",
            "text/plain",
        ),
        headers=upload_headers,
    )
    require(upload_status == 202 and uploaded["status"] == "pending", "upload failed")
    job = wait_for_job(api_url, uploaded["task_id"], headers)
    require(
        job["status"] == "succeeded" and job["stage"] == "complete",
        f"indexing failed: {job}",
    )
    repeat_status, repeated = request_json(
        f"{api_url}/api/v1/knowledge-bases/{created['id']}/documents",
        method="POST",
        file=(
            "file",
            "smoke-policy.txt",
            b"Enterprise smoke policy: indexing must persist one authorized chunk.",
            "text/plain",
        ),
        headers=upload_headers,
    )
    require(
        repeat_status == 202
        and repeated["task_id"] == uploaded["task_id"]
        and repeated["document_id"] == uploaded["document_id"],
        "idempotent resubmission created a different resource",
    )
    retrieval_status, retrieval = request_json(
        f"{api_url}/api/v1/knowledge-bases/{created['id']}/retrieve",
        method="POST",
        body={
            "query": "authorized chunk smoke policy",
            "mode": "hybrid",
            "top_k": 5,
            "candidate_k": 20,
            "rerank": True,
        },
        headers=headers,
    )
    require(retrieval_status == 200 and bool(retrieval["trace_id"]), "retrieval failed")
    expected_embedding_version = os.environ.get("EXPECTED_EMBEDDING_VERSION")
    require(
        expected_embedding_version is None
        or retrieval["embedding_version"] == expected_embedding_version,
        "retrieval used an unexpected embedding provider",
    )
    require(
        retrieval["mode"] == "hybrid"
        and retrieval["reranker_version"] is not None
        and uploaded["document_id"]
        in {candidate["document_id"] for candidate in retrieval["results"]},
        "hybrid retrieval did not return the authorized indexed document",
    )
    generation_status, generation = request_sse(
        f"{api_url}/api/v1/knowledge-bases/{created['id']}/answers/stream",
        body={
            "query": "authorized chunk smoke policy",
            "mode": "hybrid",
            "top_k": 5,
            "candidate_k": 20,
            "rerank": True,
            "provider": "deterministic",
        },
        headers=headers,
    )
    require(generation_status == 200, "generation stream failed")
    require(
        "event: token" in generation
        and "event: citation" in generation
        and '"document_id":"' + uploaded["document_id"] + '"' in generation
        and 'event: done\ndata: {"status":"succeeded","citations":1}' in generation,
        "generation did not stream a validated citation",
    )
    generation_meta = next(data for event, data in parse_sse(generation) if event == "meta")
    trace_status, trace = request_json(
        f"{api_url}/api/v1/traces/{generation_meta['generation_trace_id']}",
        headers=headers,
    )
    require(
        trace_status == 200
        and trace["retrieval"]["candidates"]
        and trace["rerank"]["enabled"] is True
        and trace["generation"]["status"] == "succeeded"
        and trace["generation"]["provider"] == "deterministic"
        and trace["generation"]["provider_attempts"] == 1,
        "question/answer trace could not reconstruct retrieval, rerank, and generation",
    )
    evaluation_status, evaluation = request_json(
        f"{api_url}/api/v1/knowledge-bases/{created['id']}/evaluations",
        method="POST",
        body={"input": {"query": "authorized chunk smoke policy", "rerank": True}},
        headers=headers,
    )
    require(
        evaluation_status == 200
        and evaluation["output"]["status"] == "succeeded"
        and evaluation["metadata"]["generation_trace_id"],
        "compatible evaluation target failed",
    )
    metrics_status, metrics = request_text(f"{api_url}/metrics")
    require(
        metrics_status == 200
        and "enterprise_rag_http_requests_total" in metrics
        and "enterprise_rag_retrieval_duration_seconds" in metrics
        and "enterprise_rag_generation_ttft_seconds" in metrics,
        "Prometheus metrics endpoint omitted required series",
    )
    delete_status, _ = request_json(
        f"{api_url}/api/v1/documents/{uploaded['document_id']}",
        method="DELETE",
        headers=headers,
    )
    deleted_job_status, _ = request_json(
        f"{api_url}/api/v1/index-jobs/{uploaded['task_id']}",
        headers=headers,
    )
    require(delete_status == 204, "document deletion failed")
    require(deleted_job_status == 404, "document deletion left its job reachable")
    print(
        "End-to-end smoke test passed: readiness, auth, tenant KB, async upload/index, "
        "idempotent resubmission, hybrid retrieval/rerank trace, SSE generation with "
        "validated citation, reconstructed QA trace, evaluation target, metrics, delete"
    )


if __name__ == "__main__":
    main()
