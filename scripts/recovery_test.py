from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import time
from uuid import UUID, uuid4

from sqlalchemy import func, select

from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.models import Chunk, IndexJob, IndexJobStatus
from smoke_test import request_json, require, seed_smoke_identity, wait_for_job


def resolve_docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("docker executable is required for recovery verification")
    return executable


DOCKER = resolve_docker()


def container_id(project: str, service: str) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed Docker command with validated service label
        [
            DOCKER,
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.ID}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    identifiers = [line for line in result.stdout.splitlines() if line]
    if len(identifiers) != 1:
        raise RuntimeError(f"expected one {project}/{service} container, found {identifiers}")
    return identifiers[0]


def docker(action: str, identifier: str) -> None:
    subprocess.run(  # noqa: S603 -- fixed Docker lifecycle operation on discovered container
        [DOCKER, action, identifier],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_healthy(identifier: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(  # noqa: S603 -- fixed Docker inspection format
            [DOCKER, "inspect", "-f", "{{.State.Health.Status}}", identifier],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() == "healthy":
            return
        time.sleep(0.25)
    raise RuntimeError(f"container {identifier} did not become healthy")


async def assert_recovered_once(settings: Settings, job_id: UUID) -> None:
    engine, session_factory = create_database_resources(settings.database_url)
    try:
        async with session_factory() as session:
            job = await session.get(IndexJob, job_id)
            if job is None:
                raise RuntimeError("recovered job disappeared")
            require(job.status is IndexJobStatus.SUCCEEDED, "recovered job did not succeed")
            count = await session.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.document_id == job.document_id)
            )
            require(count == 1, f"recovery created duplicate chunks: {count}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify a real Redis outage and worker restart recovery path"
    )
    parser.add_argument("--project", default="enterprise-rag")
    parser.add_argument("--api-url", default="http://127.0.0.1:18000")
    arguments = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]
    redis_container = container_id(arguments.project, "redis")
    worker_container = container_id(arguments.project, "worker")
    password = f"recovery-{uuid4().hex}"
    tenant_id = asyncio.run(seed_smoke_identity(settings.database_url, password))
    status, login = request_json(
        f"{arguments.api_url}/api/v1/auth/login",
        method="POST",
        form={"username": "smoke-user@example.com", "password": password},
    )
    require(status == 200, "recovery login failed")
    headers = {
        "Authorization": f"Bearer {login['access_token']}",
        "X-Tenant-ID": tenant_id,
    }
    status, knowledge_base = request_json(
        f"{arguments.api_url}/api/v1/knowledge-bases",
        method="POST",
        body={"name": f"Recovery {uuid4()}"},
        headers=headers,
    )
    require(status == 201, "recovery knowledge base creation failed")

    redis_stopped = False
    try:
        docker("stop", redis_container)
        redis_stopped = True
        upload_headers = {**headers, "Idempotency-Key": f"redis-outage-{uuid4()}"}
        upload_status, uploaded = request_json(
            f"{arguments.api_url}/api/v1/knowledge-bases/{knowledge_base['id']}/documents",
            method="POST",
            file=(
                "file",
                "redis-recovery.txt",
                b"Redis recovery must preserve one authoritative PostgreSQL job and chunk set.",
                "text/plain",
            ),
            headers=upload_headers,
        )
        require(upload_status == 202, f"upload during Redis outage failed: {uploaded}")
    finally:
        if redis_stopped:
            docker("start", redis_container)
            wait_healthy(redis_container)

    docker("restart", worker_container)
    wait_healthy(worker_container)
    job = wait_for_job(arguments.api_url, uploaded["task_id"], headers)
    require(job["status"] == "succeeded", f"recovered job failed: {job}")
    asyncio.run(assert_recovered_once(settings, UUID(uploaded["task_id"])))
    print(
        "M5 recovery test passed: upload remained pending during real Redis outage; "
        "Redis restoration plus worker restart re-enqueued and completed exactly one chunk set"
    )


if __name__ == "__main__":
    main()
