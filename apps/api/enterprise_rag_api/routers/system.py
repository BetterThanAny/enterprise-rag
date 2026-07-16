from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from enterprise_rag_core.services import ReadinessService

router = APIRouter(tags=["system"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(request: Request) -> JSONResponse:
    service = ReadinessService(request.app.state.session_factory, request.app.state.settings)
    ready, checks = await service.check()
    content: dict[str, Any] = {
        "status": "ready" if ready else "unavailable",
        "checks": checks,
    }
    return JSONResponse(status_code=200 if ready else 503, content=content)


@router.get("/version")
async def version(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "git_sha": settings.git_sha,
    }
