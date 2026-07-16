from __future__ import annotations

import json
from pathlib import Path


def test_fixed_generation_dataset_has_answer_and_abstention_samples() -> None:
    path = Path(__file__).resolve().parents[2] / "data/eval/generation.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert records[0]["version"] == "m4-controlled-grounding-v1"
    assert sum(record["type"] == "corpus" for record in records) == 20
    assert sum(record.get("kind") == "answer" for record in records) == 20
    assert sum(record.get("kind") == "abstain" for record in records) == 20
