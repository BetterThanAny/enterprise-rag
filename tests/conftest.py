from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from enterprise_rag_api.main import create_app
from enterprise_rag_core.config import Settings
from enterprise_rag_core.dispatching import RecordingDispatcher
from enterprise_rag_core.storage import MinioObjectStorage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve_uv() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv executable is required to run tests")
    return executable


UV = _resolve_uv()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for integration tests")
    return value


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    return Settings(
        database_url=_required_env("TEST_DATABASE_URL"),
        redis_url=_required_env("TEST_REDIS_URL"),
        minio_endpoint=_required_env("TEST_MINIO_ENDPOINT"),
        minio_access_key=_required_env("TEST_MINIO_ACCESS_KEY"),
        minio_secret_key=SecretStr(_required_env("TEST_MINIO_SECRET_KEY")),
        minio_secure=False,
        minio_bucket=_required_env("TEST_MINIO_BUCKET"),
        jwt_secret=SecretStr("integration-only-" + ("x" * 40)),
        index_job_lease_seconds=1,
        index_retry_base_seconds=1,
    )


@pytest.fixture(scope="session")
def migrated_database(integration_settings: Settings) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = integration_settings.database_url
    subprocess.run(  # noqa: S603 -- uv is resolved from the trusted test environment
        [UV, "run", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )


@pytest.fixture
async def db_session(
    migrated_database: None,
    integration_settings: Settings,
) -> AsyncIterator[AsyncSession]:
    del migrated_database
    engine = create_async_engine(integration_settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = MinioObjectStorage(integration_settings)
    await storage.remove_prefix("tenants/")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE generation_traces, retrieval_traces, chunks, document_acl, "
                "index_jobs, "
                "document_versions, documents, knowledge_bases, memberships, users, "
                "tenants CASCADE"
            )
        )
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def recording_dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()


@pytest.fixture
async def api_client(
    db_session: AsyncSession,
    integration_settings: Settings,
    recording_dispatcher: RecordingDispatcher,
) -> AsyncIterator[AsyncClient]:
    del db_session
    app = create_app(integration_settings, dispatcher=recording_dispatcher)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
