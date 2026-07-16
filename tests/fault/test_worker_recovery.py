from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.m2_helpers import seed_m2_identity, upload_document

from enterprise_rag_core.config import Settings
from enterprise_rag_core.models import Chunk, IndexJob, IndexJobStatus


async def wait_for_file(path: Path) -> None:
    async with asyncio.timeout(10):
        while not await asyncio.to_thread(path.exists):  # noqa: ASYNC110
            await asyncio.sleep(0.05)


def worker_env(settings: Settings, stage: str | None, signal_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": settings.database_url,
            "REDIS_URL": settings.redis_url,
            "MINIO_ENDPOINT": settings.minio_endpoint,
            "MINIO_ACCESS_KEY": settings.minio_access_key,
            "MINIO_SECRET_KEY": settings.minio_secret_key.get_secret_value(),
            "MINIO_BUCKET": settings.minio_bucket,
            "JWT_SECRET": settings.jwt_secret.get_secret_value(),
            "INDEX_JOB_LEASE_SECONDS": "1",
        }
    )
    if stage is not None:
        env["INDEXING_FAULT_PAUSE_STAGE"] = stage
        env["INDEXING_FAULT_SIGNAL_PATH"] = str(signal_path)
    else:
        env.pop("INDEXING_FAULT_PAUSE_STAGE", None)
        env.pop("INDEXING_FAULT_SIGNAL_PATH", None)
    return env


async def run_worker(job_id: UUID, env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "enterprise_rag_worker.run_job",
        str(job_id),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@pytest.mark.fault
@pytest.mark.parametrize("stage", ["parse", "embedding", "database_write"])
async def test_worker_kill_at_each_stage_recovers_without_duplicate_chunks(
    stage: str,
    tmp_path: Path,
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, f"fault-{stage}")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="fault.txt",
        content=b"Worker recovery must preserve exactly one committed chunk set.",
        idempotency_key=f"fault-{stage}",
    )
    job_id = UUID(uploaded.json()["task_id"])
    signal_path = tmp_path / f"{stage}.reached"
    first = await run_worker(job_id, worker_env(integration_settings, stage, signal_path))
    await wait_for_file(signal_path)
    first.kill()
    await first.wait()
    await asyncio.sleep(1.1)

    recovered = await run_worker(job_id, worker_env(integration_settings, None, signal_path))
    stdout, stderr = await asyncio.wait_for(recovered.communicate(), timeout=15)
    assert recovered.returncode == 0, (stdout + stderr).decode(errors="replace")
    db_session.expire_all()
    job = await db_session.get(IndexJob, job_id)
    chunk_count = await db_session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == job.document_id)
        if job is not None
        else select(func.count()).select_from(Chunk)
    )
    assert job is not None
    assert job.status is IndexJobStatus.SUCCEEDED
    assert job.attempts == 2
    assert chunk_count == 1
