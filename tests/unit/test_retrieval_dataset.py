from __future__ import annotations

from pathlib import Path

from enterprise_rag_core.evaluation import load_retrieval_dataset


def test_fixed_retrieval_dataset_has_200_labeled_queries() -> None:
    dataset = load_retrieval_dataset(
        Path(__file__).resolve().parents[2] / "data/eval/retrieval.jsonl"
    )

    assert dataset.version == "m3-controlled-synthetic-v1"
    assert dataset.kind == "controlled_synthetic_regression"
    assert len(dataset.corpus) == 50
    assert len(dataset.queries) == 200
    assert all(query.relevant_document_keys for query in dataset.queries)
    assert len({query.query_id for query in dataset.queries}) == 200
