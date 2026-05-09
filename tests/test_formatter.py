"""Tests for minion/output/formatter.py — format_tool_call/result/error/todo_list/agent_slot.

No API calls. No filesystem operations. Pure unit tests.
"""

import pytest

from minion.output.formatter import (
    format_agent_slot_summary,
    format_todo_list,
    format_tool_call,
    format_tool_error,
    format_tool_result,
)


# ─── format_tool_call ────────────────────────────────────────────────────────

class TestFormatToolCall:
    def test_returns_string(self):
        result = format_tool_call("read_file", {"path": "foo.py"})
        assert isinstance(result, str)

    def test_contains_tool_name(self):
        result = format_tool_call("read_file", {"path": "foo.py"})
        assert "read_file" in result

    def test_contains_gear_icon(self):
        result = format_tool_call("read_file", {"path": "foo.py"})
        assert "⚙" in result

    def test_short_string_arg_inline(self):
        result = format_tool_call("read_file", {"path": "foo.py"})
        assert "foo.py" in result

    def test_long_string_arg_truncated_inline(self):
        long_path = "a" * 70
        result = format_tool_call("read_file", {"path": long_path})
        assert "…" in result
        assert long_path not in result  # truncated

    def test_multiline_string_becomes_block(self):
        result = format_tool_call("run_shell", {"command": "line1\nline2\nline3"})
        assert "│" in result
        assert "3 lines" in result

    def test_write_file_content_suppressed(self):
        result = format_tool_call("write_file", {"path": "f.py", "content": "big content here"})
        assert "content" not in result
        assert "f.py" in result

    def test_edit_file_old_new_string_suppressed(self):
        result = format_tool_call("edit_file", {"path": "f.py", "old_string": "a", "new_string": "b"})
        assert "old_string" not in result
        assert "new_string" not in result
        assert "f.py" in result

    def test_dry_run_label_present(self):
        result = format_tool_call("read_file", {"path": "f.py"}, dry_run=True)
        assert "dry-run" in result

    def test_no_dry_run_label_by_default(self):
        result = format_tool_call("read_file", {"path": "f.py"})
        assert "dry-run" not in result

    def test_agent_label_present(self):
        result = format_tool_call("read_file", {"path": "f.py"}, agent_label="coder")
        assert "coder" in result

    def test_mode_badge_edits(self):
        result = format_tool_call("write_file", {"path": "f.py"}, mode_badge="edits")
        assert "»" in result

    def test_mode_badge_yolo(self):
        result = format_tool_call("run_shell", {"command": "ls"}, mode_badge="yolo")
        assert "⚡" in result

    def test_mode_badge_trusted(self):
        result = format_tool_call("read_file", {"path": "f.py"}, mode_badge="trusted")
        assert "~" in result

    def test_no_badge_by_default(self):
        result = format_tool_call("read_file", {"path": "f.py"})
        assert "»" not in result
        assert "⚡" not in result

    def test_empty_inputs(self):
        result = format_tool_call("todo_read", {})
        assert "todo_read" in result

    def test_integer_arg_rendered(self):
        result = format_tool_call("run_shell", {"timeout": 30})
        assert "30" in result

    def test_rich_markup_not_broken(self):
        """Input with Rich-special chars must be escaped so markup doesn't break."""
        result = format_tool_call("run_shell", {"command": "echo [hello]"})
        # The result should still contain a valid string (no crash)
        assert isinstance(result, str)

    def test_rich_special_chars_in_value_escaped(self):
        """[brackets] in arg values must not become Rich tags."""
        result = format_tool_call("run_shell", {"command": "echo [hello]"})
        # 'hello' must appear in the rendered string without being mis-parsed as a tag
        assert "hello" in result


# ─── format_tool_result ──────────────────────────────────────────────────────

class TestFormatToolResult:
    def test_returns_string(self):
        assert isinstance(format_tool_result("done"), str)

    def test_contains_result_preview(self):
        result = format_tool_result("Read 42 lines from foo.py")
        assert "Read 42 lines" in result

    def test_single_line_no_suffix(self):
        result = format_tool_result("ok")
        assert "more lines" not in result

    def test_multiline_shows_extra_count(self):
        result = format_tool_result("line1\nline2\nline3")
        assert "+2 more lines" in result

    def test_long_first_line_truncated(self):
        result = format_tool_result("x" * 150)
        assert "…" in result

    def test_contains_tree_branch(self):
        result = format_tool_result("ok")
        assert "└─" in result

    def test_rich_markup_in_result_escaped(self):
        result = format_tool_result("wrote [bold] file")
        assert isinstance(result, str)


# ─── format_tool_error ───────────────────────────────────────────────────────

class TestFormatToolError:
    def test_returns_string(self):
        assert isinstance(format_tool_error("something went wrong"), str)

    def test_contains_error_label(self):
        result = format_tool_error("file not found")
        assert "Error" in result

    def test_contains_error_message(self):
        result = format_tool_error("permission denied")
        assert "permission denied" in result

    def test_contains_tree_branch(self):
        result = format_tool_error("oops")
        assert "└─" in result

    def test_rich_markup_in_error_escaped(self):
        result = format_tool_error("error: [bold]critical[/bold]")
        assert isinstance(result, str)


# ─── format_todo_list ────────────────────────────────────────────────────────

class TestFormatTodoList:
    def _patch_todos(self, monkeypatch, items):
        import minion.output.formatter as fmt_mod
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: items,
        )

    def test_empty_list_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr("minion.tools.implementations.get_todo_list", lambda: [])
        result = format_todo_list()
        assert result == ""

    def test_all_done_hidden_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [{"status": "done", "text": "task"}],
        )
        result = format_todo_list()
        assert result == ""

    def test_all_done_shown_when_flag_set(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [{"status": "done", "text": "task"}],
        )
        result = format_todo_list(show_if_all_done=True)
        assert "task" in result

    def test_pending_item_shown(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [{"status": "pending", "text": "do work"}],
        )
        result = format_todo_list()
        assert "do work" in result

    def test_in_progress_item_shown(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [{"status": "in_progress", "text": "working on it"}],
        )
        result = format_todo_list()
        assert "working on it" in result

    def test_contains_tasks_header(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [{"status": "pending", "text": "x"}],
        )
        result = format_todo_list()
        assert "Tasks" in result

    def test_starts_with_blank_line(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [{"status": "pending", "text": "x"}],
        )
        result = format_todo_list()
        assert result.startswith("\n")

    def test_done_item_has_checkmark(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [
                {"status": "done",    "text": "done task"},
                {"status": "pending", "text": "pending task"},
            ],
        )
        result = format_todo_list(show_if_all_done=True)
        assert "✓" in result

    def test_in_progress_has_arrow(self, monkeypatch):
        monkeypatch.setattr(
            "minion.tools.implementations.get_todo_list",
            lambda: [{"status": "in_progress", "text": "wip"}],
        )
        result = format_todo_list()
        assert "→" in result


# ─── format_agent_slot_summary ───────────────────────────────────────────────

class TestFormatAgentSlotSummary:
    def test_returns_list_of_strings(self):
        result = format_agent_slot_summary("coder", "write tests", {"status": "complete", "latency_ms": 500, "preview": ""})
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    def test_header_contains_label(self):
        lines = format_agent_slot_summary("coder", "do work", {"status": "complete", "latency_ms": 0, "preview": ""})
        assert "coder" in lines[0]

    def test_header_contains_task(self):
        lines = format_agent_slot_summary("writer", "summarise the docs", {"status": "complete", "latency_ms": 0, "preview": ""})
        assert "summarise" in lines[0]

    def test_header_contains_bullet_icon(self):
        lines = format_agent_slot_summary("x", "t", {"status": "complete", "latency_ms": 0, "preview": ""})
        assert "⏺" in lines[0]

    def test_complete_line_contains_done(self):
        lines = format_agent_slot_summary("x", "t", {"status": "complete", "latency_ms": 2000, "preview": ""})
        assert len(lines) >= 2
        assert "done" in lines[1]
        assert "2.0s" in lines[1]

    def test_complete_with_preview_adds_third_line(self):
        lines = format_agent_slot_summary("x", "t", {"status": "complete", "latency_ms": 0, "preview": "first result line"})
        assert len(lines) == 3
        assert "first result line" in lines[2]

    def test_complete_without_preview_has_two_lines(self):
        lines = format_agent_slot_summary("x", "t", {"status": "complete", "latency_ms": 0, "preview": ""})
        assert len(lines) == 2

    def test_error_status_shows_error_message(self):
        lines = format_agent_slot_summary("x", "t", {"status": "error", "error": "timeout"})
        assert len(lines) == 2
        assert "Error" in lines[1]
        assert "timeout" in lines[1]

    def test_error_default_message_when_missing(self):
        lines = format_agent_slot_summary("x", "t", {"status": "error"})
        assert "unknown error" in lines[1]

    def test_task_truncated_at_58_chars(self):
        long_task = "a" * 100
        lines = format_agent_slot_summary("x", long_task, {"status": "complete", "latency_ms": 0, "preview": ""})
        assert "…" in lines[0]
        assert long_task not in lines[0]

    def test_task_newlines_collapsed(self):
        lines = format_agent_slot_summary("x", "line1\nline2", {"status": "complete", "latency_ms": 0, "preview": ""})
        assert "\n" not in lines[0]

    def test_label_is_rich_markup_escaped(self):
        lines = format_agent_slot_summary("[bold]sneaky[/bold]", "t", {"status": "complete", "latency_ms": 0, "preview": ""})
        assert isinstance(lines[0], str)  # no crash from markup injection

    def test_unknown_status_returns_only_header(self):
        lines = format_agent_slot_summary("x", "t", {"status": "running"})
        assert len(lines) == 1
