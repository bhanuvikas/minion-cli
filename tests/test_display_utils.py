"""Tests for minion/display_utils.py — format_tool_args, _trunc, tool_slot_header_frags, apply_slot_event.

No API calls. No filesystem operations. Pure unit tests.
"""

import pytest

from minion.display_utils import _trunc, apply_slot_event, format_tool_args, tool_name_style, tool_slot_header_frags


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


# ─── tool_slot_header_frags ───────────────────────────────────────────────────

class TestToolSlotHeaderFrags:
    def _text(self, frags):
        """Concatenate all text from fragment list."""
        return "".join(t for _, t in frags)

    def _styles(self, frags):
        """Return list of styles from fragment list."""
        return [s for s, _ in frags]

    def test_returns_list_of_tuples(self):
        frags = tool_slot_header_frags("read_file", {})
        assert isinstance(frags, list)
        assert all(isinstance(f, tuple) and len(f) == 2 for f in frags)

    def test_icon_is_first_fragment(self):
        frags = tool_slot_header_frags("read_file", {})
        _, icon_text = frags[0]
        assert "⚙" in icon_text

    def test_icon_uses_bold_yellow(self):
        frags = tool_slot_header_frags("read_file", {})
        icon_style, _ = frags[0]
        assert "bold" in icon_style
        assert "#FFD700" in icon_style

    def test_tool_name_in_second_fragment(self):
        frags = tool_slot_header_frags("read_file", {})
        _, name_text = frags[1]
        assert name_text == "read_file"

    def test_tool_name_is_bold(self):
        frags = tool_slot_header_frags("read_file", {})
        name_style, _ = frags[1]
        assert "bold" in name_style

    def test_write_file_name_uses_yellow_color(self):
        frags = tool_slot_header_frags("write_file", {})
        name_style, _ = frags[1]
        assert "#FFD700" in name_style

    def test_short_string_value_uses_single_quotes(self):
        frags = tool_slot_header_frags("read_file", {"path": "foo.py"})
        combined = self._text(frags)
        assert "'foo.py'" in combined

    def test_long_string_value_truncated_with_double_quotes(self):
        long_val = "x" * 60
        frags = tool_slot_header_frags("read_file", {"path": long_val})
        combined = self._text(frags)
        assert "…" in combined
        assert '"' in combined

    def test_value_uses_blue_style(self):
        frags = tool_slot_header_frags("read_file", {"path": "foo.py"})
        value_style = frags[-1][0]
        assert "#1E90FF" in value_style

    def test_key_label_uses_silver_style(self):
        frags = tool_slot_header_frags("read_file", {"path": "foo.py"})
        key_style = frags[-2][0]
        assert "#C0C0C0" in key_style

    def test_content_key_suppressed(self):
        frags = tool_slot_header_frags("write_file", {"path": "f.py", "content": "big blob"})
        combined = self._text(frags)
        assert "content" not in combined
        assert "f.py" in combined

    def test_old_string_suppressed(self):
        frags = tool_slot_header_frags("edit_file", {"path": "f.py", "old_string": "a", "new_string": "b"})
        combined = self._text(frags)
        assert "old_string" not in combined
        assert "new_string" not in combined
        assert "f.py" in combined

    def test_newlines_replaced_in_value(self):
        frags = tool_slot_header_frags("run_shell", {"command": "line1\nline2"})
        combined = self._text(frags)
        assert "\n" not in combined
        assert "↵" in combined

    def test_integer_value_uses_repr(self):
        frags = tool_slot_header_frags("run_shell", {"timeout": 30})
        combined = self._text(frags)
        assert "30" in combined

    def test_empty_inputs_returns_just_icon_and_name(self):
        frags = tool_slot_header_frags("todo_read", {})
        assert len(frags) == 2
        assert "⚙" in frags[0][1]
        assert frags[1][1] == "todo_read"

    def test_portable_styles_no_class_names(self):
        frags = tool_slot_header_frags("write_file", {"path": "f.py"})
        for style, _ in frags:
            assert not style.startswith("class:"), f"Found class: style {style!r}"


# ─── tool_name_style ──────────────────────────────────────────────────────────

class TestToolNameStyle:
    def test_returns_bold_for_unknown_tool(self):
        assert tool_name_style("unknown_tool") == "bold"

    def test_write_file_is_bold_yellow(self):
        style = tool_name_style("write_file")
        assert "bold" in style
        assert "#FFD700" in style

    def test_edit_file_is_bold_yellow(self):
        style = tool_name_style("edit_file")
        assert "#FFD700" in style

    def test_run_shell_is_bold_red(self):
        style = tool_name_style("run_shell")
        assert "red" in style

    def test_web_fetch_is_bold_red(self):
        style = tool_name_style("web_fetch")
        assert "red" in style

    def test_no_trailing_space_for_unknown(self):
        style = tool_name_style("read_file")
        assert not style.endswith(" ")


# ─── apply_slot_event ─────────────────────────────────────────────────────────

class TestApplySlotEvent:
    def _state(self) -> dict:
        return {"status": "pending"}

    def test_running_sets_status(self):
        s = self._state()
        apply_slot_event(s, "running")
        assert s["status"] == "running"

    def test_complete_sets_status_latency_preview(self):
        s = self._state()
        apply_slot_event(s, "complete", latency_ms=1234, preview="done text")
        assert s["status"] == "complete"
        assert s["latency_ms"] == 1234
        assert s["preview"] == "done text"

    def test_complete_defaults_latency_and_preview(self):
        s = self._state()
        apply_slot_event(s, "complete")
        assert s["latency_ms"] == 0
        assert s["preview"] == ""

    def test_error_sets_status_and_error(self):
        s = self._state()
        apply_slot_event(s, "error", error="something went wrong")
        assert s["status"] == "error"
        assert s["error"] == "something went wrong"

    def test_error_defaults_empty_string(self):
        s = self._state()
        apply_slot_event(s, "error")
        assert s["error"] == ""

    def test_tool_call_sets_last_activity(self):
        s = self._state()
        apply_slot_event(s, "tool_call", name="read_file", inputs={"path": "foo.py"})
        assert "last_activity" in s
        assert "read_file" in s["last_activity"]
        assert "↳" in s["last_activity"]

    def test_tool_call_skips_content_key(self):
        s = self._state()
        apply_slot_event(s, "tool_call", name="write_file", inputs={"path": "f.py", "content": "big"})
        assert "content" not in s["last_activity"]
        assert "f.py" in s["last_activity"]

    def test_text_accumulates_in_buffer(self):
        s = self._state()
        apply_slot_event(s, "text", text="hello ")
        apply_slot_event(s, "text", text="world")
        assert "last_activity" in s
        assert "·" in s["last_activity"]
        assert "hello" in s["last_activity"]

    def test_text_buffer_capped_at_200(self):
        s = self._state()
        apply_slot_event(s, "text", text="x" * 300)
        assert len(s["_text_buf"]) <= 200

    def test_parallel_sub_start_sets_sub_activities(self):
        s = self._state()
        tools = [{"key": "k1", "name": "read_file", "inputs": {"path": "a.py"}}]
        apply_slot_event(s, "parallel_sub_start", tools=tools)
        assert len(s["sub_activities"]) == 1
        assert s["sub_activities"][0]["key"] == "k1"
        assert s["sub_activities"][0]["done"] is False
        assert "↳" in s["sub_activities"][0]["text"]

    def test_parallel_sub_done_marks_correct_item(self):
        s = self._state()
        tools = [
            {"key": "k1", "name": "read_file", "inputs": {}},
            {"key": "k2", "name": "glob",      "inputs": {}},
        ]
        apply_slot_event(s, "parallel_sub_start", tools=tools)
        apply_slot_event(s, "parallel_sub_done", key="k1")
        assert s["sub_activities"][0]["done"] is True
        assert s["sub_activities"][1]["done"] is False

    def test_parallel_sub_done_unknown_key_is_noop(self):
        s = self._state()
        apply_slot_event(s, "parallel_sub_start", tools=[{"key": "k1", "name": "x", "inputs": {}}])
        apply_slot_event(s, "parallel_sub_done", key="unknown")
        assert s["sub_activities"][0]["done"] is False

    def test_parallel_sub_clear_empties_list(self):
        s = self._state()
        apply_slot_event(s, "parallel_sub_start", tools=[{"key": "k1", "name": "x", "inputs": {}}])
        apply_slot_event(s, "parallel_sub_clear")
        assert s["sub_activities"] == []

    def test_unknown_event_is_noop(self):
        s = self._state()
        apply_slot_event(s, "unknown_event", foo="bar")
        assert s == {"status": "pending"}  # unchanged
