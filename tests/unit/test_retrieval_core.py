from __future__ import annotations

from uuid import UUID

from enterprise_rag_core.evaluation import (
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank_at_k,
    runtime_metadata,
)
from enterprise_rag_core.reranking import DeterministicCrossEncoderStub
from enterprise_rag_core.retrieval import lexical_websearch_query, reciprocal_rank_fusion


def test_rrf_is_deterministic_and_uses_both_rankings() -> None:
    first = UUID("00000000-0000-0000-0000-000000000001")
    second = UUID("00000000-0000-0000-0000-000000000002")
    third = UUID("00000000-0000-0000-0000-000000000003")

    fused = reciprocal_rank_fusion([[first, second], [second, third]], rank_constant=60)

    assert [item_id for item_id, _ in fused] == [second, first, third]
    assert abs(fused[0][1] - ((1 / 62) + (1 / 61))) < 1e-12


def test_lexical_websearch_query_uses_safe_disjunction_and_handles_punctuation() -> None:
    assert lexical_websearch_query("Retention policy details?") == "retention OR policy OR details"
    assert lexical_websearch_query("!!!") == ""


def test_retrieval_metrics_handle_hits_misses_and_graded_order() -> None:
    relevant = {"doc-a", "doc-b"}
    ranking = ["doc-x", "doc-a", "doc-b"]

    assert recall_at_k(ranking, relevant, 2) == 0.5
    assert reciprocal_rank_at_k(ranking, relevant, 3) == 0.5
    assert abs(ndcg_at_k(ranking, relevant, 3) - 0.6934264036) < 1e-10
    assert recall_at_k([], relevant, 5) == 0.0


def test_runtime_metadata_records_reproducibility_context() -> None:
    metadata = runtime_metadata("numpy", "definitely-not-an-installed-enterprise-rag-package")
    packages = metadata["packages"]

    assert metadata["python_version"]
    assert metadata["platform"]
    assert metadata["machine"]
    assert metadata["logical_cpu_count"]
    assert isinstance(packages, dict)
    assert packages["numpy"]
    assert packages["definitely-not-an-installed-enterprise-rag-package"] == "not-installed"


def test_deterministic_cross_encoder_stub_scores_query_passage_pairs_jointly() -> None:
    reranker = DeterministicCrossEncoderStub()

    scores = reranker.score(
        "retention policy",
        ["unrelated travel guidance", "retention policy keeps records"],
    )

    assert len(scores) == 2
    assert scores[1] > scores[0]
    assert "stub" in reranker.version
