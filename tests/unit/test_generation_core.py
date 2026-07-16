from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from enterprise_rag_core.config import Settings
from enterprise_rag_core.generation import CitationStreamParser, encode_sse
from enterprise_rag_core.providers import (
    GenerationPrompt,
    OpenAICompatibleProvider,
    ProviderDefinition,
    ProviderStreamEvent,
    build_provider_registry,
)


def unit_settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused@localhost/unused",
        redis_url="redis://localhost:6379/0",
        minio_endpoint="localhost:9000",
        minio_access_key="unused-access",
        minio_secret_key=SecretStr("unused-secret"),
        minio_bucket="unused",
        jwt_secret=SecretStr("unit-only-" + ("x" * 40)),
        openai_api_key=SecretStr("openai-test-key"),
        deepseek_api_key=SecretStr("deepseek-test-key"),
    )


def test_provider_registry_exposes_two_remote_and_one_local_adapter() -> None:
    registry = build_provider_registry(unit_settings())

    assert registry["openai"].definition.location == "remote"
    assert registry["deepseek"].definition.location == "remote"
    assert registry["ollama"].definition.location == "local"
    assert {
        registry[name].definition.protocol for name in ("openai", "deepseek", "ollama")
    } == {
        "openai-chat-completions"
    }


async def test_openai_compatible_adapter_parses_sse_and_keeps_wire_types_private() -> None:
    seen_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_request.update(json.loads(request.content))
        body = (
            'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        ProviderDefinition(
            name="remote-test",
            location="remote",
            base_url="https://provider.example/v1",
            model="model-test",
            api_key=SecretStr("test-key"),
            config_version="remote-test-v1",
        ),
        transport=httpx.MockTransport(handler),
    )
    events = [
        event
        async for event in provider.stream(
            GenerationPrompt(system="system", user="user"), asyncio.Event()
        )
    ]

    assert [event.text for event in events if event.text] == ["hello ", "world"]
    assert events[-1].usage is None
    assert seen_request == {
        "model": "model-test",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }


@pytest.mark.parametrize("retry_status", [429, 503])
async def test_openai_compatible_adapter_recovers_from_retriable_pre_output_status(
    retry_status: int,
) -> None:
    attempts = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            headers = {"Retry-After": "0"} if retry_status == 429 else {}
            return httpx.Response(retry_status, headers=headers, text="retryable failure")
        body = (
            'data: {"choices":[{"delta":{"content":"recovered"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        ProviderDefinition(
            name="retry-test",
            location="remote",
            base_url="https://provider.example/v1",
            model="model-test",
            api_key=SecretStr("test-key"),
            config_version="retry-test-v1",
            max_attempts=3,
            retry_base_seconds=0,
            retry_max_seconds=0,
            input_cost_per_million_usd=2.0,
            output_cost_per_million_usd=4.0,
        ),
        transport=httpx.MockTransport(handler),
    )

    events = [
        event
        async for event in provider.stream(
            GenerationPrompt(system="system", user="user"), asyncio.Event()
        )
    ]

    assert attempts == 2
    assert events == [
        ProviderStreamEvent(text="recovered", attempt=2),
        ProviderStreamEvent.from_usage(
            input_tokens=12,
            output_tokens=3,
            attempt=2,
            source="provider",
        ),
    ]


async def test_openai_compatible_adapter_does_not_retry_after_output_started() -> None:
    attempts = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\nnot-json\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        ProviderDefinition(
            name="partial-test",
            location="remote",
            base_url="https://provider.example/v1",
            model="model-test",
            api_key=SecretStr("test-key"),
            config_version="partial-test-v1",
            max_attempts=3,
            retry_base_seconds=0,
            retry_max_seconds=0,
        ),
        transport=httpx.MockTransport(handler),
    )

    events = [
        event
        async for event in provider.stream(
            GenerationPrompt(system="system", user="user"), asyncio.Event()
        )
    ]

    assert attempts == 1
    assert events == [ProviderStreamEvent(text="partial", attempt=1)]


def test_citation_parser_handles_split_markers_and_rejects_unknown_chunks() -> None:
    allowed = UUID("00000000-0000-0000-0000-000000000001")
    unknown = UUID("00000000-0000-0000-0000-000000000002")
    parser = CitationStreamParser({allowed})

    pieces: list[str] = []
    citations: list[UUID] = []
    for delta in (
        "Policy text [[chu",
        f"nk:{allowed}]] and forged [[chunk:{unknown}]] end",
    ):
        parsed = parser.feed(delta)
        pieces.extend(parsed.text)
        citations.extend(parsed.citations)
    final = parser.finish()
    pieces.extend(final.text)
    citations.extend(final.citations)

    assert "".join(pieces) == "Policy text  and forged  end"
    assert citations == [allowed]


def test_sse_encoding_never_injects_raw_newlines_into_data_frames() -> None:
    encoded = encode_sse("token", {"text": "first\nsecond"})

    assert encoded == 'event: token\ndata: {"text":"first\\nsecond"}\n\n'
