"""Tests for minion/memory/vector_store.py — JSONVectorStore.

No API calls. All tests use tmp_path for file I/O.
"""

import json
import math
import pytest

from minion.memory.vector_store import JSONVectorStore


def _unit(dims: int, axis: int) -> list[float]:
    """Return a unit vector along the given axis."""
    v = [0.0] * dims
    v[axis] = 1.0
    return v


def _normalized(v: list[float]) -> list[float]:
    """Normalize a vector to unit length."""
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


# ─── Basic operations ─────────────────────────────────────────────────────────

class TestJSONVectorStoreBasic:
    def test_empty_store_search_returns_empty(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        results = store.search(_unit(3, 0), top_k=5)
        assert results == []

    def test_empty_store_ids_returns_empty(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        assert store.ids() == []

    def test_upsert_and_ids(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.upsert("id1", _unit(3, 0))
        assert "id1" in store.ids()

    def test_upsert_multiple_ids(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.upsert("id1", _unit(3, 0))
        store.upsert("id2", _unit(3, 1))
        assert set(store.ids()) == {"id1", "id2"}

    def test_delete_removes_id(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.upsert("id1", _unit(3, 0))
        store.delete("id1")
        assert "id1" not in store.ids()

    def test_delete_nonexistent_is_noop(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.delete("nonexistent")   # should not raise
        assert store.ids() == []

    def test_search_after_delete_excludes_deleted(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.upsert("id1", _unit(3, 0))
        store.upsert("id2", _unit(3, 1))
        store.delete("id1")
        results = store.search(_unit(3, 0), top_k=5)
        ids = [r[0] for r in results]
        assert "id1" not in ids


# ─── Similarity scoring ───────────────────────────────────────────────────────

class TestSimilarityScoring:
    def test_identical_vector_scores_one(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        v = _unit(4, 2)
        store.upsert("id1", v)
        results = store.search(v, top_k=1)
        assert len(results) == 1
        assert abs(results[0][1] - 1.0) < 1e-5

    def test_orthogonal_vectors_score_zero(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.upsert("id1", _unit(4, 0))
        results = store.search(_unit(4, 1), top_k=1)
        assert len(results) == 1
        assert abs(results[0][1]) < 1e-5

    def test_results_sorted_by_score_descending(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        # id1 is similar to query; id2 is orthogonal
        store.upsert("id1", _unit(4, 0))
        store.upsert("id2", _unit(4, 1))
        results = store.search(_unit(4, 0), top_k=5)
        scores = [r[1] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_limits_results(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        for i in range(5):
            store.upsert(f"id{i}", _unit(5, i))
        results = store.search(_unit(5, 0), top_k=2)
        assert len(results) <= 2

    def test_min_score_filters_low_similarity(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.upsert("similar", _unit(4, 0))
        store.upsert("orthogonal", _unit(4, 1))
        # min_score=0.5 should exclude the orthogonal vector (score≈0)
        results = store.search(_unit(4, 0), top_k=5, min_score=0.5)
        ids = [r[0] for r in results]
        assert "similar" in ids
        assert "orthogonal" not in ids

    def test_zero_query_vector_returns_empty(self, tmp_path):
        store = JSONVectorStore(tmp_path / "index.json")
        store.upsert("id1", _unit(3, 0))
        results = store.search([0.0, 0.0, 0.0], top_k=5)
        assert results == []


# ─── Persistence ─────────────────────────────────────────────────────────────

class TestPersistence:
    def test_survives_reload(self, tmp_path):
        index_path = tmp_path / "index.json"
        store = JSONVectorStore(index_path)
        store.upsert("id1", _unit(3, 0))

        # Reload from same file
        store2 = JSONVectorStore(index_path)
        assert "id1" in store2.ids()

    def test_search_works_after_reload(self, tmp_path):
        index_path = tmp_path / "index.json"
        store = JSONVectorStore(index_path)
        v = _unit(3, 0)
        store.upsert("id1", v)

        store2 = JSONVectorStore(index_path)
        results = store2.search(v, top_k=1)
        assert results[0][0] == "id1"

    def test_creates_parent_directory(self, tmp_path):
        index_path = tmp_path / "subdir" / "nested" / "index.json"
        store = JSONVectorStore(index_path)
        store.upsert("id1", _unit(3, 0))
        assert index_path.exists()

    def test_starts_empty_when_file_missing(self, tmp_path):
        store = JSONVectorStore(tmp_path / "nonexistent.json")
        assert store.ids() == []
