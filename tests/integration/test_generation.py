from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.m2_helpers import auth_headers, seed_m2_identity, upload_document

from enterprise_rag_api.main import create_app
from enterprise_rag_core.config import Settings
from enterprise_rag_core.dispatching import RecordingDispatcher
from enterprise_rag_core.generation import GenerationService
from enterprise_rag_core.indexing import IndexingPipeline
from enterprise_rag_core.models import (
    GenerationStatus,
    GenerationTrace,
    IndexJobStatus,
    RetrievalMode,
    Role,
)
from enterprise_rag_core.providers import (
    GenerationPrompt,
    ProviderDefinition,
    ProviderStreamEvent,
)
from enterprise_rag_core.reranking import DeterministicCrossEncoderStub
from enterprise_rag_core.retrieval import RetrievalRepository, RetrievalService
from enterprise_rag_core.services import TenantContext


def parse_sse(body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


@pytest.mark.integration
async def test_sse_answer_persists_versions_and_authorized_citation_metadata(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "generation-answer")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="handbook.md",
        content=b"# Security\n\n## Retention\n\nretentiontoken records are kept seven years",
        idempotency_key="generation-answer-upload",
    )
    result = await IndexingPipeline(integration_settings).process(UUID(uploaded.json()["task_id"]))
    assert result.status is IndexJobStatus.SUCCEEDED
    headers = await auth_headers(api_client, identity)

    response = await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/answers/stream",
        headers=headers,
        json={"query": "retentiontoken", "provider": "deterministic", "rerank": False},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    citation = next(data for event, data in events if event == "citation")
    assert citation["document_id"] == uploaded.json()["document_id"]
    assert citation["page_number"] is None
    assert citation["heading_path"] == "Security > Retention"
    assert "retentiontoken" in str(citation["excerpt"])
    assert events[-1] == ("done", {"status": "succeeded", "citations": 1})

    trace = (await db_session.execute(select(GenerationTrace))).scalar_one()
    assert trace.status is GenerationStatus.SUCCEEDED
    assert trace.prompt_version == integration_settings.generation_prompt_version
    assert trace.model == "deterministic-grounded-answer-v1"
    assert trace.retriever_version == integration_settings.retrieval_config_version
    assert trace.embedding_version == integration_settings.embedding_model_version
    assert trace.retrieval_trace_id is not None
    assert len(trace.citations) == 1


@pytest.mark.integration
async def test_answer_trace_reconstructs_retrieval_rerank_provider_and_usage(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "m5-trace")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="trace.md",
        content=b"# Operations\n\ntracetoken recovery evidence is observable",
        idempotency_key="m5-trace-upload",
    )
    result = await IndexingPipeline(integration_settings).process(UUID(uploaded.json()["task_id"]))
    assert result.status is IndexJobStatus.SUCCEEDED
    headers = await auth_headers(api_client, identity)
    headers["X-Request-ID"] = "m5-trace-request"

    answer = await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/answers/stream",
        headers=headers,
        json={"query": "tracetoken", "provider": "deterministic", "rerank": True},
    )
    meta = next(data for event, data in parse_sse(answer.text) if event == "meta")
    reconstructed = await api_client.get(
        f"/api/v1/traces/{meta['generation_trace_id']}",
        headers=headers,
    )

    assert reconstructed.status_code == 200
    payload = reconstructed.json()
    assert re.fullmatch(r"[0-9a-f]{32}", payload["trace_id"])
    assert payload["request_id"] == "m5-trace-request"
    assert payload["retrieval"]["trace_id"] == meta["retrieval_trace_id"]
    assert re.fullmatch(r"[0-9a-f]{16}", payload["retrieval"]["span_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", payload["rerank"]["span_id"])
    assert payload["retrieval"]["candidates"]
    assert payload["generation"]["provider"] == "deterministic"
    assert payload["generation"]["status"] == "succeeded"
    assert re.fullmatch(r"[0-9a-f]{16}", payload["generation"]["span_id"])
    assert re.fullmatch(r"[0-9a-f]{16}", payload["generation"]["provider_span_id"])
    assert payload["generation"]["ttft_ms"] >= 0
    assert payload["generation"]["duration_ms"] >= payload["generation"]["ttft_ms"]
    assert payload["generation"]["input_tokens"] > 0
    assert payload["generation"]["output_tokens"] > 0
    assert payload["generation"]["usage_source"] == "estimated"
    assert payload["generation"]["estimated_cost_usd"] == 0
    assert payload["generation"]["provider_attempts"] == 1


@pytest.mark.integration
async def test_llm_eval_platform_compatible_http_target_contract(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "m5-eval-target")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="evaluation.txt",
        content=b"evaltargettoken compatible evaluation evidence",
        idempotency_key="m5-eval-target-upload",
    )
    result = await IndexingPipeline(integration_settings).process(UUID(uploaded.json()["task_id"]))
    assert result.status is IndexJobStatus.SUCCEEDED
    headers = await auth_headers(api_client, identity)

    response = await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/evaluations",
        headers=headers,
        json={
            "input": {
                "query": "evaltargettoken",
                "mode": "hybrid",
                "top_k": 5,
                "candidate_k": 20,
                "rerank": True,
                "provider": "deterministic",
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["output"]["status"] == "succeeded"
    assert payload["output"]["refused"] is False
    assert payload["output"]["answer"]
    assert payload["output"]["citation_ids"]
    assert payload["usage"]["input_tokens"] > 0
    assert payload["usage"]["output_tokens"] > 0
    assert payload["metadata"]["generation_trace_id"]
    assert payload["metadata"]["retrieval_trace_id"]
    assert payload["metadata"]["retrieved_ids"]


@pytest.mark.integration
async def test_prometheus_metrics_expose_requests_errors_retrieval_and_generation(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "m5-metrics")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="metrics.txt",
        content=b"metricstoken observable generation evidence",
        idempotency_key="m5-metrics-upload",
    )
    result = await IndexingPipeline(integration_settings).process(UUID(uploaded.json()["task_id"]))
    assert result.status is IndexJobStatus.SUCCEEDED
    headers = await auth_headers(api_client, identity)
    await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/answers/stream",
        headers=headers,
        json={"query": "metricstoken", "provider": "deterministic", "rerank": True},
    )
    await api_client.get("/definitely-missing")

    response = await api_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for metric in (
        "enterprise_rag_http_requests_total",
        "enterprise_rag_http_request_duration_seconds",
        "enterprise_rag_retrieval_duration_seconds",
        "enterprise_rag_generation_ttft_seconds",
        "enterprise_rag_generation_duration_seconds",
        "enterprise_rag_generation_input_tokens_total",
        "enterprise_rag_generation_output_tokens_total",
        "enterprise_rag_generation_estimated_cost_usd_total",
        "enterprise_rag_generation_runs_total",
    ):
        assert metric in response.text


@pytest.mark.integration
async def test_no_lexical_evidence_abstains_without_calling_remote_provider(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "generation-abstain")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="policy.txt",
        content=b"knownpolicy evidence",
        idempotency_key="generation-abstain-upload",
    )
    result = await IndexingPipeline(integration_settings).process(UUID(uploaded.json()["task_id"]))
    assert result.status is IndexJobStatus.SUCCEEDED
    headers = await auth_headers(api_client, identity)

    response = await api_client.post(
        f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/answers/stream",
        headers=headers,
        json={
            "query": "noevidenceuniquetoken999",
            "provider": "deterministic",
            "rerank": False,
        },
    )

    events = parse_sse(response.text)
    assert events[-1] == ("done", {"status": "abstained", "citations": 0})
    assert all(event != "citation" for event, _ in events)
    trace = (await db_session.execute(select(GenerationTrace))).scalar_one()
    assert trace.status is GenerationStatus.ABSTAINED
    assert trace.citations == []


class StaticProvider:
    def __init__(self, name: str, *, delay: float = 0) -> None:
        self.definition = ProviderDefinition(
            name=name,
            location="test",
            base_url=f"stub://{name}",
            model=f"{name}-model",
            api_key=None,
            config_version=f"{name}-v1",
        )
        self.delay = delay
        self.calls = 0
        self.released = asyncio.Event()

    async def stream(
        self,
        prompt: GenerationPrompt,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        del cancel_event
        self.calls += 1
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            match = re.search(r'<chunk id="([0-9a-f-]{36})"', prompt.user)
            assert match is not None
            yield ProviderStreamEvent(
                text=f"provider={self.definition.name} [[chunk:{match.group(1)}]]",
                attempt=1,
            )
        finally:
            self.released.set()


@pytest.mark.integration
async def test_provider_switching_uses_one_business_endpoint_without_code_changes(
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    providers = {name: StaticProvider(name) for name in ("remote-a", "remote-b", "local-a")}
    app = create_app(
        integration_settings,
        dispatcher=RecordingDispatcher(),
        providers=providers,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            identity = await seed_m2_identity(db_session, "provider-switch")
            uploaded = await upload_document(
                client,
                identity,
                filename="switch.txt",
                content=b"switchtoken provider evidence",
                idempotency_key="provider-switch-upload",
            )
            result = await IndexingPipeline(integration_settings).process(
                UUID(uploaded.json()["task_id"])
            )
            assert result.status is IndexJobStatus.SUCCEEDED
            headers = await auth_headers(client, identity)
            for name in providers:
                response = await client.post(
                    f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/answers/stream",
                    headers=headers,
                    json={"query": "switchtoken", "provider": name, "rerank": False},
                )
                events = parse_sse(response.text)
                meta = next(data for event, data in events if event == "meta")
                assert meta["provider"] == name
                assert events[-1][1]["status"] == "succeeded"
    assert all(provider.calls == 1 for provider in providers.values())


@pytest.mark.integration
async def test_generation_timeout_releases_provider_and_persists_failure(
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    slow = StaticProvider("slow", delay=0.1)
    timeout_settings = integration_settings.model_copy(
        update={"generation_timeout_seconds": 0.01, "openai_api_key": SecretStr("unused")}
    )
    app = create_app(
        timeout_settings,
        dispatcher=RecordingDispatcher(),
        providers={"slow": slow},
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            identity = await seed_m2_identity(db_session, "provider-timeout")
            uploaded = await upload_document(
                client,
                identity,
                filename="timeout.txt",
                content=b"timeouttoken provider evidence",
                idempotency_key="provider-timeout-upload",
            )
            result = await IndexingPipeline(timeout_settings).process(
                UUID(uploaded.json()["task_id"])
            )
            assert result.status is IndexJobStatus.SUCCEEDED
            headers = await auth_headers(client, identity)
            response = await client.post(
                f"/api/v1/knowledge-bases/{identity.knowledge_base_id}/answers/stream",
                headers=headers,
                json={"query": "timeouttoken", "provider": "slow", "rerank": False},
            )
    events = parse_sse(response.text)
    assert events[-1] == ("error", {"code": "generation_timeout"})
    assert slow.released.is_set()
    trace = (await db_session.execute(select(GenerationTrace))).scalar_one()
    assert trace.status is GenerationStatus.FAILED
    assert trace.error_code == "generation_timeout"


class HangingProvider(StaticProvider):
    async def stream(
        self,
        prompt: GenerationPrompt,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        del prompt, cancel_event
        self.calls += 1
        try:
            yield ProviderStreamEvent(text="partial answer ", attempt=1)
            await asyncio.Event().wait()
        finally:
            self.released.set()


@pytest.mark.integration
async def test_closing_stream_after_client_disconnect_cancels_and_releases_provider(
    api_client: AsyncClient,
    db_session: AsyncSession,
    integration_settings: Settings,
) -> None:
    identity = await seed_m2_identity(db_session, "provider-disconnect")
    uploaded = await upload_document(
        api_client,
        identity,
        filename="disconnect.txt",
        content=b"disconnecttoken provider evidence",
        idempotency_key="provider-disconnect-upload",
    )
    result = await IndexingPipeline(integration_settings).process(UUID(uploaded.json()["task_id"]))
    assert result.status is IndexJobStatus.SUCCEEDED
    provider = HangingProvider("hanging")
    retrieval = RetrievalService(
        RetrievalRepository(db_session), integration_settings, DeterministicCrossEncoderStub()
    )
    service = GenerationService(
        db_session,
        retrieval,
        integration_settings,
        {provider.definition.name: provider},
    )
    stream = service.stream(
        context=TenantContext(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            role=Role.OWNER,
        ),
        knowledge_base_id=identity.knowledge_base_id,
        query="disconnecttoken",
        mode=RetrievalMode.HYBRID,
        top_k=5,
        candidate_k=20,
        rerank=False,
        provider_name=provider.definition.name,
        cancel_event=asyncio.Event(),
    )
    assert (await anext(stream)).event == "meta"
    assert (await anext(stream)).event == "token"

    await stream.aclose()
    db_session.expire_all()
    trace = (await db_session.execute(select(GenerationTrace))).scalar_one()
    assert trace.status is GenerationStatus.CANCELLED
    assert trace.error_code == "client_disconnected"
    assert provider.released.is_set()
