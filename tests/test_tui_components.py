"""Unit tests for TUI component state machines.

All tests here are pure Python — no Textual runtime, no real TTY.
Covers:
  - ConversationBuffer  (get_streaming_markup, append_*, stream_chunk, finalize_turn)
  - SlotsManager        (pre_register, make_callback, get_rich_text, clear)
  - StatusBar           (get_rich_markup)
  - PermissionPanel     (get_rich_markup, confirm_by_index, move_cursor, deny)
  - _diff_lines_for_panel (returns str, not list)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from rich.text import Text


# ── ConversationBuffer ────────────────────────────────────────────────────────

class TestConversationBufferStreamingMarkup:
    def setup_method(self):
        from minion.tui.conversation import ConversationBuffer
        self.buf = ConversationBuffer()

    def test_idle_returns_single_space(self):
        assert self.buf.get_streaming_markup() == " "

    def test_thinking_returns_blue_animation_markup(self):
        self.buf.set_thinking(True)
        markup = self.buf.get_streaming_markup()
        assert "[bold #1E90FF]" in markup
        assert "[italic #1E90FF]" in markup

    def test_streaming_with_text_shows_minion_prefix(self):
        self.buf._is_streaming = True
        self.buf._streaming_text = "Hello world"
        markup = self.buf.get_streaming_markup()
        assert "▌ minion ›" in markup

    def test_streaming_with_text_includes_content(self):
        self.buf._is_streaming = True
        self.buf._streaming_text = "Hello world"
        markup = self.buf.get_streaming_markup()
        assert "Hello" in markup

    def test_streaming_empty_text_shows_ellipsis(self):
        self.buf._is_streaming = True
        self.buf._streaming_text = ""
        markup = self.buf.get_streaming_markup()
        assert "…" in markup

    def test_set_thinking_false_returns_single_space(self):
        self.buf.set_thinking(True)
        self.buf.set_thinking(False)
        assert self.buf.get_streaming_markup() == " "

    def test_stream_chunk_accumulates(self):
        self.buf._is_streaming = True
        self.buf.stream_chunk("Hello")
        self.buf.stream_chunk(" World")
        assert self.buf._streaming_text == "Hello World"

    def test_finalize_turn_clears_streaming_text(self):
        writes: list[str] = []
        self.buf.set_callbacks(write_ansi_fn=writes.append, refresh_fn=lambda: None)
        self.buf._is_streaming = True
        self.buf._streaming_text = "Some text"
        self.buf.finalize_turn()
        assert self.buf._streaming_text == ""
        assert not self.buf._is_streaming

    def test_finalize_turn_with_text_calls_write_fn(self):
        writes: list[str] = []
        self.buf.set_callbacks(write_ansi_fn=writes.append, refresh_fn=lambda: None)
        self.buf._is_streaming = True
        self.buf._streaming_text = "Response text"
        self.buf.finalize_turn()
        assert len(writes) == 1

    def test_finalize_turn_empty_text_does_not_write(self):
        writes: list[str] = []
        self.buf.set_callbacks(write_ansi_fn=writes.append, refresh_fn=lambda: None)
        self.buf._is_streaming = True
        self.buf._streaming_text = ""
        self.buf.finalize_turn()
        assert len(writes) == 0

    def test_append_user_calls_write_fn(self):
        writes: list[str] = []
        self.buf.set_callbacks(write_ansi_fn=writes.append, refresh_fn=lambda: None)
        self.buf.append_user("hello user")
        assert len(writes) == 1

    def test_append_system_calls_write_fn(self):
        writes: list[str] = []
        self.buf.set_callbacks(write_ansi_fn=writes.append, refresh_fn=lambda: None)
        self.buf.append_system("[bold]system message[/]")
        assert len(writes) == 1

    def test_mark_printed_sets_flag(self):
        self.buf.mark_printed()
        assert self.buf._had_external_print

    def test_clear_resets_all_state(self):
        self.buf._streaming_text     = "text"
        self.buf._is_thinking        = True
        self.buf._had_external_print = True
        self.buf._last_was_assistant = True
        self.buf.clear()
        assert self.buf._streaming_text     == ""
        assert not self.buf._is_thinking
        assert not self.buf._had_external_print
        assert not self.buf._last_was_assistant

    def test_set_width_clamps_minimum(self):
        self.buf.set_width(10)
        assert self.buf._width >= 40

    def test_is_streaming_true_when_thinking(self):
        self.buf.set_thinking(True)
        assert self.buf.is_streaming

    def test_is_streaming_false_when_idle(self):
        assert not self.buf.is_streaming


# ── SlotsManager ──────────────────────────────────────────────────────────────

class TestSlotsManager:
    def setup_method(self):
        from minion.tui.slots import SlotsManager
        self.posted: list = []
        self.mgr = SlotsManager(post_message_fn=self.posted.append)

    def _slot(self, key="k1", tool_name="write_file", inputs=None, label=None):
        from minion.output.base import SlotSpec
        return SlotSpec(key=key, tool_name=tool_name, inputs=inputs or {}, label=label)

    def test_empty_get_rich_text_is_empty_text(self):
        result = self.mgr.get_rich_text()
        assert isinstance(result, Text)
        assert str(result) == ""

    def test_is_not_visible_when_empty(self):
        assert not self.mgr.is_visible

    def test_pre_register_makes_visible(self):
        self.mgr.pre_register([self._slot()])
        assert self.mgr.is_visible

    def test_make_callback_posts_slots_updated_message(self):
        from minion.tui.messages import SlotsUpdated
        self.mgr.pre_register([self._slot()])
        cb = self.mgr.make_callback("k1")
        cb("running")
        assert len(self.posted) == 1
        assert isinstance(self.posted[0], SlotsUpdated)

    def test_running_event_sets_status(self):
        self.mgr.pre_register([self._slot()])
        cb = self.mgr.make_callback("k1")
        cb("running")
        results = self.mgr.slot_results()
        assert results[0]["status"] == "running"

    def test_complete_event_sets_status(self):
        self.mgr.pre_register([self._slot()])
        cb = self.mgr.make_callback("k1")
        cb("complete", latency_ms=500, preview="done")
        results = self.mgr.slot_results()
        assert results[0]["status"] == "complete"
        assert results[0]["latency_ms"] == 500

    def test_error_event_sets_status(self):
        self.mgr.pre_register([self._slot()])
        cb = self.mgr.make_callback("k1")
        cb("error", error="something went wrong")
        results = self.mgr.slot_results()
        assert results[0]["status"] == "error"
        assert "something went wrong" in results[0]["error"]

    def test_clear_removes_all_slots(self):
        self.mgr.pre_register([self._slot()])
        self.mgr.clear()
        assert not self.mgr.is_visible
        assert self.mgr.get_rich_text() == Text()

    def test_get_rich_text_with_pending_slot_contains_tool_name(self):
        self.mgr.pre_register([self._slot(tool_name="read_file", inputs={"path": "/f.py"})])
        result = self.mgr.get_rich_text()
        assert "read_file" in str(result)

    def test_get_rich_text_with_running_slot_shows_running(self):
        self.mgr.pre_register([self._slot()])
        cb = self.mgr.make_callback("k1")
        cb("running")
        result = str(self.mgr.get_rich_text())
        assert "running" in result

    def test_get_rich_text_with_complete_slot_shows_done(self):
        self.mgr.pre_register([self._slot()])
        cb = self.mgr.make_callback("k1")
        cb("complete", latency_ms=100, preview="wrote file")
        result = str(self.mgr.get_rich_text())
        assert "done" in result

    def test_multiple_slots_all_rendered(self):
        self.mgr.pre_register([self._slot("k1", "read_file"), self._slot("k2", "write_file")])
        result = str(self.mgr.get_rich_text())
        assert "read_file" in result
        assert "write_file" in result

    def test_unknown_key_callback_is_no_op(self):
        cb = self.mgr.make_callback("nonexistent")
        cb("running")   # must not raise
        assert not self.mgr.is_visible

    def test_slot_results_preserves_insertion_order(self):
        self.mgr.pre_register([self._slot("a", "tool_a"), self._slot("b", "tool_b")])
        results = self.mgr.slot_results()
        assert results[0]["tool_name"] == "tool_a"
        assert results[1]["tool_name"] == "tool_b"


# ── StatusBar ─────────────────────────────────────────────────────────────────

class TestStatusBar:
    def setup_method(self):
        from minion.tui.status import StatusBar
        self.StatusBar = StatusBar

    def test_model_name_appears_in_markup(self):
        bar = self.StatusBar(model_name="claude-test")
        assert "claude-test" in bar.get_rich_markup()

    def test_update_session_model_replaces_name(self):
        bar = self.StatusBar(model_name="old-model")
        bar.update_session(model="new-model")
        markup = bar.get_rich_markup()
        assert "new-model" in markup
        assert "old-model" not in markup

    def test_memory_enabled_shows_green_dot(self):
        bar = self.StatusBar(model_name="m")
        bar.update_session(memory=True)
        markup = bar.get_rich_markup()
        assert "● memory" in markup
        assert "#4CAF50" in markup

    def test_memory_disabled_shows_empty_circle(self):
        bar = self.StatusBar(model_name="m")
        bar.update_session(memory=False)
        assert "○ memory" in bar.get_rich_markup()

    def test_project_name_appears(self):
        bar = self.StatusBar(model_name="m")
        bar.update_session(project="my-project")
        assert "my-project" in bar.get_rich_markup()

    def test_version_appears_with_prefix(self):
        bar = self.StatusBar(model_name="m")
        bar.update_session(version="1.2.3")
        assert "v1.2.3" in bar.get_rich_markup()

    def test_agent_count_plural(self):
        bar = self.StatusBar(model_name="m")
        bar.update_session(agents=3)
        assert "3 agents" in bar.get_rich_markup()

    def test_agent_count_singular(self):
        bar = self.StatusBar(model_name="m")
        bar.update_session(agents=1)
        markup = bar.get_rich_markup()
        assert "1 agent" in markup
        assert "1 agents" not in markup

    def test_long_model_name_truncated_with_ellipsis(self):
        bar = self.StatusBar(model_name="a" * 30)
        assert "…" in bar.get_rich_markup()

    def test_cwd_shortens_home_to_tilde(self):
        import os
        home = os.path.expanduser("~")
        bar = self.StatusBar(model_name="m")
        bar.update_session(cwd=home + "/Documents/my-project")
        assert "~" in bar.get_rich_markup()

    def test_set_width_affects_markup_length(self):
        bar = self.StatusBar(model_name="m", width=80)
        m80 = bar.get_rich_markup()
        bar.set_width(200)
        m200 = bar.get_rich_markup()
        assert len(m200) >= len(m80)

    def test_no_agents_when_zero(self):
        bar = self.StatusBar(model_name="m")
        bar.update_session(agents=0)
        markup = bar.get_rich_markup()
        assert "agent" not in markup

    def test_set_model_updates_displayed_name(self):
        bar = self.StatusBar(model_name="old")
        bar.set_model("new-model")
        assert "new-model" in bar.get_rich_markup()


# ── PermissionPanel ───────────────────────────────────────────────────────────

class TestPermissionPanel:
    def _make_panel(self):
        from minion.tui.permission import PermissionPanel
        return PermissionPanel(app_ref=MagicMock())

    def _pending(self, name="run_shell", inputs=None):
        from minion.tui.permission import PermissionRequest
        return PermissionRequest(name=name, inputs=inputs or {})

    def test_is_not_visible_when_idle(self):
        panel = self._make_panel()
        assert not panel.is_visible

    def test_markup_empty_when_idle(self):
        panel = self._make_panel()
        assert panel.get_rich_markup() == ""

    def test_markup_shows_tool_name(self):
        panel = self._make_panel()
        panel._pending = self._pending("write_file", {"path": "/tmp/f.py"})
        assert "write_file" in panel.get_rich_markup()

    def test_markup_shows_all_four_scope_options(self):
        panel = self._make_panel()
        panel._pending = self._pending()
        markup = panel.get_rich_markup()
        assert "Yes, once" in markup
        assert "this session" in markup
        assert "this project" in markup
        assert "No" in markup

    def test_markup_shows_cursor_arrow(self):
        panel = self._make_panel()
        panel._pending = self._pending()
        assert "❯" in panel.get_rich_markup()

    def test_markup_shows_path_detail_for_write_file(self):
        panel = self._make_panel()
        panel._pending = self._pending("write_file", {"path": "/tmp/test.py"})
        assert "/tmp/test.py" in panel.get_rich_markup()

    def test_markup_shows_command_detail_for_run_shell(self):
        panel = self._make_panel()
        panel._pending = self._pending("run_shell", {"command": "echo hi"})
        assert "echo hi" in panel.get_rich_markup()

    def test_is_visible_true_when_pending_set(self):
        panel = self._make_panel()
        panel._pending = self._pending()
        assert panel.is_visible

    def test_cursor_starts_at_zero(self):
        assert self._make_panel()._cursor == 0

    def test_move_cursor_increments(self):
        panel = self._make_panel()
        panel.move_cursor(1)
        assert panel._cursor == 1

    def test_move_cursor_clamps_at_max_index(self):
        panel = self._make_panel()
        panel.move_cursor(100)
        assert panel._cursor == 3   # 4 options (indices 0-3)

    def test_move_cursor_clamps_at_zero(self):
        panel = self._make_panel()
        panel.move_cursor(-10)
        assert panel._cursor == 0

    def test_confirm_by_index_once_sets_result_true(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel.confirm_by_index(0)
        assert req.result is True
        assert req.scope == "once"

    def test_confirm_by_index_session_sets_scope(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel.confirm_by_index(1)
        assert req.result is True
        assert req.scope == "session"

    def test_confirm_by_index_project_sets_scope(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel.confirm_by_index(2)
        assert req.result is True
        assert req.scope == "project"

    def test_confirm_by_index_no_sets_result_false(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel.confirm_by_index(3)
        assert req.result is False
        assert req.scope == "no"

    def test_confirm_by_index_out_of_range_sets_false(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel.confirm_by_index(99)
        assert req.result is False

    def test_deny_sets_result_false(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel.deny()
        assert req.result is False

    def test_confirm_current_uses_cursor_position(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel._cursor = 1   # "session"
        panel.confirm_current()
        assert req.scope == "session"

    def test_confirm_by_index_sets_event(self):
        panel = self._make_panel()
        req = self._pending()
        panel._pending = req
        panel.confirm_by_index(0)
        assert req.event.is_set()

    def test_confirm_noop_when_no_pending(self):
        panel = self._make_panel()
        panel.confirm_by_index(0)   # must not raise


# ── _diff_lines_for_panel ─────────────────────────────────────────────────────

class TestDiffLinesForPanel:
    def test_run_shell_returns_empty_string(self):
        from minion.tools.executor import _diff_lines_for_panel
        assert _diff_lines_for_panel("run_shell", {"command": "ls"}) == ""

    def test_web_fetch_returns_empty_string(self):
        from minion.tools.executor import _diff_lines_for_panel
        assert _diff_lines_for_panel("web_fetch", {"url": "https://example.com"}) == ""

    def test_unknown_tool_returns_empty_string(self):
        from minion.tools.executor import _diff_lines_for_panel
        assert _diff_lines_for_panel("glob", {"pattern": "*.py"}) == ""

    def test_write_file_new_file_returns_rich_markup(self, tmp_path):
        from minion.tools.executor import _diff_lines_for_panel
        path = str(tmp_path / "new.py")
        result = _diff_lines_for_panel("write_file", {"path": path, "content": "print('hi')\n"})
        assert isinstance(result, str)
        assert len(result) > 0
        # Result is Rich markup (not ANSI) — contains background-color style tags
        assert "on #" in result

    def test_write_file_unchanged_content_returns_empty(self, tmp_path):
        from minion.tools.executor import _diff_lines_for_panel
        p = tmp_path / "same.py"
        content = "unchanged content\n"
        p.write_text(content)
        result = _diff_lines_for_panel("write_file", {"path": str(p), "content": content})
        assert result == ""

    def test_edit_file_returns_rich_markup(self, tmp_path):
        from minion.tools.executor import _diff_lines_for_panel
        p = tmp_path / "f.py"
        p.write_text("old content\n")
        result = _diff_lines_for_panel("edit_file", {
            "path":       str(p),
            "old_string": "old content",
            "new_string": "new content",
        })
        assert isinstance(result, str)
        assert len(result) > 0
        # Rich markup uses background style for removed/added lines
        assert "on #" in result

    def test_return_type_is_str_not_list(self, tmp_path):
        from minion.tools.executor import _diff_lines_for_panel
        path = str(tmp_path / "f.py")
        result = _diff_lines_for_panel("write_file", {"path": path, "content": "x\n"})
        assert isinstance(result, str)

    def test_write_file_large_diff_no_truncation(self, tmp_path):
        from minion.tools.executor import _diff_lines_for_panel
        long_content = "\n".join(f"line {i}" for i in range(100))
        path = str(tmp_path / "big.py")
        result = _diff_lines_for_panel("write_file", {"path": path, "content": long_content})
        # Full diff returned — 100 added lines + 1 hunk header
        assert len(result.splitlines()) >= 100


# ── _console_print_safe TUI routing ──────────────────────────────────────────

class TestConsolePrintSafeTuiRouting:
    """_console_print_safe routes to conversation.append_system when TUI is active."""

    @pytest.mark.asyncio
    async def test_routes_to_tui_conversation_when_active(self):
        from unittest.mock import patch, AsyncMock
        from minion.mcp.manager import _console_print_safe

        mock_app = MagicMock()
        with patch("minion.tui.get_tui_app", return_value=mock_app):
            await _console_print_safe("[bold]hello[/]")

        mock_app.conversation.append_system.assert_called_once_with("[bold]hello[/]")

    @pytest.mark.asyncio
    async def test_falls_back_to_console_when_no_tui(self):
        from unittest.mock import patch
        from minion.mcp.manager import _console_print_safe

        with patch("minion.tui.get_tui_app", return_value=None), \
             patch("minion.mcp.manager.console") as mock_console:
            await _console_print_safe("plain message")

        mock_console.print.assert_called_once_with("plain message")
