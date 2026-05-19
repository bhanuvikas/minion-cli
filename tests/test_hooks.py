"""Tests for the hooks system: events, shell handler, built-in handler, runner, registry."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minion.hooks.builtin.minion_md import MinionMdStalenessHandler
from minion.hooks.events import (
    PostToolUseEvent,
    PreToolUseEvent,
    SessionStartEvent,
    StopTurnEvent,
    UserPromptSubmitEvent,
)
from minion.hooks.handlers.shell import ShellHookHandler
from minion.hooks.registry import HookRegistry
from minion.hooks.result import HookResult
from minion.hooks.runner import HookRunner


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _pre(tool_name="write_file", tool_input=None, cwd=Path("/tmp")):
    return PreToolUseEvent(session_id="s1", cwd=cwd, tool_name=tool_name,
                           tool_input=tool_input or {"path": "src/main.py"})


def _post(tool_name="write_file", tool_input=None, result="ok", success=True, cwd=Path("/tmp")):
    return PostToolUseEvent(session_id="s1", cwd=cwd, tool_name=tool_name,
                            tool_input=tool_input or {"path": "src/main.py"},
                            tool_result=result, tool_success=success)


def _make_defn(event="PostToolUse", command="echo '{}'", tool=None, timeout=5, blocking=None):
    from minion.hooks.manifest import HookManifest
    return HookManifest(
        name="test-hook", description="", event=event, command=command,
        tools=[tool] if tool is not None else None, timeout=timeout, blocking=blocking,
    )


# ─── TestHookEvents ───────────────────────────────────────────────────────────

class TestHookEvents:
    def test_pre_tool_event_to_json_dict_has_expected_keys(self):
        event = _pre()
        d = event.to_json_dict()
        assert d["event"] == "PreToolUse"
        assert d["tool_name"] == "write_file"
        assert d["tool_input"] == {"path": "src/main.py"}
        assert "session_id" in d
        assert "cwd" in d

    def test_post_tool_event_includes_result_fields(self):
        event = _post(result="Written 10 bytes.", success=True)
        d = event.to_json_dict()
        assert d["event"] == "PostToolUse"
        assert d["tool_result"] == "Written 10 bytes."
        assert d["tool_success"] is True

    def test_session_start_event_fields(self):
        event = SessionStartEvent(session_id="abc", cwd=Path("/project"))
        d = event.to_json_dict()
        assert d["event"] == "SessionStart"
        assert d["session_id"] == "abc"
        assert "/project" in d["cwd"]

    def test_events_are_frozen(self):
        event = _pre()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            event.tool_name = "hacked"  # type: ignore[misc]


# ─── TestShellHookHandler ─────────────────────────────────────────────────────

class TestShellHookHandler:
    def test_matches_correct_event_and_tool(self):
        h = ShellHookHandler(_make_defn(event="PostToolUse", tool="write_file"))
        assert h.matches(_post(tool_name="write_file"))

    def test_does_not_match_wrong_event_name(self):
        h = ShellHookHandler(_make_defn(event="PreToolUse", tool="write_file"))
        assert not h.matches(_post(tool_name="write_file"))

    def test_does_not_match_wrong_tool(self):
        h = ShellHookHandler(_make_defn(event="PostToolUse", tool="run_shell"))
        assert not h.matches(_post(tool_name="write_file"))

    def test_matches_all_tools_when_tool_is_none(self):
        h = ShellHookHandler(_make_defn(event="PostToolUse", tool=None))
        assert h.matches(_post(tool_name="write_file"))
        assert h.matches(_post(tool_name="edit_file"))

    @pytest.mark.asyncio
    async def test_exit_0_returns_tip_from_stdout_json(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text('#!/bin/sh\necho \'{"tip": "linting passed"}\'\n')
        script.chmod(0o755)
        h = ShellHookHandler(_make_defn(event="PostToolUse", command=str(script), timeout=10))
        result = await h.execute(_post())
        assert result.action == "proceed"
        assert result.tip == "linting passed"

    @pytest.mark.asyncio
    async def test_exit_2_blocks_pre_tool_by_default(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\necho 'danger!' >&2\nexit 2\n")
        script.chmod(0o755)
        h = ShellHookHandler(_make_defn(event="PreToolUse", command=str(script), timeout=10))
        result = await h.execute(_pre())
        assert result.action == "block"
        assert "danger!" in result.reason

    @pytest.mark.asyncio
    async def test_exit_2_non_blocking_for_post_tool(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\nexit 2\n")
        script.chmod(0o755)
        h = ShellHookHandler(_make_defn(event="PostToolUse", command=str(script), timeout=10))
        result = await h.execute(_post())
        assert result.action == "proceed"

    @pytest.mark.asyncio
    async def test_timeout_returns_tip_not_raise(self, tmp_path):
        script = tmp_path / "hook.sh"
        script.write_text("#!/bin/sh\nsleep 60\n")
        script.chmod(0o755)
        h = ShellHookHandler(_make_defn(event="PostToolUse", command=str(script), timeout=1))
        result = await h.execute(_post())
        assert result.action == "proceed"
        assert "timed out" in result.tip


# ─── TestMinionMdStalenessHandler ─────────────────────────────────────────────

class TestMinionMdStalenessHandler:
    def test_matches_write_file_when_minion_md_exists(self, tmp_path):
        (tmp_path / "MINION.md").write_text("# project")
        h = MinionMdStalenessHandler()
        event = _post(tool_name="write_file",
                      tool_input={"path": str(tmp_path / "src" / "main.py")},
                      cwd=tmp_path)
        assert h.matches(event)

    def test_does_not_match_when_no_minion_md(self, tmp_path):
        h = MinionMdStalenessHandler()
        event = _post(tool_name="write_file",
                      tool_input={"path": str(tmp_path / "src" / "main.py")},
                      cwd=tmp_path)
        assert not h.matches(event)

    def test_does_not_match_minion_md_itself_as_path(self, tmp_path):
        (tmp_path / "MINION.md").write_text("# project")
        h = MinionMdStalenessHandler()
        event = _post(tool_name="write_file",
                      tool_input={"path": str(tmp_path / "MINION.md")},
                      cwd=tmp_path)
        assert not h.matches(event)

    def test_does_not_match_read_file_tool(self, tmp_path):
        (tmp_path / "MINION.md").write_text("# project")
        h = MinionMdStalenessHandler()
        event = _post(tool_name="read_file",
                      tool_input={"path": str(tmp_path / "main.py")},
                      cwd=tmp_path)
        assert not h.matches(event)

    @pytest.mark.asyncio
    async def test_execute_returns_generic_stale_tip(self, tmp_path):
        (tmp_path / "MINION.md").write_text("# project")
        h = MinionMdStalenessHandler()
        event = _post(tool_name="write_file",
                      tool_input={"path": str(tmp_path / "app.py")},
                      cwd=tmp_path)
        result = await h.execute(event)
        assert result.action == "proceed"
        assert "stale" in result.tip.lower()
        assert "/init" in result.tip


# ─── TestHookRunner ───────────────────────────────────────────────────────────

class TestHookRunner:
    def _tip_handler(self, tip="test tip"):
        h = MagicMock()
        h.matches.return_value = True
        h.execute = AsyncMock(return_value=HookResult(tip=tip))
        return h

    def _block_handler(self, reason="blocked"):
        h = MagicMock()
        h.matches.return_value = True
        h.execute = AsyncMock(return_value=HookResult(action="block", reason=reason))
        return h

    @pytest.mark.asyncio
    async def test_fire_collects_tips_into_pending(self):
        runner = HookRunner([self._tip_handler("tip A"), self._tip_handler("tip B")])
        await runner.fire(_post())
        assert runner.pending_tips == ["tip A", "tip B"]

    @pytest.mark.asyncio
    async def test_fire_pre_tool_returns_none_when_no_block(self):
        runner = HookRunner([self._tip_handler()])
        result = await runner.fire_pre_tool(_pre())
        assert result is None

    @pytest.mark.asyncio
    async def test_fire_pre_tool_returns_first_blocking_result(self):
        runner = HookRunner([self._block_handler("stop!"), self._block_handler("also stop")])
        result = await runner.fire_pre_tool(_pre())
        assert result is not None
        assert result.action == "block"
        assert result.reason == "stop!"

    @pytest.mark.asyncio
    async def test_drain_tips_clears_pending_list(self):
        runner = HookRunner([self._tip_handler("hello")])
        await runner.fire(_post())
        tips = runner.drain_tips()
        assert tips == ["hello"]
        assert runner.pending_tips == []

    @pytest.mark.asyncio
    async def test_disabled_runner_does_not_fire(self):
        handler = self._tip_handler()
        runner = HookRunner([handler])
        runner.disable()
        await runner.fire(_post())
        handler.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_propagate(self):
        h = MagicMock()
        h.matches.return_value = True
        h.execute = AsyncMock(side_effect=RuntimeError("boom"))
        runner = HookRunner([h])
        results = await runner.fire(_post())
        assert results == [HookResult()]


# ─── TestHookRegistry ─────────────────────────────────────────────────────────

class TestHookRegistry:
    def _cfg(self, builtin_minion_md=True):
        from minion.config import HooksBuiltinConfig, MinionConfig
        cfg = MinionConfig()
        cfg.hooks_config = HooksBuiltinConfig(builtin_minion_md=builtin_minion_md)
        return cfg

    def _write_hook(self, directory, name, event="PostToolUse", command="./test.sh", tool=None):
        """Write a minimal hook YAML to directory and return the path."""
        directory.mkdir(parents=True, exist_ok=True)
        content = f"name: {name}\ndescription: test\nevent: {event}\ncommand: {command}\n"
        if tool:
            content += f"tool: {tool}\n"
        path = directory / f"{name}.yaml"
        path.write_text(content)
        return path

    def test_load_registers_builtin_by_default(self, tmp_path):
        runner = HookRegistry.load(tmp_path, self._cfg(builtin_minion_md=True)).build_runner()
        types = [type(h).__name__ for h in runner._handlers]
        assert "MinionMdStalenessHandler" in types

    def test_load_skips_builtin_when_disabled(self, tmp_path):
        runner = HookRegistry.load(tmp_path, self._cfg(builtin_minion_md=False)).build_runner()
        types = [type(h).__name__ for h in runner._handlers]
        assert "MinionMdStalenessHandler" not in types

    def test_load_registers_shell_handlers_from_yaml(self, tmp_path):
        hooks_dir = tmp_path / ".minion" / "hooks"
        self._write_hook(hooks_dir, "my-hook", event="PostToolUse", command="./test.sh", tool="write_file")
        runner = HookRegistry.load(tmp_path, self._cfg()).build_runner()
        types = [type(h).__name__ for h in runner._handlers]
        assert "ShellHookHandler" in types
        assert runner.handler_count >= 1
