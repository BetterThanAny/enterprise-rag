from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol, cast

from flashrank import Ranker, RerankRequest

from enterprise_rag_core.config import Settings

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")


class CrossEncoderReranker(Protocol):
    @property
    def version(self) -> str: ...

    def score(self, query: str, passages: list[str]) -> list[float]: ...


class DeterministicCrossEncoderStub:
    """Pairwise local test stub; not a learned production cross-encoder."""

    version = "deterministic-token-overlap-cross-encoder-stub-v1"

    def score(self, query: str, passages: list[str]) -> list[float]:
        query_tokens = set(TOKEN_PATTERN.findall(query.casefold()))
        if not query_tokens:
            return [0.0 for _ in passages]
        scores: list[float] = []
        normalized_query = " ".join(query.casefold().split())
        for passage in passages:
            passage_tokens = set(TOKEN_PATTERN.findall(passage.casefold()))
            overlap = len(query_tokens & passage_tokens) / len(query_tokens)
            phrase_bonus = 0.25 if normalized_query in " ".join(passage.casefold().split()) else 0.0
            scores.append(overlap + phrase_bonus)
        return scores


class FlashRankCrossEncoder:
    def __init__(self, *, model_name: str, cache_dir: str, max_length: int) -> None:
        self.model_name = model_name
        self._ranker = Ranker(
            model_name=model_name,
            cache_dir=str(Path(cache_dir).expanduser()),
            max_length=max_length,
            log_level="WARNING",
        )

    @property
    def version(self) -> str:
        return f"flashrank:{self.model_name}"

    def score(self, query: str, passages: list[str]) -> list[float]:
        request = RerankRequest(
            query=query,
            passages=[{"id": str(index), "text": text} for index, text in enumerate(passages)],
        )
        raw_results = self._ranker.rerank(request)
        results = cast(list[dict[str, object]], raw_results)
        scores_by_index = {
            int(str(result["id"])): float(cast(float, result["score"])) for result in results
        }
        return [scores_by_index.get(index, float("-inf")) for index in range(len(passages))]


def build_reranker(settings: Settings) -> CrossEncoderReranker:
    if settings.reranker_provider == "flashrank":
        return FlashRankCrossEncoder(
            model_name=settings.reranker_model_name,
            cache_dir=settings.reranker_cache_dir,
            max_length=settings.reranker_max_length,
        )
    return DeterministicCrossEncoderStub()
