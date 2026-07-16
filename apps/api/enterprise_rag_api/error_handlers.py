from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from enterprise_rag_core.errors import DomainError
from enterprise_rag_core.logging import request_id_context

logger = logging.getLogger(__name__)


def error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id_context.get(),
        }
    }


async def domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        raise TypeError("domain_error_handler received an incompatible exception")
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.code, exc.message),
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
    )


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise TypeError("validation_error_handler received an incompatible exception")
    details: list[dict[str, Any]] = []
    for error in exc.errors():
        details.append(
            {
                "location": [str(part) for part in error["loc"]],
                "message": error["msg"],
                "type": error["type"],
            }
        )
    payload: dict[str, Any] = error_payload("validation_error", "Request validation failed")
    payload["error"]["details"] = details
    return JSONResponse(status_code=422, content=payload)


async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=error_payload("internal_error", "Internal server error"),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
