from __future__ import annotations

import asyncio
import logging
import re
import time
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from enterprise_rag_core.logging import request_id_context
from enterprise_rag_core.observability import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


async def request_context(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    incoming = request.headers.get("X-Request-ID", "")
    request_id = incoming if REQUEST_ID_PATTERN.fullmatch(incoming) else str(uuid4())
    context_token = request_id_context.set(request_id)
    started = time.perf_counter()
    request.state.cancel_event = asyncio.Event()
    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(
            method=request.method,
            route=route_path,
            status=str(response.status_code),
        ).inc()
        HTTP_DURATION.labels(method=request.method, route=route_path).observe(
            duration_ms / 1000
        )
        response.headers["X-Request-ID"] = request_id
        log_fields: dict[str, object] = {
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }
        logger.info("request_completed", extra=log_fields)
        return response
    except Exception:
        duration_seconds = time.perf_counter() - started
        HTTP_REQUESTS.labels(
            method=request.method,
            route="unmatched",
            status="500",
        ).inc()
        HTTP_DURATION.labels(method=request.method, route="unmatched").observe(duration_seconds)
        raise
    finally:
        request_id_context.reset(context_token)


def register_request_middleware(app: FastAPI) -> None:
    app.middleware("http")(request_context)
