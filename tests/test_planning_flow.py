"""Flow tests for the planning pipeline.

Tests verify the key integration points of the planning system:
  create_plan() → save_plan() → load_plan() → execute_plan()

Each phase has existing unit tests in test_planner.py. These tests cover the
transitions between phases — the handoffs that could silently break if the
API between storage, creation, and execution changes.

All LLM calls mocked. File I/O uses tmp_path + monkeypatch.chdir.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minion.llm.base import StreamComplete, TextChunk, ToolUseBlock
from minion.planner.storage import load_plan, save_plan


# ─── Shared helpers (matching test_planner.py pattern) ───────────────────────

def _text_stream(*texts: str, stop_reason: str = "end_turn"):
    events = [TextChunk(text=t) for t in texts]
    events.append(StreamComplete(
        stop_reason=stop_reason,
        input_tokens=10,
        output_tokens=5,
        model="test-model",
    ))
    return iter(events)


def _make_status_ctx():
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _make_client(plan_text: str = "# Plan\n\n## Goal\nDo the thing.") -> MagicMock:
    client = MagicMock()
    client.stream.return_value = _text_stream(plan_text, stop_reason="end_turn")
    return client


# ─── TestPlanStorageRoundtrip ─────────────────────────────────────────────────

class TestPlanStorageRoundtrip:
    """save_plan → load_plan preserves content exactly."""

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        content = "# My Plan\n\n## Steps\n1. Do this\n2. Do that\n"
        path = save_plan(content, "do stuff")

        assert path.exists()
        assert load_plan(path) == content

    def test_saved_plan_is_in_minion_plans_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = save_plan("content", "some goal")

        assert ".minion" in str(path)
        assert "plans" in str(path)

    def test_multiple_saves_produce_distinct_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p1 = save_plan("first", "same goal")
        p2 = save_plan("second", "same goal")

        assert p1 != p2
        assert p1.exists() and p2.exists()


# ─── TestCreatePlanFlow ───────────────────────────────────────────────────────

class TestCreatePlanFlow:
    """create_plan() produces a PlanResult and writes to disk."""

    def test_create_plan_saves_file_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from minion.planner.creator import create_plan

        client = _make_client("# Implementation Plan\n\n## Goal\nAdd auth.")

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = create_plan("add auth", client)

        assert result is not None
        assert result.path.exists()
        assert result.goal == "add auth"

    def test_create_plan_content_matches_disk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from minion.planner.creator import create_plan

        plan_text = "# My Plan\n\nDo the thing in three steps."
        client = _make_client(plan_text)

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = create_plan("do the thing", client)

        assert result is not None
        on_disk = load_plan(result.path)
        assert plan_text in on_disk

    def test_create_plan_with_tool_exploration_executes_tool(self, tmp_path, monkeypatch):
        """Planner does tool_use on first call, then produces plan on second call."""
        monkeypatch.chdir(tmp_path)
        from minion.planner.creator import create_plan

        tool_block = ToolUseBlock(id="t1", name="list_directory", input={"path": "."})
        call_count = 0

        def _stream_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return iter([tool_block, StreamComplete(
                    stop_reason="tool_use", input_tokens=10, output_tokens=5, model="m"
                )])
            return _text_stream("# Plan\n\nAfter exploring, here is the plan.")

        client = MagicMock()
        client.stream.side_effect = _stream_side

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
            patch("minion.planner.creator.ToolExecutor") as MockExec,
        ):
            mc.status.return_value = _make_status_ctx()
            MockExec.return_value.execute.return_value = "file1.py\nfile2.py"
            result = create_plan("explore and plan", client)

        assert result is not None
        assert client.stream.call_count == 2  # explore + plan
        MockExec.return_value.execute.assert_called_once()


# ─── TestExecutePlanFlow ──────────────────────────────────────────────────────

class TestExecutePlanFlow:
    """execute_plan() injects plan content into system prompt."""

    def test_execute_plan_injects_plan_into_system_prompt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from minion.planner.creator import execute_plan
        from minion.llm.conversation import Conversation
        from minion.repl import ReplState

        plan_text = "## Steps\n1. Do this important step\n2. Do another step"
        plan_path = save_plan(plan_text, "my goal")

        captured_prompt = {}

        def mock_run_prompt(prompt, client, conv, system_prompt, **kwargs):
            captured_prompt["system"] = system_prompt

        conv = Conversation()
        state = ReplState()

        with patch("minion.planner.creator.run_prompt", side_effect=mock_run_prompt):
            execute_plan(plan_path, MagicMock(), conv, "BASE SYSTEM PROMPT", state)

        assert "BASE SYSTEM PROMPT" in captured_prompt["system"]
        assert "Do this important step" in captured_prompt["system"]
        assert "Active Mission Plan" in captured_prompt["system"]

    def test_full_cycle_create_then_execute(self, tmp_path, monkeypatch):
        """End-to-end: create plan → save to disk → execute → plan in prompt."""
        monkeypatch.chdir(tmp_path)
        from minion.planner.creator import create_plan, execute_plan
        from minion.llm.conversation import Conversation
        from minion.repl import ReplState

        unique_text = "## Key Step\nDo the unique thing XYZ-9999."
        client = _make_client(f"# Full Plan\n\n{unique_text}")

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = create_plan("full cycle goal", client)

        assert result is not None

        captured_prompt = {}

        def mock_run_prompt(prompt, client, conv, system_prompt, **kwargs):
            captured_prompt["system"] = system_prompt

        with patch("minion.planner.creator.run_prompt", side_effect=mock_run_prompt):
            execute_plan(result.path, MagicMock(), Conversation(), "BASE", ReplState())

        assert "XYZ-9999" in captured_prompt["system"]
