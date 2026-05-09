"""Tests for minion/display_utils.py — format_tool_args and _trunc.

No API calls. No filesystem operations. Pure unit tests.
"""

import pytest

from minion.display_utils import _trunc, format_tool_args


# ─── _trunc ──────────────────────────────────────────────────────────────────

class TestTrunc:
    def test_short_string_unchanged(self):
        assert _trunc("hello", 10) == "hello"

    def test_exact_length_unchanged(self):
        assert _trunc("hello", 5) == "hello"

    def test_long_string_truncated_with_ellipsis(self):
        result = _trunc("hello world", 8)
        assert result == "hello w…"
        assert len(result) == 8

    def test_empty_string_unchanged(self):
        assert _trunc("", 5) == ""

    def test_single_char_limit(self):
        result = _trunc("abc", 1)
        assert result == "…"
        assert len(result) == 1


# ─── format_tool_args — empty / basic ────────────────────────────────────────

class TestFormatToolArgsEmpty:
    def test_empty_dict_returns_empty_string(self):
        assert format_tool_args({}) == ""

    def test_single_string_value(self):
        result = format_tool_args({"path": "foo.py"})
        assert result == "path='foo.py'"

    def test_single_int_value(self):
        result = format_tool_args({"count": 42})
        assert "count=" in result
        assert "42" in result

    def test_single_bool_value(self):
        result = format_tool_args({"flag": True})
        assert "flag=" in result


# ─── format_tool_args — newline replacement ───────────────────────────────────

class TestFormatToolArgsNewlines:
    def test_newlines_replaced_with_arrow(self):
        result = format_tool_args({"text": "line1\nline2"})
        assert "↵" in result
        assert "\n" not in result

    def test_newline_replacement_in_expanded_mode(self):
        result = format_tool_args({"text": "a\nb"}, expanded=True)
        assert "↵" in result
        assert "\n" not in result


# ─── format_tool_args — skip keys (normal mode) ──────────────────────────────

class TestFormatToolArgsSkipKeys:
    def test_content_key_skipped_in_normal_mode(self):
        result = format_tool_args({"content": "big blob of text"})
        assert result == ""

    def test_old_string_skipped_in_normal_mode(self):
        result = format_tool_args({"old_string": "x"})
        assert result == ""

    def test_new_string_skipped_in_normal_mode(self):
        result = format_tool_args({"new_string": "y"})
        assert result == ""

    def test_path_shown_when_content_skipped(self):
        result = format_tool_args({"path": "foo.py", "content": "big blob"})
        assert "path='foo.py'" in result
        assert "content" not in result

    def test_path_shown_for_str_replace(self):
        result = format_tool_args({"path": "f.py", "old_string": "a", "new_string": "b"})
        assert "path='f.py'" in result
        assert "old_string" not in result
        assert "new_string" not in result


# ─── format_tool_args — expanded mode includes all keys ──────────────────────

class TestFormatToolArgsExpanded:
    def test_content_included_in_expanded_mode(self):
        result = format_tool_args({"path": "f.py", "content": "hello"}, expanded=True)
        assert "content=" in result

    def test_old_string_included_in_expanded_mode(self):
        result = format_tool_args({"path": "f.py", "old_string": "a"}, expanded=True)
        assert "old_string=" in result

    def test_expanded_limit_is_200(self):
        long_val = "x" * 300
        result = format_tool_args({"key": long_val}, expanded=True)
        # value portion should be truncated to 200 chars (plus quotes and ellipsis)
        assert len(result) < 210

    def test_normal_limit_is_45(self):
        long_val = "x" * 100
        result = format_tool_args({"key": long_val})
        # value should be truncated to 45 chars (plus quotes and ellipsis)
        assert len(result) < 55


# ─── format_tool_args — multi-pair output ────────────────────────────────────

class TestFormatToolArgsMultiPair:
    def test_two_non_skipped_pairs_joined_with_spaces(self):
        result = format_tool_args({"task": "fix bug", "role": "coder"})
        assert "task=" in result
        assert "role=" in result
        assert "  " in result  # pairs joined with double space

    def test_max_three_pairs_returned(self):
        result = format_tool_args({
            "a": "1", "b": "2", "c": "3", "d": "4"
        })
        parts = result.split("  ")
        assert len(parts) <= 3

    def test_expanded_max_three_pairs(self):
        result = format_tool_args(
            {"a": "1", "b": "2", "c": "3", "d": "4"}, expanded=True
        )
        parts = result.split("  ")
        assert len(parts) <= 3


# ─── format_tool_args — consistency between modes ────────────────────────────

class TestFormatToolArgsModes:
    def test_spawn_agent_shows_task_and_role(self):
        inputs = {"task": "write tests", "role": "coder"}
        result = format_tool_args(inputs)
        assert "task=" in result
        assert "role=" in result

    def test_bash_command_shown(self):
        result = format_tool_args({"command": "ls -la"})
        assert "command='ls -la'" == result

    def test_read_file_shows_path(self):
        result = format_tool_args({"path": "src/main.py"})
        assert "path='src/main.py'" == result
