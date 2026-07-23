from __future__ import annotations

import json
import math
import os
import platform
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class CorpusDocument:
    document_key: str
    title: str
    content: str


@dataclass(frozen=True)
class RetrievalQuery:
    query_id: str
    query: str
    relevant_document_keys: frozenset[str]


@dataclass(frozen=True)
class RetrievalDataset:
    version: str
    kind: str
    description: str
    corpus: tuple[CorpusDocument, ...]
    queries: tuple[RetrievalQuery, ...]


def runtime_metadata(*package_names: str) -> dict[str, object]:
    packages: dict[str, str] = {}
    for package_name in package_names:
        try:
            packages[package_name] = version(package_name)
        except PackageNotFoundError:
            packages[package_name] = "not-installed"
    return {
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "logical_cpu_count": os.cpu_count(),
        "packages": packages,
    }


def recall_at_k(ranking: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranking[:k]) & relevant) / len(relevant)


def reciprocal_rank_at_k(ranking: list[str], relevant: set[str], k: int) -> float:
    for rank, item in enumerate(ranking[:k], start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranking: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item in enumerate(ranking[:k], start=1)
        if item in relevant
    )
    ideal_hits = min(len(relevant), k)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / ideal if ideal else 0.0


def _required_string(record: dict[str, object], field: str, line_number: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"line {line_number}: {field} must be a non-empty string")
    return value


def load_retrieval_dataset(path: Path) -> RetrievalDataset:
    metadata: dict[str, object] | None = None
    corpus: list[CorpusDocument] = []
    queries: list[RetrievalQuery] = []
    with path.open(encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            if not raw_line.strip():
                continue
            parsed = json.loads(raw_line)
            if not isinstance(parsed, dict):
                raise ValueError(f"line {line_number}: record must be an object")
            record = cast(dict[str, object], parsed)
            record_type = _required_string(record, "record_type", line_number)
            if record_type == "metadata":
                if metadata is not None:
                    raise ValueError("dataset must contain exactly one metadata record")
                metadata = record
            elif record_type == "corpus":
                corpus.append(
                    CorpusDocument(
                        document_key=_required_string(record, "document_key", line_number),
                        title=_required_string(record, "title", line_number),
                        content=_required_string(record, "content", line_number),
                    )
                )
            elif record_type == "query":
                labels = record.get("relevant_document_keys")
                if not isinstance(labels, list) or not labels:
                    raise ValueError(
                        f"line {line_number}: relevant_document_keys must contain labels"
                    )
                label_strings: list[str] = []
                for item in cast(list[object], labels):
                    if not isinstance(item, str) or not item:
                        raise ValueError(
                            f"line {line_number}: relevant_document_keys must contain labels"
                        )
                    label_strings.append(item)
                queries.append(
                    RetrievalQuery(
                        query_id=_required_string(record, "query_id", line_number),
                        query=_required_string(record, "query", line_number),
                        relevant_document_keys=frozenset(label_strings),
                    )
                )
            else:
                raise ValueError(f"line {line_number}: unsupported record_type {record_type}")
    if metadata is None:
        raise ValueError("dataset metadata record is required")
    document_keys = {document.document_key for document in corpus}
    if len(document_keys) != len(corpus):
        raise ValueError("corpus document keys must be unique")
    if len({query.query_id for query in queries}) != len(queries):
        raise ValueError("query IDs must be unique")
    if any(not query.relevant_document_keys <= document_keys for query in queries):
        raise ValueError("every relevance label must reference a corpus document")
    return RetrievalDataset(
        version=_required_string(metadata, "dataset_version", 1),
        kind=_required_string(metadata, "dataset_kind", 1),
        description=_required_string(metadata, "description", 1),
        corpus=tuple(corpus),
        queries=tuple(queries),
    )
