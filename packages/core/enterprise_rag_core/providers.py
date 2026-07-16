from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal, Protocol, cast

import httpx
from pydantic import SecretStr

from enterprise_rag_core.config import Settings
from enterprise_rag_core.observability import PROVIDER_RETRIES


@dataclass(frozen=True)
class GenerationPrompt:
    system: str
    user: str


@dataclass(frozen=True)
class ProviderDefinition:
    name: str
    location: Literal["remote", "local", "test"]
    base_url: str
    model: str
    api_key: SecretStr | None
    config_version: str
    protocol: str = "openai-chat-completions"
    max_attempts: int = 3
    retry_base_seconds: float = 0.25
    retry_max_seconds: float = 5.0
    input_cost_per_million_usd: float = 0
    output_cost_per_million_usd: float = 0


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int
    output_tokens: int
    source: Literal["provider", "estimated"]


@dataclass(frozen=True)
class ProviderStreamEvent:
    text: str | None = None
    usage: ProviderUsage | None = None
    attempt: int = 1

    @classmethod
    def from_usage(
        cls,
        *,
        input_tokens: int,
        output_tokens: int,
        attempt: int,
        source: Literal["provider", "estimated"],
    ) -> ProviderStreamEvent:
        return cls(
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                source=source,
            ),
            attempt=attempt,
        )


class GenerationProvider(Protocol):
    definition: ProviderDefinition

    def stream(
        self,
        prompt: GenerationPrompt,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[ProviderStreamEvent, None]: ...


class OpenAICompatibleProvider:
    """The only module that knows OpenAI-compatible HTTP and SSE wire types."""

    def __init__(
        self,
        definition: ProviderDefinition,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.definition = definition
        self.transport = transport

    async def stream(
        self,
        prompt: GenerationPrompt,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        headers = {"Content-Type": "application/json"}
        if self.definition.api_key is not None:
            headers["Authorization"] = (
                f"Bearer {self.definition.api_key.get_secret_value()}"
            )
        payload = {
            "model": self.definition.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(300.0),
            follow_redirects=False,
        ) as client:
            endpoint = f"{self.definition.base_url.rstrip('/')}/chat/completions"
            for attempt in range(1, self.definition.max_attempts + 1):
                async with client.stream(
                    "POST", endpoint, headers=headers, json=payload
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < self.definition.max_attempts:
                            PROVIDER_RETRIES.labels(
                                provider=self.definition.name,
                                reason=str(response.status_code),
                            ).inc()
                            retry_after = self._retry_after(response, attempt)
                            if cancel_event.is_set():
                                return
                            await asyncio.sleep(retry_after)
                            continue
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if cancel_event.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            if data == "[DONE]":
                                return
                            continue
                        event = cast(dict[str, object], json.loads(data))
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            typed_usage = cast(dict[str, object], usage)
                            input_tokens = typed_usage.get("prompt_tokens")
                            output_tokens = typed_usage.get("completion_tokens")
                            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                                yield ProviderStreamEvent.from_usage(
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    attempt=attempt,
                                    source="provider",
                                )
                        choices = event.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        raw_choice = cast(list[object], choices)[0]
                        if not isinstance(raw_choice, dict):
                            continue
                        choice = cast(dict[str, object], raw_choice)
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        delta = cast(dict[str, object], delta)
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            yield ProviderStreamEvent(text=content, attempt=attempt)
                    return

    def _retry_after(self, response: httpx.Response, attempt: int) -> float:
        raw = response.headers.get("Retry-After")
        if raw is not None:
            try:
                return min(max(float(raw), 0), self.definition.retry_max_seconds)
            except ValueError:
                pass
        delay = self.definition.retry_base_seconds * (2 ** max(attempt - 1, 0))
        return min(delay, self.definition.retry_max_seconds)


class DeterministicGenerationStub:
    """Explicit no-cost test/development stub; never used as a production model."""

    definition = ProviderDefinition(
        name="deterministic",
        location="test",
        base_url="stub://deterministic",
        model="deterministic-grounded-answer-v1",
        api_key=None,
        config_version="deterministic-generation-stub-v1",
    )

    async def stream(
        self,
        prompt: GenerationPrompt,
        cancel_event: asyncio.Event,
    ) -> AsyncGenerator[ProviderStreamEvent, None]:
        match = re.search(r'<chunk id="([0-9a-f-]{36})"', prompt.user)
        if match is None:
            return
        parts = (
            "根据已授权的知识库证据,",
            "可以确认该信息。",
            f" [[chunk:{match.group(1)}]]",
        )
        for part in parts:
            if cancel_event.is_set():
                return
            await asyncio.sleep(0)
            yield ProviderStreamEvent(text=part, attempt=1)
        rendered = f"{prompt.system}\n{prompt.user}"
        answer = "".join(parts)
        yield ProviderStreamEvent.from_usage(
            input_tokens=estimate_tokens(rendered),
            output_tokens=estimate_tokens(answer),
            attempt=1,
            source="estimated",
        )


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def build_provider_registry(settings: Settings) -> dict[str, GenerationProvider]:
    definitions = (
        ProviderDefinition(
            name="openai",
            location="remote",
            base_url=settings.openai_base_url,
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            config_version=f"openai:{settings.generation_config_version}",
            max_attempts=settings.provider_max_attempts,
            retry_base_seconds=settings.provider_retry_base_seconds,
            retry_max_seconds=settings.provider_retry_max_seconds,
            input_cost_per_million_usd=settings.openai_input_cost_per_million_usd,
            output_cost_per_million_usd=settings.openai_output_cost_per_million_usd,
        ),
        ProviderDefinition(
            name="deepseek",
            location="remote",
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            config_version=f"deepseek:{settings.generation_config_version}",
            max_attempts=settings.provider_max_attempts,
            retry_base_seconds=settings.provider_retry_base_seconds,
            retry_max_seconds=settings.provider_retry_max_seconds,
            input_cost_per_million_usd=settings.deepseek_input_cost_per_million_usd,
            output_cost_per_million_usd=settings.deepseek_output_cost_per_million_usd,
        ),
        ProviderDefinition(
            name="ollama",
            location="local",
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            api_key=None,
            config_version=f"ollama:{settings.generation_config_version}",
            max_attempts=settings.provider_max_attempts,
            retry_base_seconds=settings.provider_retry_base_seconds,
            retry_max_seconds=settings.provider_retry_max_seconds,
        ),
    )
    registry: dict[str, GenerationProvider] = {
        definition.name: OpenAICompatibleProvider(definition) for definition in definitions
    }
    registry["deterministic"] = DeterministicGenerationStub()
    return registry
