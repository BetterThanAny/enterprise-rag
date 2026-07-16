from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from enterprise_rag_api.main import create_app
from enterprise_rag_core.config import Settings


def unit_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        redis_url="redis://localhost:6379/0",
        minio_endpoint="localhost:9000",
        minio_access_key="unused-access",
        minio_secret_key=SecretStr("unused-secret"),
        minio_bucket="unused",
        jwt_secret=SecretStr("unit-only-" + ("x" * 40)),
    )


async def test_liveness_and_version_are_dependency_independent() -> None:
    app = create_app(unit_settings())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/health/live", headers={"X-Request-ID": "known-request"})
        version = await client.get("/version")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert live.headers["X-Request-ID"] == "known-request"
    assert version.status_code == 200
    assert version.json()["version"] == "0.1.0"


async def test_missing_credentials_have_stable_error_shape() -> None:
    app = create_app(unit_settings())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/knowledge-bases",
            headers={"X-Tenant-ID": "00000000-0000-0000-0000-000000000001"},
        )

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "authentication_required"
    assert body["error"]["message"] == "Authentication required"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]
