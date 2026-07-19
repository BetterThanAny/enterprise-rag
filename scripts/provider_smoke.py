from __future__ import annotations

import argparse
import asyncio

from enterprise_rag_core.providers import (
    GenerationPrompt,
    OpenAICompatibleProvider,
    ProviderDefinition,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a real OpenAI-compatible provider returns streamed text."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default="qwen2.5:0.5b")
    return parser.parse_args()


async def run(base_url: str, model: str) -> None:
    provider = OpenAICompatibleProvider(
        ProviderDefinition(
            name="ollama-live-smoke",
            location="local",
            base_url=base_url,
            model=model,
            api_key=None,
            config_version=f"ollama-live:{model}",
            max_attempts=1,
        )
    )
    prompt = GenerationPrompt(
        system="Reply with one short sentence.",
        user="State that the local provider smoke test returned a real token.",
    )
    text_parts: list[str] = []
    usage_seen = False
    async for event in provider.stream(prompt, asyncio.Event()):
        if event.text:
            text_parts.append(event.text)
        if event.usage is not None:
            usage_seen = True
    rendered = "".join(text_parts).strip()
    if not rendered:
        raise RuntimeError("Provider completed without returning streamed text")
    print(
        f"Provider smoke passed: model={model}, streamed_characters={len(rendered)}, "
        f"usage_reported={str(usage_seen).lower()}"
    )
    print(rendered)


def main() -> None:
    arguments = parse_arguments()
    asyncio.run(run(arguments.base_url, arguments.model))


if __name__ == "__main__":
    main()
