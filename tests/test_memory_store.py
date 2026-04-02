"""Tests for minion/memory/store.py — MemoryStore.

All LLM calls mocked. Embedder mocked. File I/O uses tmp_path.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minion.llm.base import LLMResponse
from minion.memory.config import MemoryConfig
from minion.memory.embedder import Embedder
from minion.memory.record import MemoryRecord
from minion.memory.store import MemoryStore
from minion.memory.triggers import AlwaysTrigger, ManualOnlyTrigger


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_client(extract_response: str = "[]", consolidate_response: str | None = None) -> MagicMock:
    client = MagicMock()
    responses = [LLMResponse(content=extract_response, input_tokens=5, output_tokens=5, model="test")]
    if consolidate_response is not None:
        responses.append(LLMResponse(content=consolidate_response, input_tokens=5, output_tokens=5, model="test"))
    client.complete.side_effect = responses
    return client


class _MockEmbedder(Embedder):
    """Returns a deterministic unit vector based on the hash of the text."""
    DIMS = 4

    def embed(self, text: str) -> list[float]:
        h = hash(text) % self.DIMS
        v = [0.0] * self.DIMS
        v[h] = 1.0
        return v

    @property
    def is_available(self) -> bool:
        return True


def _make_store(tmp_path: Path, *, embedder=None, config: MemoryConfig | None = None) -> MemoryStore:
    if config is None:
        config = MemoryConfig(global_memory_dir=tmp_path / "global")
    return MemoryStore(
        config=config,
        project_cwd=tmp_path / "project",
        client=_mock_client(),
        embedder=embedder,
    )


def _record(
    *,
    id: str | None = None,
    content: str = "User uses PostgreSQL.",
    type_: str = "semantic",
    scope: str = "project",
    tags: list[str] | None = None,
    superseded_by: str | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        id=id or str(uuid.uuid4()),
        content=content,
        type=type_,
        scope=scope,
        project_path="/proj" if scope == "project" else None,
        tags=tags or [],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        superseded_by=superseded_by,
    )


# ─── store() ─────────────────────────────────────────────────────────────────

class TestStore:
    def test_creates_project_record_file(self, tmp_path):
        store = _make_store(tmp_path)
        r = _record(scope="project")
        store.store(r)
        expected = tmp_path / "project" / ".minion" / "memory" / "records" / f"{r.id}.md"
        assert expected.exists()

    def test_creates_global_record_file(self, tmp_path):
        store = _make_store(tmp_path)
        r = _record(scope="global")
        store.store(r)
        expected = tmp_path / "global" / "records" / f"{r.id}.md"
        assert expected.exists()

    def test_stored_file_content_is_parseable(self, tmp_path):
        store = _make_store(tmp_path)
        r = _record()
        store.store(r)
        path = tmp_path / "project" / ".minion" / "memory" / "records" / f"{r.id}.md"
        loaded = MemoryRecord.from_file(path)
        assert loaded.id == r.id
        assert loaded.content == r.content

    def test_vector_index_updated_when_embedder_present(self, tmp_path):
        store = _make_store(tmp_path, embedder=_MockEmbedder())
        r = _record(scope="project")
        store.store(r)
        assert r.id in store._project_index.ids()

    def test_no_vector_index_without_embedder(self, tmp_path):
        store = _make_store(tmp_path, embedder=None)
        r = _record(scope="project")
        store.store(r)
        assert store._project_index.ids() == []


# ─── delete() ────────────────────────────────────────────────────────────────

class TestDelete:
    def test_delete_by_exact_id(self, tmp_path):
        store = _make_store(tmp_path)
        r = _record()
        store.store(r)
        count = store.delete(r.id)
        assert count == 1
        path = tmp_path / "project" / ".minion" / "memory" / "records" / f"{r.id}.md"
        assert not path.exists()

    def test_delete_by_fuzzy_text_match(self, tmp_path):
        store = _make_store(tmp_path)
        r = _record(content="User prefers pytest over unittest")
        store.store(r)
        count = store.delete("prefers pytest")
        assert count == 1

    def test_delete_returns_zero_for_no_match(self, tmp_path):
        store = _make_store(tmp_path)
        r = _record(content="unrelated fact")
        store.store(r)
        assert store.delete("totally unrelated query xyz") == 0

    def test_delete_removes_from_vector_index(self, tmp_path):
        store = _make_store(tmp_path, embedder=_MockEmbedder())
        r = _record(scope="project")
        store.store(r)
        store.delete(r.id)
        assert r.id not in store._project_index.ids()


# ─── list_all() ──────────────────────────────────────────────────────────────

class TestListAll:
    def test_returns_all_active_records(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(_record(content="fact A"))
        store.store(_record(content="fact B"))
        records = store.list_all()
        contents = {r.content for r in records}
        assert "fact A" in contents
        assert "fact B" in contents

    def test_excludes_superseded_records(self, tmp_path):
        store = _make_store(tmp_path)
        r = _record(content="old fact", superseded_by="newer-id")
        store.store(r)
        active = store.list_all()
        assert all(rec.superseded_by is None for rec in active)

    def test_keyword_filter(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(_record(content="User uses PostgreSQL"))
        store.store(_record(content="Project uses FastAPI"))
        results = store.list_all(query="postgresql")
        assert len(results) == 1
        assert "PostgreSQL" in results[0].content

    def test_tag_filter(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(_record(content="db fact", tags=["database"]))
        store.store(_record(content="api fact", tags=["api"]))
        results = store.list_all(query="database")
        assert len(results) == 1
        assert results[0].content == "db fact"

    def test_sorted_by_created_at_descending(self, tmp_path):
        store = _make_store(tmp_path)
        r1 = _record(content="older")
        r1.created_at = "2026-01-01T00:00:00"
        r2 = _record(content="newer")
        r2.created_at = "2026-04-01T00:00:00"
        store.store(r1)
        store.store(r2)
        results = store.list_all()
        assert results[0].content == "newer"


# ─── stats() ─────────────────────────────────────────────────────────────────

class TestStats:
    def test_stats_counts_global_and_project(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(_record(scope="global"))
        store.store(_record(scope="project"))
        s = store.stats()
        assert s["global_count"] == 1
        assert s["project_count"] == 1
        assert s["total_count"] == 2

    def test_stats_has_embeddings_false_without_embedder(self, tmp_path):
        store = _make_store(tmp_path, embedder=None)
        assert store.stats()["has_embeddings"] is False

    def test_stats_has_embeddings_true_with_embedder(self, tmp_path):
        store = _make_store(tmp_path, embedder=_MockEmbedder())
        assert store.stats()["has_embeddings"] is True

    def test_stats_excludes_superseded(self, tmp_path):
        store = _make_store(tmp_path)
        store.store(_record(scope="project", superseded_by="other"))
        assert store.stats()["project_count"] == 0


# ─── maybe_extract() ─────────────────────────────────────────────────────────

class TestMaybeExtract:
    def test_extracts_and_stores_when_trigger_fires(self, tmp_path):
        config = MemoryConfig(
            global_memory_dir=tmp_path / "global",
            trigger=AlwaysTrigger(),
        )
        extracted_json = json.dumps([{
            "type": "semantic", "scope": "project",
            "content": "User prefers mypy.", "tags": ["typing"],
        }])
        store = MemoryStore(
            config=config,
            project_cwd=tmp_path / "project",
            client=_mock_client(extract_response=extracted_json),
            embedder=None,
        )
        records = store.maybe_extract("prompt", " ".join(["word"] * 60))
        assert len(records) == 1
        assert records[0].content == "User prefers mypy."
        # File should exist on disk
        assert any(
            (tmp_path / "project" / ".minion" / "memory" / "records").glob("*.md")
        )

    def test_skips_extraction_when_trigger_does_not_fire(self, tmp_path):
        config = MemoryConfig(
            global_memory_dir=tmp_path / "global",
            trigger=ManualOnlyTrigger(),
        )
        store = MemoryStore(
            config=config,
            project_cwd=tmp_path / "project",
            client=_mock_client(),
            embedder=None,
        )
        records = store.maybe_extract("prompt", "short response")
        assert records == []
        store._client.complete.assert_not_called()

    def test_returns_empty_when_llm_finds_nothing(self, tmp_path):
        config = MemoryConfig(
            global_memory_dir=tmp_path / "global",
            trigger=AlwaysTrigger(),
        )
        store = MemoryStore(
            config=config,
            project_cwd=tmp_path / "project",
            client=_mock_client(extract_response="[]"),
            embedder=None,
        )
        records = store.maybe_extract("prompt", " ".join(["word"] * 60))
        assert records == []


# ─── retrieve() keyword fallback ─────────────────────────────────────────────

class TestKeywordRetrieve:
    def test_retrieve_returns_matching_record(self, tmp_path):
        store = _make_store(tmp_path, embedder=None)
        r = _record(content="User loves PostgreSQL databases")
        store.store(r)
        results = store.retrieve("postgresql")
        assert any(rec.id == r.id for rec in results)

    def test_retrieve_returns_empty_for_no_match(self, tmp_path):
        store = _make_store(tmp_path, embedder=None)
        store.store(_record(content="User loves FastAPI"))
        results = store.retrieve("completely unrelated xyz")
        assert results == []

    def test_retrieve_excludes_superseded(self, tmp_path):
        store = _make_store(tmp_path, embedder=None)
        r = _record(content="old postgres fact", superseded_by="newer-id")
        store.store(r)
        results = store.retrieve("postgres")
        assert all(rec.superseded_by is None for rec in results)
