from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from enterprise_rag_api.error_handlers import register_error_handlers
from enterprise_rag_api.middleware import register_request_middleware
from enterprise_rag_api.routers import (
    auth,
    documents,
    generation,
    knowledge_bases,
    observability,
    retrieval,
    system,
)
from enterprise_rag_core.config import Settings
from enterprise_rag_core.database import create_database_resources
from enterprise_rag_core.dispatching import JobDispatcher, UnavailableDispatcher
from enterprise_rag_core.logging import configure_logging
from enterprise_rag_core.observability import configure_telemetry
from enterprise_rag_core.providers import GenerationProvider, build_provider_registry
from enterprise_rag_core.reranking import CrossEncoderReranker, build_reranker


def create_app(
    settings: Settings | None = None,
    *,
    dispatcher: JobDispatcher | None = None,
    reranker: CrossEncoderReranker | None = None,
    providers: Mapping[str, GenerationProvider] | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()  # type: ignore[call-arg]
    configure_logging(resolved_settings.log_level)
    configure_telemetry(
        service_name=resolved_settings.app_name,
        service_version=resolved_settings.app_version,
        environment=resolved_settings.environment,
        otlp_endpoint=resolved_settings.otel_exporter_otlp_endpoint,
    )
    engine, session_factory = create_database_resources(
        resolved_settings.database_url,
        pool_size=resolved_settings.database_pool_size,
        max_overflow=resolved_settings.database_max_overflow,
        pool_timeout=resolved_settings.database_pool_timeout_seconds,
    )
    if dispatcher is None:
        if settings is None:
            from enterprise_rag_worker.dispatcher import DramatiqDispatcher

            dispatcher = DramatiqDispatcher(resolved_settings)
        else:
            dispatcher = UnavailableDispatcher()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title="Enterprise RAG API",
        version=resolved_settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.dispatcher = dispatcher
    app.state.reranker = reranker or build_reranker(resolved_settings)
    app.state.providers = dict(providers or build_provider_registry(resolved_settings))
    register_request_middleware(app)
    register_error_handlers(app)
    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(knowledge_bases.router)
    app.include_router(documents.router)
    app.include_router(retrieval.router)
    app.include_router(generation.router)
    app.include_router(observability.router)
    return app
