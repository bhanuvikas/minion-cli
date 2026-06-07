"""Tests for minion/tools/confirmation.py — ConfirmationManager.

Covers:
  - diff_lines parameter is now str (not list) — migration correctness
  - TUI path: asyncio.run_coroutine_threadsafe routing to permission.request()
  - Non-TUI path: falls through to _interactive_confirm
  - confirm_async delegates to confirm_sync via asyncio.to_thread
  - set_tui stores app + loop references
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minion.tools.confirmation import ConfirmationManager


# ── Construction ──────────────────────────────────────────────────────────────

class TestConstruction:
    def test_tui_refs_none_initially(self):
        cm = ConfirmationManager()
        assert cm._tui_app  is None
        assert cm._tui_loop is None

    def test_permission_store_stored(self):
        store = MagicMock()
        cm = ConfirmationManager(permission_store=store)
        assert cm._permission_store is store

    def test_no_args_does_not_raise(self):
        ConfirmationManager()


# ── set_tui ───────────────────────────────────────────────────────────────────

class TestSetTui:
    def test_stores_app_reference(self):
        cm = ConfirmationManager()
        app = MagicMock()
        loop = asyncio.new_event_loop()
        try:
            cm.set_tui(app, loop)
            assert cm._tui_app is app
        finally:
            loop.close()

    def test_stores_loop_reference(self):
        cm = ConfirmationManager()
        loop = asyncio.new_event_loop()
        try:
            cm.set_tui(MagicMock(), loop)
            assert cm._tui_loop is loop
        finally:
            loop.close()

    def test_can_be_called_multiple_times(self):
        cm = ConfirmationManager()
        loop = asyncio.new_event_loop()
        try:
            cm.set_tui(MagicMock(), loop)
            app2 = MagicMock()
            cm.set_tui(app2, loop)
            assert cm._tui_app is app2
        finally:
            loop.close()


# ── confirm_sync — TUI path ───────────────────────────────────────────────────

class TestConfirmSyncTuiPath:
    def _make_with_tui(self):
        """Return (cm, mock_app, loop). Caller must loop.close() when done."""
        cm  = ConfirmationManager()
        app = MagicMock()
        loop = asyncio.new_event_loop()
        cm.set_tui(app, loop)
        return cm, app, loop

    def test_calls_run_coroutine_threadsafe(self):
        cm, app, loop = self._make_with_tui()
        try:
            mock_future = MagicMock()
            mock_future.result.return_value = True
            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future) as mock_rct:
                cm.confirm_sync("run_shell", {"command": "ls"})
            mock_rct.assert_called_once()
        finally:
            loop.close()

    def test_passes_correct_loop_to_run_coroutine_threadsafe(self):
        cm, app, loop = self._make_with_tui()
        try:
            mock_future = MagicMock()
            mock_future.result.return_value = False
            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future) as mock_rct:
                cm.confirm_sync("run_shell", {})
            _, passed_loop = mock_rct.call_args[0]
            assert passed_loop is loop
        finally:
            loop.close()

    def test_calls_permission_request_with_name_and_inputs(self):
        cm, app, loop = self._make_with_tui()
        try:
            mock_future = MagicMock()
            mock_future.result.return_value = True
            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
                cm.confirm_sync("write_file", {"path": "/tmp/f.py"})
            app.permission.request.assert_called_once_with(
                "write_file", {"path": "/tmp/f.py"}, diff_lines=""
            )
        finally:
            loop.close()

    def test_passes_str_diff_lines_to_permission_request(self):
        cm, app, loop = self._make_with_tui()
        try:
            mock_future = MagicMock()
            mock_future.result.return_value = True
            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
                cm.confirm_sync("write_file", {}, diff_lines="\x1b[32m+new line\x1b[0m")
            app.permission.request.assert_called_once_with(
                "write_file", {}, diff_lines="\x1b[32m+new line\x1b[0m"
            )
        finally:
            loop.close()

    def test_returns_true_when_future_result_true(self):
        cm, _, loop = self._make_with_tui()
        try:
            mock_future = MagicMock()
            mock_future.result.return_value = True
            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
                result = cm.confirm_sync("run_shell", {})
            assert result is True
        finally:
            loop.close()

    def test_returns_false_when_future_result_false(self):
        cm, _, loop = self._make_with_tui()
        try:
            mock_future = MagicMock()
            mock_future.result.return_value = False
            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
                result = cm.confirm_sync("run_shell", {})
            assert result is False
        finally:
            loop.close()

    def test_does_not_call_interactive_confirm(self):
        cm, _, loop = self._make_with_tui()
        try:
            mock_future = MagicMock()
            mock_future.result.return_value = True
            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future), \
                 patch("minion.tools.executor._interactive_confirm") as mock_ic:
                cm.confirm_sync("run_shell", {})
            mock_ic.assert_not_called()
        finally:
            loop.close()


# ── confirm_sync — non-TUI path ───────────────────────────────────────────────

class TestConfirmSyncNonTuiPath:
    def test_calls_interactive_confirm(self):
        cm = ConfirmationManager()
        with patch("minion.tools.executor._interactive_confirm", return_value=True) as mock_ic, \
             patch("minion.agents.display.get_active_live_display", return_value=None):
            result = cm.confirm_sync("run_shell", {"command": "ls"})
        mock_ic.assert_called_once_with("run_shell", {"command": "ls"}, cm._permission_store)
        assert result is True

    def test_returns_false_when_declined(self):
        cm = ConfirmationManager()
        with patch("minion.tools.executor._interactive_confirm", return_value=False), \
             patch("minion.agents.display.get_active_live_display", return_value=None):
            result = cm.confirm_sync("run_shell", {})
        assert result is False

    def test_accepts_str_diff_lines_without_error(self):
        cm = ConfirmationManager()
        with patch("minion.tools.executor._interactive_confirm", return_value=True), \
             patch("minion.agents.display.get_active_live_display", return_value=None):
            result = cm.confirm_sync("run_shell", {}, diff_lines="")
        assert isinstance(result, bool)

    def test_pauses_live_display_when_active(self):
        cm = ConfirmationManager()
        mock_display = MagicMock()
        with patch("minion.tools.executor._interactive_confirm", return_value=True), \
             patch("minion.agents.display.get_active_live_display", return_value=mock_display):
            cm.confirm_sync("run_shell", {})
        mock_display.pause.assert_called_once()
        mock_display.resume.assert_called_once()

    def test_resumes_display_even_if_confirm_raises(self):
        cm = ConfirmationManager()
        mock_display = MagicMock()
        with patch("minion.tools.executor._interactive_confirm",
                   side_effect=RuntimeError("oops")), \
             patch("minion.agents.display.get_active_live_display", return_value=mock_display):
            with pytest.raises(RuntimeError):
                cm.confirm_sync("run_shell", {})
        mock_display.resume.assert_called_once()

    def test_default_diff_lines_is_empty_str(self):
        """diff_lines default must be str, not list — migration regression guard."""
        import inspect
        sig = inspect.signature(ConfirmationManager.confirm_sync)
        default = sig.parameters["diff_lines"].default
        assert default == ""
        assert isinstance(default, str)


# ── confirm_async ─────────────────────────────────────────────────────────────

class TestConfirmAsync:
    @pytest.mark.asyncio
    async def test_delegates_to_confirm_sync(self):
        cm = ConfirmationManager()
        with patch.object(cm, "confirm_sync", return_value=True) as mock_sync:
            result = await cm.confirm_async("run_shell", {"command": "ls"}, diff_lines="")
        mock_sync.assert_called_once_with("run_shell", {"command": "ls"}, "")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_sync_returns_false(self):
        cm = ConfirmationManager()
        with patch.object(cm, "confirm_sync", return_value=False):
            result = await cm.confirm_async("run_shell", {})
        assert result is False

    @pytest.mark.asyncio
    async def test_default_diff_lines_is_empty_str(self):
        import inspect
        sig = inspect.signature(ConfirmationManager.confirm_async)
        default = sig.parameters["diff_lines"].default
        assert default == ""
        assert isinstance(default, str)

    @pytest.mark.asyncio
    async def test_runs_in_thread_does_not_block_event_loop(self):
        """confirm_async must use asyncio.to_thread so the event loop stays responsive."""
        cm = ConfirmationManager()
        ran_in_thread = []

        def _fake_confirm_sync(name, inputs, diff_lines=""):
            import threading
            # If we're not on the main thread, asyncio.to_thread is working
            ran_in_thread.append(not threading.main_thread() == threading.current_thread())
            return True

        with patch.object(cm, "confirm_sync", side_effect=_fake_confirm_sync):
            await cm.confirm_async("run_shell", {})

        assert ran_in_thread == [True]
