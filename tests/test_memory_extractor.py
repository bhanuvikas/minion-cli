"""Tests for minion/memory/extractor.py — MemoryExtractor.

All LLM calls mocked. No API calls. No filesystem operations.
"""

import json
import pytest
from unittest.mock import MagicMock

from minion.llm.base import LLMResponse
from minion.memory.config import MemoryConfig
from minion.memory.extractor import ConsolidationResult, MemoryExtractor
from minion.memory.record import MemoryRecord


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, input_tokens=10, output_tokens=20, model="test")


def _mock_client(content: str) -> MagicMock:
    client = MagicMock()
    client.complete.return_value = _response(content)
    return client


def _make_record(content: str = "fact", type_: str = "semantic") -> MemoryRecord:
    return MemoryRecord(
        id="abc", content=content, type=type_, scope="project",
        project_path="/proj", tags=[], created_at="2026-04-02T10:00:00",
    )


# ─── extract() ───────────────────────────────────────────────────────────────

class TestExtract:
    def _extractor(self):
        return MemoryExtractor(MemoryConfig())

    def test_returns_records_from_valid_json(self):
        payload = json.dumps([
            {"type": "semantic", "scope": "project", "content": "User uses PostgreSQL.", "tags": ["db"]}
        ])
        client = _mock_client(payload)
        records = self._extractor().extract("prompt", "response", client, "/proj")
        assert len(records) == 1
        assert records[0].content == "User uses PostgreSQL."
        assert records[0].type == "semantic"
        assert records[0].scope == "project"
        assert records[0].tags == ["db"]
        assert records[0].project_path == "/proj"

    def test_returns_empty_list_for_empty_json_array(self):
        client = _mock_client("[]")
        assert self._extractor().extract("p", "r", client) == []

    def test_returns_empty_list_on_malformed_json(self):
        client = _mock_client("not json at all")
        assert self._extractor().extract("p", "r", client) == []

    def test_strips_markdown_code_fences(self):
        payload = '```json\n[{"type":"semantic","scope":"global","content":"prefers pytest","tags":[]}]\n```'
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client)
        assert len(records) == 1
        assert records[0].content == "prefers pytest"

    def test_assigns_uuid_ids(self):
        payload = json.dumps([{"type": "semantic", "scope": "project", "content": "fact", "tags": []}])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client)
        assert len(records[0].id) == 36  # UUID4 format

    def test_global_scope_has_no_project_path(self):
        payload = json.dumps([{"type": "semantic", "scope": "global", "content": "prefers pytest", "tags": []}])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client, project_path="/proj")
        assert records[0].project_path is None

    def test_project_scope_has_project_path(self):
        payload = json.dumps([{"type": "semantic", "scope": "project", "content": "uses postgres", "tags": []}])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client, project_path="/myproject")
        assert records[0].project_path == "/myproject"

    def test_unknown_type_defaults_to_semantic(self):
        payload = json.dumps([{"type": "UNKNOWN", "scope": "project", "content": "fact", "tags": []}])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client)
        assert records[0].type == "semantic"

    def test_unknown_scope_defaults_to_project(self):
        payload = json.dumps([{"type": "semantic", "scope": "WEIRD", "content": "fact", "tags": []}])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client)
        assert records[0].scope == "project"

    def test_skips_items_with_empty_content(self):
        payload = json.dumps([
            {"type": "semantic", "scope": "project", "content": "", "tags": []},
            {"type": "semantic", "scope": "project", "content": "real fact", "tags": []},
        ])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client)
        assert len(records) == 1
        assert records[0].content == "real fact"

    def test_multiple_records_extracted(self):
        payload = json.dumps([
            {"type": "semantic", "scope": "project", "content": "fact A", "tags": []},
            {"type": "episodic", "scope": "global", "content": "fact B", "tags": ["tag"]},
        ])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client)
        assert len(records) == 2

    def test_episodic_type_preserved(self):
        payload = json.dumps([{"type": "episodic", "scope": "project", "content": "fixed bug", "tags": []}])
        client = _mock_client(payload)
        records = self._extractor().extract("p", "r", client)
        assert records[0].type == "episodic"

    def test_uses_complete_not_stream(self):
        client = _mock_client("[]")
        self._extractor().extract("p", "r", client)
        client.stream.assert_not_called()
        client.complete.assert_called_once()


# ─── consolidate() ───────────────────────────────────────────────────────────

class TestConsolidate:
    def _extractor(self):
        return MemoryExtractor(MemoryConfig())

    def test_supersede_a_action(self):
        client = _mock_client(json.dumps({"action": "supersede_a", "merged_content": None}))
        result = self._extractor().consolidate(_make_record("A"), _make_record("B"), client)
        assert result.action == "supersede_a"
        assert result.merged_content is None

    def test_supersede_b_action(self):
        client = _mock_client(json.dumps({"action": "supersede_b", "merged_content": None}))
        result = self._extractor().consolidate(_make_record("A"), _make_record("B"), client)
        assert result.action == "supersede_b"

    def test_keep_both_action(self):
        client = _mock_client(json.dumps({"action": "keep_both", "merged_content": None}))
        result = self._extractor().consolidate(_make_record("A"), _make_record("B"), client)
        assert result.action == "keep_both"

    def test_merge_action_with_content(self):
        client = _mock_client(json.dumps({"action": "merge", "merged_content": "A and B merged"}))
        result = self._extractor().consolidate(_make_record("A"), _make_record("B"), client)
        assert result.action == "merge"
        assert result.merged_content == "A and B merged"

    def test_falls_back_to_keep_both_on_bad_json(self):
        client = _mock_client("not valid json")
        result = self._extractor().consolidate(_make_record("A"), _make_record("B"), client)
        assert result.action == "keep_both"

    def test_falls_back_to_keep_both_on_unknown_action(self):
        client = _mock_client(json.dumps({"action": "WEIRD_ACTION", "merged_content": None}))
        result = self._extractor().consolidate(_make_record("A"), _make_record("B"), client)
        assert result.action == "keep_both"

    def test_uses_complete_not_stream(self):
        client = _mock_client(json.dumps({"action": "keep_both", "merged_content": None}))
        self._extractor().consolidate(_make_record("A"), _make_record("B"), client)
        client.stream.assert_not_called()
        client.complete.assert_called_once()
