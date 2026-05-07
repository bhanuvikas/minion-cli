"""Tests for Phase 10 — SubAgents (Minion Army).

Tests cover:
  - AgentRoleManifest YAML loading
  - AgentRegistry three-tier loading + shadowing
  - run_agent() subagent execution + tracing
  - ToolExecutor.execute() routing for spawn_agent
  - Parallel _execute_tools() via ThreadPoolExecutor
  - _CONFIRM_LOCK serializing dangerous-tool confirmations
  - enable_agents / agent_depth filtering of spawn_agent from tool list
"""

from __future__ import annotations

import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from minion.agents.manifest import AgentRoleManifest, load_manifest
from minion.agents.registry import AgentRegistry, load_agent_registry
from minion.conversation import Conversation
from minion.llm.base import ToolUseBlock
from minion.tools.definitions import TOOL_DEFINITIONS
from minion.tools.executor import ToolExecutor, _CONFIRM_LOCK


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_tool_use_block(name: str, tool_id: str = "t1", inputs: dict | None = None) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=inputs or {})


def _has_spawn_agent(tools: list[dict] | None) -> bool:
    if tools is None:
        return any(t["name"] == "spawn_agent" for t in TOOL_DEFINITIONS)
    return any(t["name"] == "spawn_agent" for t in tools)


# ─── TestAgentManifest ────────────────────────────────────────────────────────

class TestAgentManifest:
    def test_load_manifest_all_fields(self, tmp_path):
        yaml_path = tmp_path / "analyst.yaml"
        yaml_path.write_text(
            "name: analyst\n"
            "description: Analyzes things\n"
            "system_prompt: You are an analyst.\n"
            "tools:\n  - read_file\n  - search_code\n"
            "max_iterations: 12\n"
        )
        m = load_manifest(yaml_path, source="user")
        assert m.name == "analyst"
        assert m.description == "Analyzes things"
        assert m.system_prompt == "You are an analyst."
        assert m.tools == ["read_file", "search_code"]
        assert m.max_iterations == 12
        assert m.source == "user"

    def test_load_manifest_defaults_name_to_filename_stem(self, tmp_path):
        yaml_path = tmp_path / "my-role.yaml"
        yaml_path.write_text("system_prompt: You are helpful.\n")
        m = load_manifest(yaml_path)
        assert m.name == "my-role"

    def test_load_manifest_missing_system_prompt_raises(self, tmp_path):
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text("name: bad\ndescription: missing prompt\n")
        with pytest.raises(ValueError, match="system_prompt"):
            load_manifest(yaml_path)

    def test_load_manifest_tools_none_when_absent(self, tmp_path):
        yaml_path = tmp_path / "all.yaml"
        yaml_path.write_text("system_prompt: You are all-tools.\n")
        m = load_manifest(yaml_path)
        assert m.tools is None

    def test_load_manifest_unknown_fields_ignored(self, tmp_path):
        yaml_path = tmp_path / "extra.yaml"
        yaml_path.write_text(
            "system_prompt: Hello\n"
            "future_field: some_value\n"
            "another_unknown: 42\n"
        )
        # Should not raise — dataclass ignores extra kwargs via explicit construction
        m = load_manifest(yaml_path)
        assert m.system_prompt == "Hello"


# ─── TestAgentRegistry ────────────────────────────────────────────────────────

class TestAgentRegistry:
    def test_load_registry_builtin_only(self):
        """The package ships 4 builtin roles."""
        registry = load_agent_registry(Path("/nonexistent/cwd"))
        assert "researcher" in registry
        assert "coder" in registry
        assert "reviewer" in registry
        assert "tester" in registry
        assert all(r.source == "builtin" for r in registry.values())

    def test_load_registry_user_shadows_builtin(self, tmp_path):
        user_dir = tmp_path / ".minion" / "agents"
        user_dir.mkdir(parents=True)
        (user_dir / "researcher.yaml").write_text(
            "name: researcher\n"
            "description: Custom researcher\n"
            "system_prompt: You are a custom researcher.\n"
        )
        with patch("minion.agents.registry.Path.home", return_value=tmp_path):
            registry = load_agent_registry(Path("/nonexistent/cwd"))
        assert registry["researcher"].description == "Custom researcher"
        assert registry["researcher"].source == "user"

    def test_load_registry_project_shadows_user(self, tmp_path):
        user_dir = tmp_path / "home" / ".minion" / "agents"
        user_dir.mkdir(parents=True)
        (user_dir / "analyst.yaml").write_text(
            "name: analyst\ndescription: user analyst\nsystem_prompt: user\n"
        )
        project_dir = tmp_path / "project" / ".minion" / "agents"
        project_dir.mkdir(parents=True)
        (project_dir / "analyst.yaml").write_text(
            "name: analyst\ndescription: project analyst\nsystem_prompt: project\n"
        )
        with patch("minion.agents.registry.Path.home", return_value=tmp_path / "home"):
            registry = load_agent_registry(tmp_path / "project")
        assert registry["analyst"].description == "project analyst"
        assert registry["analyst"].source == "project"

    def test_load_registry_empty_when_no_custom_files(self):
        """Returns at least the 4 builtins (never truly empty)."""
        registry = load_agent_registry(Path("/nonexistent/cwd"))
        assert len(registry) >= 4


# ─── TestRunAgent ─────────────────────────────────────────────────────────────

class TestRunAgent:
    def _make_registry(self) -> AgentRegistry:
        return {
            "researcher": AgentRoleManifest(
                name="researcher",
                description="Reads code",
                system_prompt="You are a researcher.",
                tools=["read_file"],
                max_iterations=5,
                source="builtin",
            )
        }

    def test_run_agent_returns_captured_text(self):
        registry = self._make_registry()
        client = MagicMock()

        # run_agent does `from ..runner import run_prompt` inside the function,
        # so patch the name in minion.runner where it is resolved.
        with patch("minion.runner.run_prompt", return_value="Research result") as mock_rp:
            from minion.agents.runner import run_agent
            result = run_agent("Analyze the code", "researcher", registry, client)

        assert result == "Research result"
        mock_rp.assert_called_once()
        kwargs = mock_rp.call_args.kwargs
        assert kwargs["capture_output"] is True
        assert kwargs["agent_depth"] == 1

    def test_run_agent_unknown_role_falls_back_to_researcher(self):
        registry = self._make_registry()
        client = MagicMock()

        with patch("minion.runner.run_prompt", return_value="fallback result"), \
             patch("minion.agents.runner.console") as mc:
            from minion.agents.runner import run_agent
            result = run_agent("task", "nonexistent_role", registry, client)

        mc.print.assert_any_call(
            "[muted]  ⚠  Unknown agent role 'nonexistent_role', using researcher.[/]"
        )
        assert result == "fallback result"

    def test_run_agent_emits_spawn_and_complete_events(self):
        registry = self._make_registry()
        client = MagicMock()

        emitted = []
        with patch("minion.runner.run_prompt", return_value="done"), \
             patch("minion.agents.runner.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit.side_effect = lambda *a, **kw: emitted.append((a, kw))
            from minion.agents.runner import run_agent
            run_agent("task", "researcher", registry, client)

        types = [e[0][0] for e in emitted]
        assert "agent_spawn" in types
        assert "agent_complete" in types

    def test_run_agent_emits_error_event_on_exception(self):
        registry = self._make_registry()
        client = MagicMock()

        emitted = []
        with patch("minion.runner.run_prompt", side_effect=RuntimeError("boom")), \
             patch("minion.agents.runner.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit.side_effect = lambda *a, **kw: emitted.append((a, kw))
            from minion.agents.runner import run_agent
            result = run_agent("task", "researcher", registry, client)

        assert "Error in [researcher] subagent" in result
        types = [e[0][0] for e in emitted]
        assert "agent_error" in types

    def test_run_agent_passes_correct_depth_to_run_prompt(self):
        registry = self._make_registry()
        client = MagicMock()

        with patch("minion.runner.run_prompt", return_value="ok") as mock_rp:
            from minion.agents.runner import run_agent
            run_agent("task", "researcher", registry, client, parent_depth=0)

        assert mock_rp.call_args.kwargs["agent_depth"] == 1


# ─── TestSpawnAgentTool ───────────────────────────────────────────────────────

def _mock_renderer():
    """Return a MagicMock satisfying OutputRenderer interface for tests."""
    from minion.output import OutputRenderer
    r = MagicMock(spec=OutputRenderer)
    r.spinner.return_value.__enter__ = MagicMock(return_value=None)
    r.spinner.return_value.__exit__ = MagicMock(return_value=False)
    return r


class TestSpawnAgentTool:
    def test_executor_routes_spawn_agent_to_agent_runner(self):
        runner = MagicMock(return_value="subagent output")
        executor = ToolExecutor(agent_runner=runner, renderer=_mock_renderer())
        tb = _make_tool_use_block("spawn_agent", inputs={"task": "do stuff", "role": "researcher"})

        with patch("minion.tools.executor.get_tracer"):
            result = executor.execute(tb)

        runner.assert_called_once_with("do stuff", "researcher")
        assert result == "subagent output"

    def test_executor_spawn_agent_no_runner_returns_error(self):
        executor = ToolExecutor(agent_runner=None, renderer=_mock_renderer())
        tb = _make_tool_use_block("spawn_agent", inputs={"task": "task"})
        result = executor.execute(tb)
        assert "not available" in result.lower()

    def test_executor_spawn_agent_extracts_task_and_role(self):
        received = {}
        def fake_runner(task, role):
            received["task"] = task
            received["role"] = role
            return "done"

        executor = ToolExecutor(agent_runner=fake_runner, renderer=_mock_renderer())
        tb = _make_tool_use_block(
            "spawn_agent", inputs={"task": "analyze auth.py", "role": "reviewer"}
        )
        with patch("minion.tools.executor.get_tracer"):
            executor.execute(tb)

        assert received["task"] == "analyze auth.py"
        assert received["role"] == "reviewer"

    def test_executor_native_tools_still_work_alongside_agent_runner(self):
        runner = MagicMock(return_value="agent result")
        executor = ToolExecutor(agent_runner=runner, renderer=_mock_renderer())
        tb = _make_tool_use_block("read_file", inputs={"path": "README.md"})

        # _DISPATCH holds references captured at import time; patch via _DISPATCH directly.
        from minion.tools import executor as _exec_mod
        original = _exec_mod._DISPATCH["read_file"]
        mock_read = MagicMock(return_value="file contents")
        _exec_mod._DISPATCH["read_file"] = mock_read
        try:
            with patch("minion.tools.executor.get_tracer"):
                result = executor.execute(tb)
        finally:
            _exec_mod._DISPATCH["read_file"] = original

        mock_read.assert_called_once_with(path="README.md")
        assert result == "file contents"
        runner.assert_not_called()


# ─── TestParallelToolExecution ────────────────────────────────────────────────

class TestParallelToolExecution:
    def test_single_tool_uses_fast_path(self):
        """Single tool block takes the fast path — no ThreadPoolExecutor overhead."""
        from minion.runner import _execute_tools

        executor = MagicMock()
        executor.execute.return_value = "result"
        conv = MagicMock()
        tb = _make_tool_use_block("read_file", inputs={"path": "f.py"})

        with patch("minion.runner.ThreadPoolExecutor") as mock_pool:
            _execute_tools([tb], executor, conv)

        mock_pool.assert_not_called()
        executor.execute.assert_called_once_with(tb)
        conv.add_tool_result.assert_called_once_with(tb.id, "result")

    def test_multiple_tools_run_concurrently(self):
        """Two slow tools complete faster together than sequentially."""
        from minion.runner import _execute_tools

        DELAY = 0.08

        def slow_execute(tb):
            time.sleep(DELAY)
            return f"result_{tb.id}"

        executor = MagicMock()
        executor.execute.side_effect = slow_execute
        conv = MagicMock()
        blocks = [
            _make_tool_use_block("read_file", tool_id="t1"),
            _make_tool_use_block("search_code", tool_id="t2"),
        ]

        start = time.monotonic()
        _execute_tools(blocks, executor, conv)
        elapsed = time.monotonic() - start

        # Sequential would take 2*DELAY ≈ 0.16s; parallel should be ~DELAY
        assert elapsed < DELAY * 1.8, f"Took {elapsed:.3f}s, expected < {DELAY * 1.8:.3f}s"
        assert conv.add_tool_result.call_count == 2

    def test_parallel_results_injected_in_original_order(self):
        """Results are added to conversation in original tool_block order, not completion order."""
        from minion.runner import _execute_tools

        order_of_injection = []

        def fake_add_tool_result(tool_id, result):
            order_of_injection.append(tool_id)

        conv = MagicMock()
        conv.add_tool_result.side_effect = fake_add_tool_result

        # Make t1 slower so t2 finishes first
        def execute(tb):
            if tb.id == "t1":
                time.sleep(0.05)
            return f"r_{tb.id}"

        executor = MagicMock()
        executor.execute.side_effect = execute
        blocks = [
            _make_tool_use_block("read_file", tool_id="t1"),
            _make_tool_use_block("search_code", tool_id="t2"),
        ]

        _execute_tools(blocks, executor, conv)

        # Despite t2 finishing first, results are injected in original order
        assert order_of_injection == ["t1", "t2"]

    def test_parallel_exception_captured_as_error_string(self):
        """If a tool raises, its slot gets an error string, not a crash."""
        from minion.runner import _execute_tools

        def execute(tb):
            if tb.id == "t2":
                raise RuntimeError("disk full")
            return "ok"

        executor = MagicMock()
        executor.execute.side_effect = execute
        conv = MagicMock()
        blocks = [
            _make_tool_use_block("read_file", tool_id="t1"),
            _make_tool_use_block("write_file", tool_id="t2"),
        ]

        _execute_tools(blocks, executor, conv)

        calls = {c[0][0]: c[0][1] for c in conv.add_tool_result.call_args_list}
        assert calls["t1"] == "ok"
        assert "Error" in calls["t2"]
        assert "disk full" in calls["t2"]


# ─── TestConfirmLock ──────────────────────────────────────────────────────────

class TestConfirmLock:
    def test_confirm_lock_is_module_level_threading_lock(self):
        """_CONFIRM_LOCK is a threading.Lock usable across threads."""
        assert hasattr(_CONFIRM_LOCK, "acquire")
        assert hasattr(_CONFIRM_LOCK, "release")
        assert _CONFIRM_LOCK.acquire(blocking=False)
        _CONFIRM_LOCK.release()

    def test_concurrent_dangerous_tools_serialize_confirmations(self):
        """Threads competing to confirm must wait: at most 1 active at a time.

        We test the lock directly — simulating 3 threads each trying to hold
        _CONFIRM_LOCK while counting concurrent holders. The count must never
        exceed 1, which proves confirmations are serialized.
        """
        active_count = [0]
        max_concurrent = [0]
        counter_lock = threading.Lock()

        def hold_confirm_lock():
            with _CONFIRM_LOCK:
                with counter_lock:
                    active_count[0] += 1
                    max_concurrent[0] = max(max_concurrent[0], active_count[0])
                time.sleep(0.02)  # simulate user interaction time
                with counter_lock:
                    active_count[0] -= 1

        threads = [threading.Thread(target=hold_confirm_lock) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_concurrent[0] == 1, (
            f"Expected at most 1 concurrent confirmation holder, got {max_concurrent[0]}"
        )


# ─── TestAgentsToggle ─────────────────────────────────────────────────────────

class TestAgentsToggle:
    """Verify that spawn_agent appears or disappears based on enable_agents/agent_depth."""

    async def _captured_tools(self, **run_prompt_kwargs) -> list[dict] | None:
        """Call run_prompt_async with a minimal mock client and capture the tools list
        passed to _stream_one_iteration_async."""
        from minion.runner import run_prompt_async

        captured = {}

        async def fake_stream_one_iteration_async(client, conversation, system_prompt, tools=None, **kwargs):
            captured["tools"] = tools
            return None  # triggers early return (None = error path pops user message)

        client = MagicMock()
        client.model_id = "stub"
        conv = Conversation()

        with patch("minion.runner._stream_one_iteration_async", side_effect=fake_stream_one_iteration_async), \
             patch("minion.runner.print_error"):
            await run_prompt_async("hi", client, conv, "system", **run_prompt_kwargs)

        return captured.get("tools")

    @pytest.mark.asyncio
    async def test_spawn_agent_excluded_when_enable_agents_false(self):
        tools = await self._captured_tools(enable_agents=False)
        assert not _has_spawn_agent(tools)

    @pytest.mark.asyncio
    async def test_spawn_agent_excluded_when_agent_depth_at_max(self):
        from minion.agents.runner import MAX_AGENT_DEPTH
        tools = await self._captured_tools(enable_agents=True, agent_depth=MAX_AGENT_DEPTH)
        assert not _has_spawn_agent(tools)

    @pytest.mark.asyncio
    async def test_spawn_agent_included_when_agents_enabled_depth_zero(self):
        # No registry → no agent_runner → but spawn_agent still in tool list
        tools = await self._captured_tools(enable_agents=True, agent_depth=0)
        # spawn_agent is in TOOL_DEFINITIONS; effective_tools defaults to TOOL_DEFINITIONS
        # when tools=None (not excluded). _stream_one_iteration_async receives None → it uses TOOL_DEFINITIONS.
        # tools captured as None means TOOL_DEFINITIONS is used, which includes spawn_agent.
        if tools is None:
            assert _has_spawn_agent(None)  # TOOL_DEFINITIONS contains spawn_agent
        else:
            assert _has_spawn_agent(tools)
