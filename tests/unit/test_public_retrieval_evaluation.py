from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from scripts.evaluate_public_retrieval import evaluate_provider, load_scifact_archive


def _write_scifact_archive(path: Path) -> str:
    corpus: list[dict[str, object]] = [
        {"doc_id": 10, "title": "Retention", "abstract": ["Records remain seven years."]},
        {"doc_id": 20, "title": "Travel", "abstract": ["Receipts support expenses."]},
    ]
    claims: list[dict[str, object]] = [
        {
            "id": 1,
            "claim": "Records remain for seven years.",
            "evidence": {"10": [{"sentences": [0], "label": "SUPPORT"}]},
            "cited_doc_ids": [10],
        },
        {"id": 2, "claim": "An unlabeled claim.", "evidence": {}, "cited_doc_ids": [20]},
    ]
    with tarfile.open(path, "w:gz") as archive:
        datasets: tuple[tuple[str, list[dict[str, object]]], ...] = (
            ("data/corpus.jsonl", corpus),
            ("data/claims_dev.jsonl", claims),
        )
        for name, records in datasets:
            content = "".join(json.dumps(record) + "\n" for record in records).encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class KeywordEmbeddingProvider:
    dimensions = 2
    is_semantic = True
    version = "keyword-test-v1"

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [1.0, 0.0] if "record" in text.casefold() else [0.0, 1.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def test_scifact_loader_keeps_only_queries_with_human_evidence(tmp_path: Path) -> None:
    archive = tmp_path / "scifact.tar.gz"
    digest = _write_scifact_archive(archive)

    dataset = load_scifact_archive(archive, expected_sha256=digest)

    assert dataset.kind == "public_human_labeled_scientific_retrieval"
    assert len(dataset.corpus) == 2
    assert len(dataset.queries) == 1
    assert dataset.queries[0].relevant_document_keys == frozenset({"10"})

    with pytest.raises(ValueError, match="checksum"):
        load_scifact_archive(archive, expected_sha256="0" * 64)


def test_public_evaluation_reports_standard_retrieval_metrics(tmp_path: Path) -> None:
    archive = tmp_path / "scifact.tar.gz"
    digest = _write_scifact_archive(archive)
    dataset = load_scifact_archive(archive, expected_sha256=digest)

    metrics = evaluate_provider(dataset, KeywordEmbeddingProvider())

    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr_at_10"] == 1.0
    assert metrics["ndcg_at_10"] == 1.0
    assert metrics["queries"] == 1
