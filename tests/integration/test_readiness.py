from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from enterprise_rag_api.main import create_app
from enterprise_rag_core.config import Settings


@pytest.mark.integration
async def test_readiness_checks_all_m1_dependencies(api_client: AsyncClient) -> None:
    response = await api_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "minio": "ok", "redis": "ok"},
    }


@pytest.mark.integration
async def test_readiness_reports_dependency_failure(
    integration_settings: Settings,
    migrated_database: None,
) -> None:
    del migrated_database
    unavailable_settings = integration_settings.model_copy(
        update={"redis_url": "redis://127.0.0.1:1/0"}
    )
    app = create_app(unavailable_settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "ok", "minio": "ok", "redis": "unavailable"},
    }
