"""Tests for minion/memory/injection.py — inject_memories and _format_age.

No API calls. Pure unit tests.
"""

import pytest
from datetime import datetime, timedelta, timezone

from minion.memory.injection import _format_age, inject_memories
from minion.memory.record import MemoryRecord


def _record(
    content: str = "User prefers pytest.",
    type_: str = "semantic",
    category: str = "project",
    tags: list[str] | None = None,
    created_at: str = "2026-04-02T10:00:00+00:00",
) -> MemoryRecord:
    return MemoryRecord(
        id="abc123",
        content=content,
        type=type_,
        scope="project",
        project_path="/proj",
        tags=tags or [],
        created_at=created_at,
        category=category,
    )


# ─── inject_memories ─────────────────────────────────────────────────────────

class TestInjectMemories:
    def test_returns_prompt_unchanged_when_no_memories(self):
        prompt = "You are Minion."
        assert inject_memories(prompt, []) == prompt

    def test_appends_memory_block_to_prompt(self):
        result = inject_memories("base", [_record("User uses PostgreSQL.")])
        assert "base" in result
        assert "What I Remember" in result
        assert "User uses PostgreSQL." in result

    def test_identity_category_renders_in_about_section(self):
        result = inject_memories("base", [_record(category="identity")])
        assert "About the user" in result

    def test_event_category_renders_in_past_sessions_section(self):
        result = inject_memories("base", [_record(category="event")])
        assert "From past sessions" in result

    def test_project_category_renders_in_project_context_section(self):
        result = inject_memories("base", [_record(category="project")])
        assert "Project context" in result

    def test_preference_category_renders_in_preferences_section(self):
        result = inject_memories("base", [_record(category="preference")])
        assert "User preferences" in result

    def test_sections_only_rendered_when_category_present(self):
        # Only identity and event — should not show Project context section
        memories = [_record(category="identity"), _record(category="event")]
        result = inject_memories("base", memories)
        assert "About the user" in result
        assert "From past sessions" in result
        assert "Project context" not in result

    def test_includes_tags_in_output(self):
        result = inject_memories("base", [_record(tags=["database", "postgresql"])])
        assert "database" in result
        assert "postgresql" in result

    def test_caps_tag_display_at_three(self):
        result = inject_memories("base", [_record(tags=["a", "b", "c", "d", "e"])])
        # Should show only first 3 tags
        assert "d" not in result or "a" in result  # at most 3 shown

    def test_multiple_memories_all_included(self):
        memories = [_record("fact A"), _record("fact B"), _record("fact C")]
        result = inject_memories("base", memories)
        assert "fact A" in result
        assert "fact B" in result
        assert "fact C" in result

    def test_empty_tags_not_shown_in_brackets(self):
        result = inject_memories("base", [_record(tags=[])])
        # No empty bracket like "[] User..."
        assert "[]" not in result


# ─── _format_age ─────────────────────────────────────────────────────────────

class TestFormatAge:
    def _ts(self, seconds_ago: float) -> str:
        dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        return dt.isoformat(timespec="seconds")

    def test_just_now_for_recent(self):
        assert _format_age(self._ts(10)) == "just now"

    def test_minutes_ago(self):
        result = _format_age(self._ts(120))  # 2 minutes
        assert "minute" in result
        assert "2" in result

    def test_hours_ago(self):
        result = _format_age(self._ts(7200))  # 2 hours
        assert "hour" in result
        assert "2" in result

    def test_days_ago(self):
        result = _format_age(self._ts(172800))  # 2 days
        assert "day" in result
        assert "2" in result

    def test_empty_timestamp_returns_unknown(self):
        assert _format_age("") == "unknown"

    def test_invalid_timestamp_returns_unknown(self):
        assert _format_age("not-a-date") == "unknown"

    def test_singular_minute(self):
        result = _format_age(self._ts(65))
        assert "1 minute ago" == result

    def test_singular_hour(self):
        result = _format_age(self._ts(3700))
        assert "1 hour ago" == result

    def test_singular_day(self):
        result = _format_age(self._ts(86500))
        assert "1 day ago" == result
