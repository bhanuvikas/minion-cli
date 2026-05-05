"""Flow tests for the memory pipeline.

Tests verify the components that make up the memory pipeline work correctly together:
  retrieve() → inject_memories() → [LLM sees memories] → maybe_extract()

Memory injection happens in repl.py (not runner.py), so these tests exercise
the memory subsystem components at the integration boundary rather than through
the full REPL loop.

All LLM calls mocked. File I/O uses tmp_path. Embedder uses MockEmbedder
(same pattern as test_memory_store.py).
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from minion.llm.base import LLMResponse
from minion.memory.config import MemoryConfig
from minion.memory.embedder import Embedder
from minion.memory.injection import inject_memories
from minion.memory.record import MemoryRecord
from minion.memory.store import MemoryStore
from minion.memory.triggers import AlwaysTrigger, ManualOnlyTrigger


# ─── Shared fixtures ──────────────────────────────────────────────────────────

class _MockEmbedder(Embedder):
    """Deterministic unit-vector embedder — same as test_memory_store.py."""
    DIMS = 8

    def embed(self, text: str) -> list[float]:
        h = hash(text) % self.DIMS
        v = [0.0] * self.DIMS
        v[h] = 1.0
        return v

    @property
    def is_available(self) -> bool:
        return True


def _mock_client(extract_response: str = "[]") -> MagicMock:
    client = MagicMock()
    client.complete.return_value = LLMResponse(
        content=extract_response,
        input_tokens=5,
        output_tokens=5,
        model="test",
    )
    return client


def _make_store(tmp_path: Path, client=None, trigger=None) -> MemoryStore:
    config = MemoryConfig(
        global_memory_dir=tmp_path / "global",
        trigger=AlwaysTrigger() if trigger is None else ManualOnlyTrigger(),
    )
    return MemoryStore(
        config=config,
        project_cwd=tmp_path / "project",
        client=client or _mock_client(),
        embedder=_MockEmbedder(),
    )


def _record(content: str, category: str = "preference", scope: str = "global") -> MemoryRecord:
    return MemoryRecord(
        id=str(uuid.uuid4()),
        content=content,
        type="semantic",
        scope=scope,
        category=category,
        tags=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        project_path=None,
        superseded_by=None,
    )


# ─── TestMemoryInjection ──────────────────────────────────────────────────────

class TestMemoryInjection:
    """Tests for inject_memories() — the formatting step of the pipeline."""

    def test_inject_memories_adds_what_i_remember_block(self):
        memories = [_record("User prefers pytest over unittest", category="preference")]
        result = inject_memories("base prompt", memories)

        assert "## What I Remember" in result
        assert "pytest" in result

    def test_inject_memories_returns_base_unchanged_when_empty(self):
        base = "You are a helpful assistant."
        result = inject_memories(base, [])
        assert result == base

    def test_inject_memories_groups_by_category(self):
        memories = [
            _record("User prefers black formatter", category="preference"),
            _record("This project uses FastAPI", category="project"),
        ]
        result = inject_memories("base", memories)

        assert "User preferences" in result
        assert "Project context" in result

    def test_inject_memories_multiple_memories_all_appear(self):
        memories = [
            _record("Fact one", category="preference"),
            _record("Fact two", category="preference"),
            _record("Fact three", category="preference"),
        ]
        result = inject_memories("base", memories)

        assert "Fact one" in result
        assert "Fact two" in result
        assert "Fact three" in result

    def test_inject_memories_appends_after_base_prompt(self):
        base = "BASE SYSTEM PROMPT"
        memories = [_record("some memory")]
        result = inject_memories(base, memories)

        assert result.startswith(base)
        assert len(result) > len(base)


# ─── TestMemoryStoreFlow ──────────────────────────────────────────────────────

class TestMemoryStoreFlow:
    """Tests for the store + retrieval pipeline."""

    def test_store_and_retrieve_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        record = _record("User prefers PostgreSQL for this project", category="project")
        store.store(record)

        retrieved = store.retrieve("database preferences")

        assert any(r.id == record.id for r in retrieved)

    def test_retrieve_returns_empty_for_empty_store(self, tmp_path):
        store = _make_store(tmp_path)
        result = store.retrieve("anything")
        assert result == []

    def test_preference_category_always_injected(self, tmp_path):
        store = _make_store(tmp_path)
        record = _record("User prefers tabs over spaces", category="preference")
        store.store(record)

        # Preference memories are always injected regardless of query
        retrieved = store.retrieve("completely unrelated query about networking")
        assert any(r.id == record.id for r in retrieved)

    def test_store_then_inject_contains_content(self, tmp_path):
        store = _make_store(tmp_path)
        record = _record("User works in Python 3.12 exclusively", category="preference")
        store.store(record)

        memories = store.retrieve("what language")
        injected = inject_memories("base", memories)

        assert "Python 3.12" in injected

    def test_superseded_record_not_retrieved(self, tmp_path):
        store = _make_store(tmp_path)
        old = _record("User used Python 3.10", category="preference")
        new = _record("User now uses Python 3.12", category="preference")
        old.superseded_by = new.id

        store.store(old)
        store.store(new)

        retrieved = store.retrieve("python version")
        retrieved_ids = {r.id for r in retrieved}

        assert new.id in retrieved_ids
        assert old.id not in retrieved_ids


# ─── TestMemoryExtractionFlow ─────────────────────────────────────────────────

class TestMemoryExtractionFlow:
    """Tests for maybe_extract() — the extraction step of the pipeline."""

    def test_extraction_returns_records_from_llm_response(self, tmp_path):
        import json
        record_json = json.dumps([{
            "content": "User prefers pytest",
            "type": "semantic",
            "scope": "global",
            "category": "preference",
            "tags": ["testing"],
        }])
        client = _mock_client(extract_response=record_json)
        store = _make_store(tmp_path, client=client)

        extracted = store.maybe_extract(
            prompt="what testing framework should I use?",
            response="You should use pytest, it is superior to unittest.",
        )

        assert len(extracted) >= 1
        assert any("pytest" in r.content for r in extracted)

    def test_extraction_returns_empty_for_no_memories(self, tmp_path):
        client = _mock_client(extract_response="[]")
        store = _make_store(tmp_path, client=client)

        extracted = store.maybe_extract(
            prompt="what is 2 + 2?",
            response="4",
        )

        assert extracted == []

    def test_extracted_records_are_persisted(self, tmp_path):
        import json
        record_json = json.dumps([{
            "content": "User's project is called minion-cli",
            "type": "semantic",
            "scope": "global",
            "category": "project",
            "tags": [],
        }])
        client = _mock_client(extract_response=record_json)
        store = _make_store(tmp_path, client=client)

        store.maybe_extract(
            prompt="what is the project name?",
            response="The project is called minion-cli.",
        )

        # Retrieve with a fresh store pointing at the same tmp_path
        store2 = _make_store(tmp_path, client=_mock_client())
        retrieved = store2.retrieve("project name")
        assert any("minion-cli" in r.content for r in retrieved)
