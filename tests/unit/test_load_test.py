from __future__ import annotations

import pytest
from scripts.load_test import chunk_content, percentile


def test_load_percentile_uses_nearest_rank_and_rejects_empty_input() -> None:
    assert percentile([4, 1, 3, 2], 0.50) == 2
    assert percentile([4, 1, 3, 2], 0.95) == 4
    with pytest.raises(ValueError, match="at least one"):
        percentile([], 0.95)


def test_load_fixture_is_deterministic_and_has_bounded_query_cardinality() -> None:
    assert chunk_content(0) == chunk_content(0)
    assert "loadtoken000" in chunk_content(0)
    assert "loadtoken000" in chunk_content(200)
    assert chunk_content(0) != chunk_content(200)
