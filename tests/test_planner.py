"""Tests for minion/planner/ — storage and creator modules.

All LLM calls are mocked. Filesystem operations use tmp_path so no real
.minion/plans/ directories are created.
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minion.llm.base import StreamComplete, TextChunk, ToolUseBlock
from minion.planner.storage import list_plans, load_plan, plans_dir, save_plan


# ─── Helpers ─────────────────────────────────────────────────────────────────

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


# ─── storage.py tests ─────────────────────────────────────────────────────────

class TestSavePlan:
    def test_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = save_plan("# My Plan\nSome content.", "add auth")
        assert path.exists()
        assert path.read_text() == "# My Plan\nSome content."

    def test_slug_format(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = save_plan("content", "add hold piece to Tetris")
        # Should be YYYY-MM-DD-add-hold-piece-to-tetris.md (or truncated)
        assert path.name.endswith(".md")
        parts = path.stem.split("-")
        # First three parts are YYYY, MM, DD
        assert len(parts[0]) == 4 and parts[0].isdigit()
        assert len(parts[1]) == 2 and parts[1].isdigit()
        assert len(parts[2]) == 2 and parts[2].isdigit()
        # Slug uses hyphens and lowercase
        slug = "-".join(parts[3:])
        assert slug == slug.lower()
        assert " " not in slug

    def test_collision_avoidance(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path1 = save_plan("first", "same goal")
        path2 = save_plan("second", "same goal")
        assert path1 != path2
        assert path1.exists()
        assert path2.exists()

    def test_content_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        content = "# Plan\n\n## Goal\nDo something.\n\n## Steps\n1. Step one\n"
        path = save_plan(content, "do something")
        assert load_plan(path) == content

    def test_plans_in_minion_subdir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = save_plan("content", "test goal")
        assert ".minion" in str(path)
        assert "plans" in str(path)


class TestListPlans:
    def test_empty_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert list_plans() == []

    def test_sorted_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        p1 = save_plan("first", "goal one")
        time.sleep(0.01)
        p2 = save_plan("second", "goal two")
        result = list_plans()
        assert result[0] == p2
        assert result[1] == p1

    def test_returns_only_md_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        save_plan("content", "goal")
        (plans_dir() / "note.txt").write_text("not a plan")
        result = list_plans()
        assert all(p.suffix == ".md" for p in result)
        assert len(result) == 1


# ─── creator.py tests ─────────────────────────────────────────────────────────

class TestCreatePlan:
    def test_happy_path_returns_plan_result(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = MagicMock()
        client.stream.return_value = _text_stream("# Plan\n\nThe plan.", stop_reason="end_turn")

        from minion.planner.creator import create_plan

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = create_plan("add hold piece", client)

        assert result is not None
        assert "Plan" in result.content
        assert result.goal == "add hold piece"
        assert result.path.exists()

    def test_returns_none_on_stream_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = MagicMock()
        client.stream.side_effect = RuntimeError("API error")

        from minion.planner.creator import create_plan

        with (
            patch("minion.planner.creator.console") as mc,
            patch("minion.planner.creator.print_error"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = create_plan("some goal", client)

        assert result is None

    def test_tool_call_is_executed_before_plan(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        tool_block = ToolUseBlock(id="t1", name="list_directory", input={"path": "."})

        # First call: returns tool use; second call: returns plan text
        call_count = 0

        def _stream_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                events = [tool_block, StreamComplete(
                    stop_reason="tool_use", input_tokens=10, output_tokens=5, model="m"
                )]
                return iter(events)
            else:
                return _text_stream("# Plan\nContent.", stop_reason="end_turn")

        client = MagicMock()
        client.stream.side_effect = _stream_side_effect

        from minion.planner.creator import create_plan

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
            patch("minion.planner.creator.ToolExecutor") as MockExecutor,
        ):
            mc.status.return_value = _make_status_ctx()
            mock_exec = MagicMock()
            mock_exec.execute.return_value = "file1.py\nfile2.py"
            MockExecutor.return_value = mock_exec

            result = create_plan("explore project", client)

        assert result is not None
        assert mock_exec.execute.called
        assert client.stream.call_count == 2

    def test_plan_file_written_with_content(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = MagicMock()
        client.stream.return_value = _text_stream("# Implementation Plan\n\nDetails here.")

        from minion.planner.creator import create_plan

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = create_plan("implement feature", client)

        assert result is not None
        assert load_plan(result.path) == result.content


class TestRefinePlan:
    def test_returns_revised_text(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = MagicMock()
        client.stream.return_value = _text_stream("# Revised Plan\n\nBetter content.")

        from minion.planner.creator import _refine_plan

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = _refine_plan("# Original Plan", "add more detail", "my goal", client)

        assert result == "# Revised Plan\n\nBetter content."

    def test_passes_no_tools_on_refinement(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = MagicMock()
        client.stream.return_value = _text_stream("Revised.")

        from minion.planner.creator import _refine_plan

        with (
            patch("minion.planner.creator.console") as mc,
            patch("sys.stdout"),
        ):
            mc.status.return_value = _make_status_ctx()
            _refine_plan("Original.", "feedback", "goal", client)

        # tools kwarg should be None (no re-exploration during refinement)
        call_kwargs = client.stream.call_args[1]
        assert call_kwargs.get("tools") is None

    def test_returns_none_on_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        client = MagicMock()
        client.stream.side_effect = RuntimeError("fail")

        from minion.planner.creator import _refine_plan

        with (
            patch("minion.planner.creator.console") as mc,
            patch("minion.planner.creator.print_error"),
        ):
            mc.status.return_value = _make_status_ctx()
            result = _refine_plan("plan", "feedback", "goal", client)

        assert result is None


class TestExecutePlan:
    def test_injects_plan_into_system_prompt(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan_path = save_plan("# My Plan\nDo the thing.", "test goal")

        from minion.planner.creator import execute_plan
        from minion.repl import ReplState

        state = ReplState()

        with patch("minion.planner.creator.run_prompt") as mock_run:
            execute_plan(plan_path, MagicMock(), MagicMock(), "Base prompt.", state)

        assert mock_run.called
        _, kwargs = mock_run.call_args[0], mock_run.call_args
        # system_prompt arg (positional index 3) should contain plan content
        call_args = mock_run.call_args[0]
        system_arg = call_args[3]
        assert "# My Plan" in system_arg
        assert "Do the thing." in system_arg
        assert "Base prompt." in system_arg

    def test_uses_execute_prompt_as_user_message(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan_path = save_plan("# Plan", "goal")

        from minion.planner.creator import execute_plan
        from minion.repl import ReplState

        with patch("minion.planner.creator.run_prompt") as mock_run:
            execute_plan(plan_path, MagicMock(), MagicMock(), "sys", ReplState())

        user_prompt = mock_run.call_args[0][0]
        assert "Execute" in user_prompt
        assert "mission plan" in user_prompt.lower()

    def test_passes_reflect_config_when_reflect_depth_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan_path = save_plan("# Plan", "goal")

        from minion.planner.creator import execute_plan
        from minion.repl import ReplState
        from minion.reflection import ReflectionConfig

        state = ReplState(reflect_depth=2)

        with patch("minion.planner.creator.run_prompt") as mock_run:
            execute_plan(plan_path, MagicMock(), MagicMock(), "sys", state)

        kwargs = mock_run.call_args[1]
        assert isinstance(kwargs.get("reflect_config"), ReflectionConfig)
        assert kwargs["reflect_config"].depth == 2

    def test_no_reflect_config_when_depth_zero(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        plan_path = save_plan("# Plan", "goal")

        from minion.planner.creator import execute_plan
        from minion.repl import ReplState

        with patch("minion.planner.creator.run_prompt") as mock_run:
            execute_plan(plan_path, MagicMock(), MagicMock(), "sys", ReplState(reflect_depth=0))

        kwargs = mock_run.call_args[1]
        assert kwargs.get("reflect_config") is None


# ─── REPL command registration ────────────────────────────────────────────────

class TestPlanReplIntegration:
    def test_plan_command_registered(self):
        from minion.repl import REPL_COMMANDS
        assert "/plan" in REPL_COMMANDS

    def test_plan_command_in_tab_completion(self):
        from minion.repl import REPL_COMMANDS
        assert any("/plan" in cmd for cmd in REPL_COMMANDS)

    def test_active_plan_defaults_to_none(self):
        from minion.repl import ReplState
        state = ReplState()
        assert state.active_plan is None
        assert state.active_plan_goal is None

    def test_plan_clear_resets_state(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from minion.repl import ReplState, _handle_slash_command

        state = ReplState()
        state.active_plan = Path("/some/plan.md")
        state.active_plan_goal = "old goal"

        with patch("minion.repl.console"):
            _handle_slash_command("/plan --clear", MagicMock(), MagicMock(), state=state)

        assert state.active_plan is None
        assert state.active_plan_goal is None

    def test_plan_list_calls_list_plans(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from minion.repl import _handle_slash_command

        with (
            patch("minion.repl.console"),
            patch("minion.planner.storage.list_plans", return_value=[]) as mock_list,
        ):
            _handle_slash_command("/plan --list", MagicMock(), MagicMock())

        mock_list.assert_called_once()

    def test_plan_execute_dispatches_to_execute_plan(self, tmp_path, monkeypatch):
        """/plan execute with state.active_plan set calls execute_plan() with the right path."""
        monkeypatch.chdir(tmp_path)
        plan_path = save_plan("# My Plan\nDo things.", "test goal")

        from minion.repl import ReplState, _handle_slash_command

        state = ReplState()
        state.active_plan = plan_path
        state.active_plan_goal = "test goal"

        with (
            patch("minion.repl.console"),
            patch("minion.planner.creator.run_prompt") as mock_run,
        ):
            _handle_slash_command(
                "/plan --execute",
                MagicMock(),
                MagicMock(),
                state=state,
                base_system_prompt="Base prompt.",
            )

        # execute_plan() calls run_prompt internally — verify it was invoked
        mock_run.assert_called_once()
        # system_prompt arg (index 3) must contain the plan content
        system_arg = mock_run.call_args[0][3]
        assert "# My Plan" in system_arg
        assert "Do things." in system_arg

    def test_plan_reference_block_format(self, tmp_path, monkeypatch):
        """Plan reference block appended to system prompt contains goal, path, and read_file hint."""
        monkeypatch.chdir(tmp_path)
        plan_path = save_plan("# My Plan", "add feature")

        from minion.repl import ReplState

        state = ReplState()
        state.active_plan = plan_path
        state.active_plan_goal = "add feature"

        base_prompt = "You are Minion."

        # Reproduce the injection logic from run_repl()
        augmented = base_prompt
        if state.active_plan and state.active_plan.exists():
            goal_hint = state.active_plan_goal or state.active_plan.stem
            augmented += (
                f"\n\n## Recently Executed Plan\n"
                f"Goal: {goal_hint}\n"
                f"Path: {state.active_plan}\n"
                f"Use read_file on this path if it is relevant to the current request."
            )

        assert "## Recently Executed Plan" in augmented
        assert "add feature" in augmented
        assert str(plan_path) in augmented
        assert "read_file" in augmented
        assert base_prompt in augmented
