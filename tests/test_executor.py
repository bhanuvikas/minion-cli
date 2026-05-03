"""Tests for minion/tools/executor.py — dispatch, dry-run, and confirmation logic."""

import pytest
from unittest.mock import MagicMock, patch

from minion.llm.base import ToolUseBlock
from minion.tools.executor import ToolExecutor


def _block(name: str, **inputs) -> ToolUseBlock:
    return ToolUseBlock(id="toolu_test", name=name, input=inputs)


# ─── Dry-run ──────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_does_not_call_implementation(self, tmp_path):
        executor = ToolExecutor(dry_run=True)
        block = _block("read_file", path=str(tmp_path / "nope.txt"))
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_result"):
            result = executor.execute(block)
        assert result == "[dry-run: tool not executed]"

    def test_dry_run_skips_confirmation_for_dangerous_tool(self):
        executor = ToolExecutor(dry_run=True)
        block = _block("run_shell", command="rm -rf /")
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_result"), \
             patch("minion.tools.executor.questionary") as mock_q:
            executor.execute(block)
        # confirm() must never be called in dry-run mode
        mock_q.confirm.assert_not_called()


# ─── Confirmation ─────────────────────────────────────────────────────────────

class TestConfirmation:
    def test_safe_tool_skips_confirmation(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hi")
        executor = ToolExecutor(dry_run=False)
        block = _block("read_file", path=str(f))
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_result"), \
             patch("minion.tools.executor.questionary") as mock_q:
            executor.execute(block)
        mock_q.confirm.assert_not_called()

    def test_dangerous_tool_confirmed_executes(self, tmp_path):
        executor = ToolExecutor(dry_run=False)
        path = str(tmp_path / "out.txt")
        block = _block("write_file", path=path, content="banana")
        mock_confirm = MagicMock()
        mock_confirm.ask.return_value = True
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_result"), \
             patch("minion.tools.executor.questionary") as mock_q:
            mock_q.confirm.return_value = mock_confirm
            result = executor.execute(block)
        assert "Wrote" in result
        assert (tmp_path / "out.txt").read_text() == "banana"

    def test_dangerous_tool_declined_does_not_execute(self):
        executor = ToolExecutor(dry_run=False)
        block = _block("run_shell", command="echo banana")
        mock_select = MagicMock()
        mock_select.ask.return_value = "No"
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_result"), \
             patch("minion.tools.executor.questionary") as mock_q:
            mock_q.select.return_value = mock_select
            result = executor.execute(block)
        assert result == "User declined tool execution."


# ─── Dispatch ─────────────────────────────────────────────────────────────────

class TestDispatch:
    def test_unknown_tool_returns_error(self):
        executor = ToolExecutor(dry_run=False)
        block = _block("teleport", destination="moon")
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_error"):
            result = executor.execute(block)
        assert "Error" in result
        assert "teleport" in result

    def test_dispatches_read_file(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_text("bello")
        executor = ToolExecutor(dry_run=False)
        block = _block("read_file", path=str(f))
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_result"):
            result = executor.execute(block)
        assert "bello" in result  # content present (output now includes line numbers)

    def test_dispatches_list_directory(self, tmp_path):
        (tmp_path / "file.txt").write_text("x")
        executor = ToolExecutor(dry_run=False)
        block = _block("list_directory", path=str(tmp_path))
        with patch("minion.tools.executor.print_tool_call"), \
             patch("minion.tools.executor.print_tool_result"):
            result = executor.execute(block)
        assert "file.txt" in result
