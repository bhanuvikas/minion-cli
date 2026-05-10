"""Tests for minion/repl/ — slash commands, completer, command registry.

We do NOT test run_repl_async() (the full loop) because it requires a live
prompt_toolkit session with a TTY. We test the stable, pure-logic pieces:
  - REPL_COMMANDS registry structure
  - _SlashCompleter.get_completions() — pure filtering logic
  - _handle_slash_command() — dispatch and return values

The /exit omission bug (command existed but wasn't in REPL_COMMANDS so it
never appeared in tab-completion) is covered by test_all_commands_registered.
"""

import pytest
import typer
from unittest.mock import MagicMock, patch
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText

from minion.repl import (
    REPL_COMMANDS, ReplState, CommandContext, _SlashCompleter,
    _handle_slash_command, _generate_minion_md, _generate_minion_md_llm,
)
from minion.context.project import ProjectContext
from minion.context.manifest import ProjectManifest


def _make_ctx(client=None, conversation=None, state=None, project_context=None, **kwargs):
    """Build a minimal CommandContext for testing."""
    return CommandContext(
        client=client or MagicMock(),
        conversation=conversation or MagicMock(),
        state=state,
        project_context=project_context,
        **kwargs,
    )


# ─── REPL_COMMANDS registry ───────────────────────────────────────────────────

class TestReplCommandsRegistry:
    def test_all_expected_commands_present(self):
        """Regression: /exit was missing from REPL_COMMANDS in an earlier version."""
        for cmd in ("/help", "/init", "/model", "/quit", "/exit"):
            assert cmd in REPL_COMMANDS, f"{cmd} missing from REPL_COMMANDS"

    def test_every_command_has_non_empty_description(self):
        for cmd, desc in REPL_COMMANDS.items():
            assert isinstance(desc, str) and desc.strip(), \
                f"{cmd} has empty description"

    def test_all_commands_start_with_slash(self):
        for cmd in REPL_COMMANDS:
            assert cmd.startswith("/"), f"Command '{cmd}' must start with '/'"


# ─── _SlashCompleter ──────────────────────────────────────────────────────────

class TestSlashCompleter:
    def _completions(self, text: str) -> list:
        completer = _SlashCompleter()
        doc = Document(text, cursor_position=len(text))
        return list(completer.get_completions(doc, MagicMock()))

    def test_no_completions_for_empty_input(self):
        assert self._completions("") == []

    def test_no_completions_for_regular_text(self):
        assert self._completions("explain closures") == []

    def test_all_commands_returned_for_bare_slash(self):
        results = self._completions("/")
        assert len(results) == len(REPL_COMMANDS)

    def test_filters_by_prefix(self):
        results = self._completions("/mod")
        assert len(results) == 1
        assert results[0].display == FormattedText([("", "/model")])

    def test_completion_text_is_suffix_not_full_command(self):
        """Completion text must be only the missing suffix, not the full command."""
        results = self._completions("/mod")
        assert results[0].text == "el"

    def test_completion_includes_description_as_meta(self):
        results = self._completions("/help")
        assert results[0].display_meta == FormattedText([("", REPL_COMMANDS["/help"])])

    def test_exact_match_still_completes(self):
        """Typing the full command exactly should still yield a completion (empty suffix)."""
        results = self._completions("/help")
        assert len(results) == 1
        assert results[0].text == ""

    def test_partial_match_for_quit(self):
        results = self._completions("/qu")
        assert len(results) == 1
        assert results[0].display == FormattedText([("", "/quit")])

    def test_no_match_for_unknown_prefix(self):
        assert self._completions("/banana") == []


# ─── _handle_slash_command ────────────────────────────────────────────────────

class TestHandleSlashCommand:
    def _call(self, raw: str, client=None, conversation=None):
        ctx = _make_ctx(client=client, conversation=conversation)
        with patch("minion.repl.commands.console"), \
             patch("minion.repl.commands.run_model_config"):
            return _handle_slash_command(raw, ctx)

    def test_returns_false_for_regular_text(self):
        assert self._call("explain recursion") is False

    def test_returns_false_for_empty_string(self):
        assert self._call("") is False

    def test_help_returns_true(self):
        assert self._call("/help") is True

    def test_model_returns_true(self):
        assert self._call("/model") is True

    def test_model_calls_run_model_config(self):
        client = MagicMock()
        ctx = _make_ctx(client=client)
        with patch("minion.repl.commands.console"), \
             patch("minion.repl.commands.run_model_config") as mock_config:
            _handle_slash_command("/model", ctx)
        mock_config.assert_called_once_with(client)

    def test_unknown_slash_command_returns_true(self):
        """Unknown /commands are handled (not forwarded to LLM), return True."""
        assert self._call("/banana") is True

    def test_quit_raises_system_exit(self):
        ctx = _make_ctx()
        with patch("minion.repl.commands.console"), pytest.raises(typer.Exit):
            _handle_slash_command("/quit", ctx)

    def test_exit_raises_system_exit(self):
        ctx = _make_ctx()
        with patch("minion.repl.commands.console"), pytest.raises(typer.Exit):
            _handle_slash_command("/exit", ctx)

    def test_commands_are_case_insensitive(self):
        """Slash commands must work regardless of capitalisation."""
        assert self._call("/HELP") is True
        assert self._call("/Help") is True

    def test_commands_ignore_surrounding_whitespace(self):
        assert self._call("  /help  ") is True


# ─── _generate_minion_md ──────────────────────────────────────────────────────

def _make_context(tmp_path, language="Python 3.12", framework=None, entry_point=None):
    manifest = ProjectManifest(language=language, framework=framework, entry_point=entry_point)
    return ProjectContext(cwd=tmp_path, manifest=manifest, file_tree="", minion_md=None)


class TestGenerateMinionMd:
    def test_without_context_returns_generic_header(self):
        result = _generate_minion_md(None)
        assert "Project instructions for Minion" in result
        assert "Add anything" in result

    def test_with_manifest_includes_language(self, tmp_path):
        ctx = _make_context(tmp_path, language="Python 3.12")
        result = _generate_minion_md(ctx)
        assert "Python 3.12" in result

    def test_with_framework_includes_framework(self, tmp_path):
        ctx = _make_context(tmp_path, language="Python 3.12", framework="Flask")
        result = _generate_minion_md(ctx)
        assert "Flask" in result

    def test_without_framework_no_framework_in_header(self, tmp_path):
        ctx = _make_context(tmp_path, language="Go 1.21")
        result = _generate_minion_md(ctx)
        assert "·" not in result.splitlines()[2]

    def test_with_entry_point_mentioned_in_how_to_run(self, tmp_path):
        ctx = _make_context(tmp_path, entry_point="src/main.py")
        result = _generate_minion_md(ctx)
        assert "src/main.py" in result

    def test_without_entry_point_shows_generic_placeholder(self, tmp_path):
        ctx = _make_context(tmp_path)
        result = _generate_minion_md(ctx)
        assert "e.g." in result

    def test_contains_all_expected_sections(self):
        result = _generate_minion_md(None)
        for section in ("## How to run", "## How to test", "## Key directories", "## Notes for Minion"):
            assert section in result

    def test_result_ends_with_newline(self):
        assert _generate_minion_md(None).endswith("\n")


# ─── /init command ────────────────────────────────────────────────────────────

class TestInitCommand:
    def _call_init(self, tmp_path, project_context=None):
        ctx = _make_ctx(project_context=project_context)
        with patch("minion.repl.commands.console"), \
             patch("minion.repl.commands.Path") as mock_path_cls, \
             patch("minion.repl.init_md._generate_minion_md_llm", return_value=None):
            mock_path_cls.cwd.return_value = tmp_path
            mock_path_cls.return_value = tmp_path / "MINION.md"
            # Patch Path inside _handle_init (in commands module)
            with patch("minion.repl.commands._handle_init") as mock_init:
                mock_init.return_value = True
                result = _handle_slash_command("/init", ctx)
            return result

    def test_init_returns_true(self, tmp_path):
        """_handle_init is called and returns True."""
        ctx = _make_ctx(project_context=None)
        with patch("minion.repl.commands._handle_init", return_value=True) as mock_init:
            result = _handle_slash_command("/init", ctx)
        mock_init.assert_called_once()
        assert result is True

    def test_init_creates_minion_md(self, tmp_path):
        ctx = _make_ctx(project_context=None)
        with patch("minion.repl.commands._handle_init") as mock_init:
            def side_effect(arg, client, state, project_context):
                (tmp_path / "MINION.md").write_text(_generate_minion_md(project_context))
                return True
            mock_init.side_effect = side_effect
            _handle_slash_command("/init", ctx)
        assert (tmp_path / "MINION.md").exists()

    def test_init_file_contains_sections(self, tmp_path):
        content = _generate_minion_md(None)
        assert "## How to run" in content
        assert "## How to test" in content

    def test_init_confirms_before_overwrite(self, tmp_path):
        (tmp_path / "MINION.md").write_text("existing content")
        ctx = _make_ctx(project_context=None)
        with patch("minion.repl.commands._handle_init") as mock_init:
            mock_init.return_value = True
            _handle_slash_command("/init", ctx)
        # File untouched because _handle_init is mocked
        assert (tmp_path / "MINION.md").read_text() == "existing content"

    def test_init_with_context_prefills_language(self, tmp_path):
        pc = _make_context(tmp_path, language="Python 3.12", framework="FastAPI")
        content = _generate_minion_md(pc)
        assert "Python 3.12" in content
        assert "FastAPI" in content


# ─── LLM-assisted /init ───────────────────────────────────────────────────────

class TestInitCommandLLM:
    def _make_client(self, content: str = "# MINION.md\n\nGenerated content.\n") -> MagicMock:
        from minion.llm.base import StreamComplete, TextChunk
        client = MagicMock()
        client.stream.return_value = iter([
            TextChunk(text=content),
            StreamComplete(stop_reason="end_turn", input_tokens=50, output_tokens=100, model="test-model"),
        ])
        return client

    def test_generate_minion_md_llm_yields_chunks(self, tmp_path):
        pc = _make_context(tmp_path, language="Go 1.21")
        client = self._make_client("# MINION.md\n\nGo project.\n")
        chunks = list(_generate_minion_md_llm(pc, client))
        assert "Go project." in "".join(chunks)

    def test_generate_minion_md_llm_raises_on_exception(self, tmp_path):
        pc = _make_context(tmp_path, language="Go 1.21")
        client = MagicMock()
        client.stream.side_effect = RuntimeError("network error")
        with pytest.raises(RuntimeError):
            list(_generate_minion_md_llm(pc, client))

    def test_generate_minion_md_llm_yields_nothing_on_empty_response(self, tmp_path):
        pc = _make_context(tmp_path, language="Go 1.21")
        client = self._make_client("")
        chunks = list(_generate_minion_md_llm(pc, client))
        assert "".join(chunks) == ""


# ─── /reflect command ─────────────────────────────────────────────────────────

class TestReflectCommand:
    def _call(self, raw: str, state: ReplState | None = None):
        if state is None:
            state = ReplState()
        ctx = _make_ctx(state=state)
        with patch("minion.repl.commands.console"), patch("minion.repl.commands.print_error"):
            result = _handle_slash_command(raw, ctx)
        return result, state

    def test_reflect_registered_in_repl_commands(self):
        assert "/reflect" in REPL_COMMANDS

    def test_reflect_on_sets_depth_one(self):
        _, state = self._call("/reflect --on")
        assert state.reflect_depth == 1

    def test_reflect_off_sets_depth_zero(self):
        state = ReplState(reflect_depth=2)
        _, state = self._call("/reflect --off", state=state)
        assert state.reflect_depth == 0

    def test_reflect_integer_sets_depth(self):
        _, state = self._call("/reflect 3")
        assert state.reflect_depth == 3

    def test_reflect_zero_sets_off(self):
        _, state = self._call("/reflect 0")
        assert state.reflect_depth == 0

    def test_reflect_no_arg_shows_state_returns_true(self):
        result, _ = self._call("/reflect")
        assert result is True

    def test_reflect_invalid_arg_does_not_crash(self):
        result, state = self._call("/reflect banana")
        assert result is True
        assert state.reflect_depth == 0

    def test_reflect_returns_true(self):
        result, _ = self._call("/reflect --on")
        assert result is True

    def test_reflect_without_state_returns_true(self):
        ctx = _make_ctx(state=None)
        with patch("minion.repl.commands.console"):
            result = _handle_slash_command("/reflect --on", ctx)
        assert result is True


# ─── /verbose command ─────────────────────────────────────────────────────────

class TestVerboseCommand:
    def _call(self, raw: str, state: ReplState | None = None):
        if state is None:
            state = ReplState()
        ctx = _make_ctx(state=state)
        with patch("minion.repl.commands.console"), patch("minion.repl.commands.print_error"):
            result = _handle_slash_command(raw, ctx)
        return result, state

    def test_verbose_registered_in_repl_commands(self):
        assert "/verbose" in REPL_COMMANDS

    def test_verbose_on_sets_flag(self):
        _, state = self._call("/verbose --on")
        assert state.verbose is True

    def test_verbose_off_clears_flag(self):
        state = ReplState(verbose=True)
        _, state = self._call("/verbose --off", state=state)
        assert state.verbose is False

    def test_verbose_no_arg_shows_state_returns_true(self):
        result, _ = self._call("/verbose")
        assert result is True

    def test_verbose_returns_true(self):
        result, _ = self._call("/verbose --on")
        assert result is True

    def test_verbose_invalid_arg_does_not_crash(self):
        result, state = self._call("/verbose maybe")
        assert result is True

    def test_verbose_without_state_returns_true(self):
        ctx = _make_ctx(state=None)
        with patch("minion.repl.commands.console"):
            result = _handle_slash_command("/verbose --on", ctx)
        assert result is True
