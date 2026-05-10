"""Tests for minion/skills/ — manifest loading, registry discovery, execution.

All LLM calls are mocked. Filesystem operations use tmp_path so no real
~/.minion/skills/ or .minion/skills/ directories are affected.
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from minion.llm.base import StreamComplete, TextChunk
from minion.skills.manifest import SkillManifest, load_manifest
from minion.skills.registry import SkillRegistry, load_skill_registry
from minion.skills.runner import _resolve_tools, execute_skill
from minion.tools.definitions import TOOL_DEFINITIONS


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _write_skill(directory: Path, filename: str, content: str) -> Path:
    """Write a YAML skill file to a directory and return the path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


_MINIMAL_YAML = """\
name: myskill
description: A test skill
prompt: Do something useful.
"""

_REVIEW_YAML = """\
name: review
description: Code review skill
prompt: |
  Review the code carefully.
  Target: {arg}
tools:
  - read_file
  - search_code
args:
  - name: target
    description: "File to review"
    required: false
max_iterations: 15
"""

_CHAINED_YAML = """\
name: ship
description: Test then commit
prompt: Ship the code.
steps:
  - test
  - commit
"""


def _make_status_ctx():
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _text_stream(*texts: str, stop_reason: str = "end_turn"):
    events = [TextChunk(text=t) for t in texts]
    events.append(StreamComplete(stop_reason=stop_reason, input_tokens=10, output_tokens=5, model="test"))
    return iter(events)


# ─── TestSkillManifest ────────────────────────────────────────────────────────

class TestSkillManifest:
    def test_load_minimal_yaml(self, tmp_path):
        path = _write_skill(tmp_path, "myskill.yaml", _MINIMAL_YAML)
        m = load_manifest(path)
        assert m.name == "myskill"
        assert m.description == "A test skill"
        assert m.prompt == "Do something useful."
        assert m.tools is None           # absent → all tools
        assert m.steps == []
        assert m.args == []
        assert m.max_iterations == 20
        assert m.source == "builtin"     # default

    def test_tools_none_when_omitted(self, tmp_path):
        path = _write_skill(tmp_path, "s.yaml", _MINIMAL_YAML)
        assert load_manifest(path).tools is None

    def test_tools_list_when_specified(self, tmp_path):
        yaml_text = _MINIMAL_YAML + "tools:\n  - read_file\n"
        path = _write_skill(tmp_path, "s.yaml", yaml_text)
        assert load_manifest(path).tools == ["read_file"]

    def test_steps_populated(self, tmp_path):
        path = _write_skill(tmp_path, "s.yaml", _CHAINED_YAML)
        m = load_manifest(path)
        assert m.steps == ["test", "commit"]

    def test_missing_prompt_raises_value_error(self, tmp_path):
        yaml_text = "name: bad\ndescription: no prompt\n"
        path = _write_skill(tmp_path, "bad.yaml", yaml_text)
        with pytest.raises(ValueError, match="missing required 'prompt'"):
            load_manifest(path)

    def test_source_preserved(self, tmp_path):
        path = _write_skill(tmp_path, "s.yaml", _MINIMAL_YAML)
        assert load_manifest(path, source="project").source == "project"

    def test_name_falls_back_to_stem(self, tmp_path):
        yaml_text = "description: no name\nprompt: something\n"
        path = _write_skill(tmp_path, "myskill.yaml", yaml_text)
        assert load_manifest(path).name == "myskill"


# ─── TestSkillRegistry ────────────────────────────────────────────────────────

class TestSkillRegistry:
    def test_builtin_skills_loaded(self):
        registry = load_skill_registry(cwd=Path("/nonexistent"))
        assert len(registry) == 5

    def test_all_builtin_names_present(self):
        registry = load_skill_registry(cwd=Path("/nonexistent"))
        for name in ("commit", "review", "test", "explain", "refactor"):
            assert name in registry

    def test_project_shadows_builtin(self, tmp_path):
        project_skills = tmp_path / ".minion" / "skills"
        _write_skill(project_skills, "commit.yaml",
                     "name: commit\ndescription: custom commit\nprompt: custom prompt\n")
        registry = load_skill_registry(cwd=tmp_path)
        assert registry.get("commit").source == "project"
        assert registry.get("commit").description == "custom commit"

    def test_user_shadows_builtin(self, tmp_path, monkeypatch):
        user_skills = tmp_path / "user_skills"
        _write_skill(user_skills, "commit.yaml",
                     "name: commit\ndescription: user commit\nprompt: user prompt\n")
        # Patch _USER_DIR inside the registry module
        import minion.skills.registry as reg_mod
        monkeypatch.setattr(reg_mod, "_USER_DIR", user_skills)
        registry = load_skill_registry(cwd=Path("/nonexistent"))
        assert registry.get("commit").source == "user"

    def test_project_shadows_user(self, tmp_path, monkeypatch):
        user_skills = tmp_path / "user_skills"
        _write_skill(user_skills, "commit.yaml",
                     "name: commit\ndescription: user\nprompt: user prompt\n")
        project_skills = tmp_path / ".minion" / "skills"
        _write_skill(project_skills, "commit.yaml",
                     "name: commit\ndescription: project\nprompt: project prompt\n")
        import minion.skills.registry as reg_mod
        monkeypatch.setattr(reg_mod, "_USER_DIR", user_skills)
        registry = load_skill_registry(cwd=tmp_path)
        assert registry.get("commit").source == "project"

    def test_bad_yaml_skipped_gracefully(self, tmp_path):
        project_skills = tmp_path / ".minion" / "skills"
        _write_skill(project_skills, "bad.yaml", "name: bad\nnot: valid: yaml: ::::\n")
        _write_skill(project_skills, "good.yaml",
                     "name: mygood\ndescription: fine\nprompt: fine prompt\n")
        # Should not raise; good skill loads, bad skill is skipped
        registry = load_skill_registry(cwd=tmp_path)
        assert registry.get("mygood") is not None

    def test_get_unknown_returns_none(self):
        registry = load_skill_registry(cwd=Path("/nonexistent"))
        assert registry.get("nonexistent_skill_xyz") is None

    def test_iteration_yields_all_names(self):
        registry = load_skill_registry(cwd=Path("/nonexistent"))
        names = list(registry)
        assert "commit" in names
        assert "review" in names


# ─── TestResolveTools ─────────────────────────────────────────────────────────

class TestResolveTools:
    def test_none_returns_none(self):
        assert _resolve_tools(None) is None

    def test_empty_list_returns_empty(self):
        assert _resolve_tools([]) == []

    def test_named_tools_filtered(self):
        result = _resolve_tools(["read_file"])
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].name == "read_file"

    def test_multiple_tools_filtered(self):
        result = _resolve_tools(["read_file", "run_shell"])
        names = {t.name for t in result}
        assert names == {"read_file", "run_shell"}

    def test_unknown_tool_name_excluded(self):
        result = _resolve_tools(["read_file", "nonexistent_tool"])
        assert len(result) == 1
        assert result[0].name == "read_file"


# ─── TestExecuteSkill ─────────────────────────────────────────────────────────

class TestExecuteSkill:
    def _make_skill(self, **kwargs) -> SkillManifest:
        defaults = dict(
            name="myskill", description="A skill", prompt="Do something. Target: {arg}",
            tools=["read_file"], max_iterations=5, source="builtin",
        )
        defaults.update(kwargs)
        return SkillManifest(**defaults)

    def _make_registry(self, skills: dict | None = None) -> SkillRegistry:
        return SkillRegistry(skills or {})

    def test_run_prompt_called_once(self):
        skill = self._make_skill()
        with patch("minion.skills.runner.run_prompt") as mock_rp, \
             patch("minion.skills.runner.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit = MagicMock()
            execute_skill(skill, "src/foo.py", MagicMock(), MagicMock(),
                          "base_prompt", self._make_registry())
        mock_rp.assert_called_once()

    def test_system_prompt_contains_skill_prompt_text(self):
        skill = self._make_skill(prompt="My special instructions.")
        captured = {}
        def capture(*args, **kwargs):
            captured["system"] = args[3]
        with patch("minion.skills.runner.run_prompt", side_effect=capture), \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(skill, "", MagicMock(), MagicMock(), "BASE", self._make_registry())
        assert "My special instructions." in captured["system"]
        assert "BASE" in captured["system"]

    def test_arg_substituted_in_prompt(self):
        skill = self._make_skill(prompt="Review {arg} carefully.")
        captured = {}
        def capture(*args, **kwargs):
            captured["system"] = args[3]
        with patch("minion.skills.runner.run_prompt", side_effect=capture), \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(skill, "src/auth.py", MagicMock(), MagicMock(), "BASE", self._make_registry())
        assert "src/auth.py" in captured["system"]

    def test_tool_filtering_passed_to_run_prompt(self):
        skill = self._make_skill(tools=["read_file"])
        captured = {}
        def capture(*args, **kwargs):
            captured["tools"] = kwargs.get("tools")
        with patch("minion.skills.runner.run_prompt", side_effect=capture), \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(skill, "", MagicMock(), MagicMock(), "BASE", self._make_registry())
        assert captured["tools"] is not None
        assert len(captured["tools"]) == 1
        assert captured["tools"][0].name == "read_file"

    def test_no_tools_in_manifest_passes_none(self):
        skill = self._make_skill(tools=None)
        captured = {}
        def capture(*args, **kwargs):
            captured["tools"] = kwargs.get("tools")
        with patch("minion.skills.runner.run_prompt", side_effect=capture), \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(skill, "", MagicMock(), MagicMock(), "BASE", self._make_registry())
        assert captured["tools"] is None

    def test_chained_skill_calls_each_step_in_order(self):
        test_skill = self._make_skill(name="test", prompt="Run tests.")
        commit_skill = self._make_skill(name="commit", prompt="Commit code.")
        ship_skill = SkillManifest(
            name="ship", description="Ship", prompt="Ship.",
            steps=["test", "commit"], source="builtin",
        )
        registry = self._make_registry({"test": test_skill, "commit": commit_skill})
        call_order = []
        original = execute_skill.__wrapped__ if hasattr(execute_skill, "__wrapped__") else None

        with patch("minion.skills.runner.run_prompt") as mock_rp, \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(ship_skill, "arg", MagicMock(), MagicMock(), "BASE", registry)
        # Both steps should have triggered run_prompt
        assert mock_rp.call_count == 2


# ─── TestRunPromptToolsParam ──────────────────────────────────────────────────

class TestRunPromptToolsParam:
    def _make_async_client(self, stop_reason="end_turn"):
        events = [TextChunk(text="done"), StreamComplete(
            stop_reason=stop_reason, input_tokens=5, output_tokens=3, model="test"
        )]
        async def _gen(*args, **kwargs):
            for e in events:
                yield e
        client = MagicMock()
        client.model_id = "test"
        client.async_stream = MagicMock(side_effect=_gen)
        return client

    @pytest.mark.asyncio
    async def test_defaults_to_all_tools(self):
        from minion.runner import run_prompt_async
        from minion.llm.conversation import Conversation
        client = self._make_async_client()
        with patch("minion.runner.loop.console"):
            await run_prompt_async("hello", client, Conversation(), "sys")
        _, call_kwargs = client.async_stream.call_args
        # send_remote_task is filtered out when no a2a_manager is provided
        expected = [t for t in TOOL_DEFINITIONS if t.name != "send_remote_task"]
        assert call_kwargs["tools"] == expected

    @pytest.mark.asyncio
    async def test_custom_subset_passed(self):
        from minion.runner import run_prompt_async
        from minion.llm.conversation import Conversation
        single_tool = [TOOL_DEFINITIONS[0]]
        client = self._make_async_client()
        with patch("minion.runner.loop.console"):
            await run_prompt_async("hello", client, Conversation(), "sys", tools=single_tool)
        _, call_kwargs = client.async_stream.call_args
        assert call_kwargs["tools"] == single_tool

    @pytest.mark.asyncio
    async def test_empty_tools_passes_empty_list(self):
        from minion.runner import run_prompt_async
        from minion.llm.conversation import Conversation
        client = self._make_async_client()
        with patch("minion.runner.loop.console"):
            await run_prompt_async("hello", client, Conversation(), "sys", tools=[])
        _, call_kwargs = client.async_stream.call_args
        assert call_kwargs["tools"] == []


# ─── TestReplSkillIntegration ─────────────────────────────────────────────────

class TestReplSkillIntegration:
    def _make_registry(self) -> SkillRegistry:
        s1 = SkillManifest(name="commit", description="Commit skill", prompt="Do commit.", source="builtin")
        s2 = SkillManifest(name="review", description="Review skill", prompt="Do review.", source="builtin")
        return SkillRegistry({"commit": s1, "review": s2})

    def test_skill_dispatch_from_slash_command(self):
        from minion.repl import _handle_slash_command, CommandContext
        registry = self._make_registry()
        ctx = CommandContext(client=MagicMock(), conversation=MagicMock(), skill_registry=registry)
        with patch("minion.skills.runner.execute_skill") as mock_exec, \
             patch("minion.skills.runner.get_tracer"):
            _handle_slash_command("/commit", ctx)
        mock_exec.assert_called_once()
        # First arg is the skill manifest
        assert mock_exec.call_args[0][0].name == "commit"

    def test_skills_command_lists_all(self, capsys):
        from minion.repl import _handle_slash_command, CommandContext
        from unittest.mock import patch as _patch
        registry = self._make_registry()
        printed = []
        ctx = CommandContext(client=MagicMock(), conversation=MagicMock(), skill_registry=registry)
        with _patch("minion.repl.commands.console") as mc:
            mc.print.side_effect = lambda msg, **kw: printed.append(msg)
            result = _handle_slash_command("/skills", ctx)
        assert result is True
        combined = " ".join(printed)
        assert "commit" in combined
        assert "review" in combined

    def test_skills_in_repl_commands_after_load(self, tmp_path, monkeypatch):
        """Skills loaded into REPL_COMMANDS so tab completion works."""
        from minion import repl as repl_mod
        # Backup and clear any existing skill keys
        original = dict(repl_mod.REPL_COMMANDS)
        try:
            # Remove any skill keys that may already be there
            for k in list(repl_mod.REPL_COMMANDS):
                if k not in original or k.startswith("/commit"):
                    pass

            registry = self._make_registry()
            for name, skill in registry.items():
                cmd_key = f"/{name}"
                if cmd_key not in repl_mod.REPL_COMMANDS:
                    repl_mod.REPL_COMMANDS[cmd_key] = f"[skill] {skill.description}"
            assert "/commit" in repl_mod.REPL_COMMANDS
            assert "/review" in repl_mod.REPL_COMMANDS
        finally:
            # Restore original
            repl_mod.REPL_COMMANDS.clear()
            repl_mod.REPL_COMMANDS.update(original)

    def test_unknown_command_not_dispatched_as_skill(self):
        from minion.repl import _handle_slash_command, CommandContext
        registry = self._make_registry()  # only has "commit" and "review"
        ctx = CommandContext(client=MagicMock(), conversation=MagicMock(), skill_registry=registry)
        with patch("minion.skills.runner.execute_skill") as mock_exec:
            result = _handle_slash_command("/nonexistent_skill_xyz", ctx)
        mock_exec.assert_not_called()
        assert result is True  # handled by unknown-command fallback


# ─── TestSkillTracing ─────────────────────────────────────────────────────────

class TestSkillTracing:
    def _simple_skill(self) -> SkillManifest:
        return SkillManifest(
            name="myskill", description="test", prompt="Do something.",
            tools=["read_file"], max_iterations=5, source="builtin",
        )

    def test_skill_start_emitted(self):
        skill = self._simple_skill()
        emitted = []
        mock_tracer = MagicMock()
        mock_tracer.emit.side_effect = lambda event, **kw: emitted.append((event, kw))
        with patch("minion.skills.runner.run_prompt"), \
             patch("minion.skills.runner.get_tracer", return_value=mock_tracer):
            execute_skill(skill, "src/foo.py", MagicMock(), MagicMock(),
                          "BASE", SkillRegistry({}))
        start_events = [(e, kw) for e, kw in emitted if e == "skill_start"]
        assert len(start_events) == 1
        _, kw = start_events[0]
        assert kw["skill_name"] == "myskill"
        assert kw["arg"] == "src/foo.py"
        assert kw["source"] == "builtin"

    def test_skill_complete_emitted(self):
        skill = self._simple_skill()
        emitted = []
        mock_tracer = MagicMock()
        mock_tracer.emit.side_effect = lambda event, **kw: emitted.append((event, kw))
        with patch("minion.skills.runner.run_prompt"), \
             patch("minion.skills.runner.get_tracer", return_value=mock_tracer):
            execute_skill(skill, "src/foo.py", MagicMock(), MagicMock(),
                          "BASE", SkillRegistry({}))
        complete_events = [e for e, _ in emitted if e == "skill_complete"]
        assert len(complete_events) == 1


# ─── TestExecuteSkillEdgeCases ────────────────────────────────────────────────

class TestExecuteSkillEdgeCases:
    def test_chained_skill_unknown_step_aborts(self):
        """Chain with a missing step name should abort without calling run_prompt."""
        ship_skill = SkillManifest(
            name="ship", description="Ship", prompt="Ship.",
            steps=["nonexistent_step"], source="builtin",
        )
        registry = SkillRegistry({})  # empty — step not found
        with patch("minion.skills.runner.run_prompt") as mock_rp, \
             patch("minion.skills.runner.get_tracer"), \
             patch("minion.skills.runner.console"):
            execute_skill(ship_skill, "", MagicMock(), MagicMock(), "BASE", registry)
        mock_rp.assert_not_called()

    def test_max_iterations_from_manifest_passed_to_run_prompt(self):
        """The max_iterations field in the manifest is forwarded to run_prompt."""
        skill = SkillManifest(
            name="myskill", description="test", prompt="Do it.",
            tools=None, max_iterations=7, source="builtin",
        )
        captured = {}
        def capture(*args, **kwargs):
            captured["max_iterations"] = kwargs.get("max_iterations")
        with patch("minion.skills.runner.run_prompt", side_effect=capture), \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(skill, "", MagicMock(), MagicMock(), "BASE", SkillRegistry({}))
        assert captured["max_iterations"] == 7

    def test_no_arg_user_message_omits_target(self):
        """When no arg is given, user message says 'Run the /skill skill.' (no target)."""
        skill = SkillManifest(
            name="commit", description="Commit", prompt="Commit the changes.",
            source="builtin",
        )
        captured = {}
        def capture(user_msg, *args, **kwargs):
            captured["user_msg"] = user_msg
        with patch("minion.skills.runner.run_prompt", side_effect=capture), \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(skill, "", MagicMock(), MagicMock(), "BASE", SkillRegistry({}))
        assert "Target:" not in captured["user_msg"]
        assert "/commit" in captured["user_msg"]

    def test_arg_included_in_user_message(self):
        """When arg is given, user message includes 'Target: <arg>'."""
        skill = SkillManifest(
            name="review", description="Review", prompt="Review code.",
            source="builtin",
        )
        captured = {}
        def capture(user_msg, *args, **kwargs):
            captured["user_msg"] = user_msg
        with patch("minion.skills.runner.run_prompt", side_effect=capture), \
             patch("minion.skills.runner.get_tracer"):
            execute_skill(skill, "src/auth.py", MagicMock(), MagicMock(), "BASE", SkillRegistry({}))
        assert "src/auth.py" in captured["user_msg"]


# ─── TestSkillsNoRegistry ─────────────────────────────────────────────────────

class TestSkillsNoRegistry:
    def test_skills_command_with_no_registry_prints_message(self):
        """/skills with skill_registry=None prints 'No skills loaded.'"""
        from minion.repl import _handle_slash_command, CommandContext
        printed = []
        ctx = CommandContext(client=MagicMock(), conversation=MagicMock())
        with patch("minion.repl.commands.console") as mc:
            mc.print.side_effect = lambda msg, **kw: printed.append(msg)
            result = _handle_slash_command("/skills", ctx)
        assert result is True
        assert any("No skills loaded" in str(m) for m in printed)

    def test_skill_command_without_registry_falls_through(self):
        """/commit with no registry → unknown command (no execute_skill call)."""
        from minion.repl import _handle_slash_command, CommandContext
        ctx = CommandContext(client=MagicMock(), conversation=MagicMock())
        with patch("minion.skills.runner.execute_skill") as mock_exec:
            result = _handle_slash_command("/commit", ctx)
        mock_exec.assert_not_called()
        assert result is True  # handled as unknown command


# ─── TestCLISkillsList ────────────────────────────────────────────────────────

class TestCLISkillsList:
    def test_list_skills_prints_builtin_skills(self):
        """_list_skills() outputs all 5 built-in skill names."""
        from minion.cli import _list_skills
        printed = []
        with patch("minion.cli.console") as mc:
            mc.print.side_effect = lambda msg, **kw: printed.append(msg)
            _list_skills()
        combined = " ".join(printed)
        for name in ("commit", "review", "test", "explain", "refactor"):
            assert name in combined, f"Expected '{name}' in CLI skills list output"

    def test_list_skills_shows_source(self):
        """Each printed line includes the source tier label."""
        from minion.cli import _list_skills
        printed = []
        with patch("minion.cli.console") as mc:
            mc.print.side_effect = lambda msg, **kw: printed.append(msg)
            _list_skills()
        # All built-ins should show [builtin]
        assert any("builtin" in str(m) for m in printed)

    def test_list_skills_project_override_reflected(self, tmp_path, monkeypatch):
        """When a project skill overrides a builtin, the CLI shows the project version."""
        monkeypatch.chdir(tmp_path)
        project_skills = tmp_path / ".minion" / "skills"
        project_skills.mkdir(parents=True)
        (project_skills / "commit.yaml").write_text(
            "name: commit\ndescription: custom commit\nprompt: custom prompt\n",
            encoding="utf-8",
        )
        from minion.cli import _list_skills
        printed = []
        with patch("minion.cli.console") as mc:
            mc.print.side_effect = lambda msg, **kw: printed.append(msg)
            _list_skills()
        # At least one line should mention "project" for the overridden commit skill
        assert any("project" in str(m) for m in printed)
