"""Tests for minion/memory/record.py — MemoryRecord serialization and parsing.

No API calls. No filesystem operations beyond tmp_path.
"""

import pytest
from pathlib import Path

from minion.memory.record import MemoryRecord


def _make_record(**kwargs) -> MemoryRecord:
    defaults = dict(
        id="abc123",
        content="User prefers PostgreSQL 15.",
        type="semantic",
        scope="project",
        project_path="/home/user/myproject",
        tags=["database", "postgresql"],
        created_at="2026-04-02T10:30:00",
        superseded_by=None,
    )
    defaults.update(kwargs)
    return MemoryRecord(**defaults)


# ─── to_dict / from_dict ──────────────────────────────────────────────────────

class TestDictRoundTrip:
    def test_to_dict_contains_all_fields(self):
        r = _make_record()
        d = r.to_dict()
        assert d["id"] == "abc123"
        assert d["content"] == "User prefers PostgreSQL 15."
        assert d["type"] == "semantic"
        assert d["scope"] == "project"
        assert d["project_path"] == "/home/user/myproject"
        assert d["tags"] == ["database", "postgresql"]
        assert d["created_at"] == "2026-04-02T10:30:00"
        assert d["superseded_by"] is None

    def test_from_dict_round_trip(self):
        r = _make_record()
        assert MemoryRecord.from_dict(r.to_dict()) == r

    def test_from_dict_handles_missing_optional_fields(self):
        minimal = {
            "id": "x1",
            "content": "fact",
            "type": "episodic",
            "scope": "global",
        }
        r = MemoryRecord.from_dict(minimal)
        assert r.tags == []
        assert r.created_at == ""
        assert r.superseded_by is None
        assert r.project_path is None

    def test_from_dict_preserves_superseded_by(self):
        r = _make_record(superseded_by="def456")
        assert MemoryRecord.from_dict(r.to_dict()).superseded_by == "def456"


# ─── to_file_content / from_file ──────────────────────────────────────────────

class TestFileRoundTrip:
    def test_to_file_content_contains_frontmatter_delimiters(self):
        content = _make_record().to_file_content()
        assert content.startswith("---\n")
        # Two delimiters: opening and closing
        assert content.count("---") >= 2

    def test_to_file_content_includes_all_fields(self):
        r = _make_record()
        content = r.to_file_content()
        assert "id: abc123" in content
        assert "type: semantic" in content
        assert "scope: project" in content
        assert "database, postgresql" in content
        assert "created_at: 2026-04-02T10:30:00" in content
        assert "User prefers PostgreSQL 15." in content

    def test_from_file_round_trip(self, tmp_path):
        r = _make_record()
        p = tmp_path / "abc123.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        loaded = MemoryRecord.from_file(p)
        assert loaded == r

    def test_from_file_none_project_path(self, tmp_path):
        r = _make_record(scope="global", project_path=None)
        p = tmp_path / "r.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        loaded = MemoryRecord.from_file(p)
        assert loaded.project_path is None

    def test_from_file_none_superseded_by(self, tmp_path):
        r = _make_record(superseded_by=None)
        p = tmp_path / "r.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        assert MemoryRecord.from_file(p).superseded_by is None

    def test_from_file_with_superseded_by(self, tmp_path):
        r = _make_record(superseded_by="newer999")
        p = tmp_path / "r.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        assert MemoryRecord.from_file(p).superseded_by == "newer999"

    def test_from_file_empty_tags(self, tmp_path):
        r = _make_record(tags=[])
        p = tmp_path / "r.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        assert MemoryRecord.from_file(p).tags == []

    def test_from_file_multiline_content(self, tmp_path):
        r = _make_record(content="Line one.\nLine two.\nLine three.")
        p = tmp_path / "r.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        loaded = MemoryRecord.from_file(p)
        assert "Line one." in loaded.content
        assert "Line two." in loaded.content

    def test_from_file_invalid_format_raises(self, tmp_path):
        p = tmp_path / "bad.md"
        p.write_text("no frontmatter here", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid memory file format"):
            MemoryRecord.from_file(p)

    def test_episodic_type_preserved(self, tmp_path):
        r = _make_record(type="episodic")
        p = tmp_path / "r.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        assert MemoryRecord.from_file(p).type == "episodic"

    def test_global_scope_preserved(self, tmp_path):
        r = _make_record(scope="global", project_path=None)
        p = tmp_path / "r.md"
        p.write_text(r.to_file_content(), encoding="utf-8")
        assert MemoryRecord.from_file(p).scope == "global"
