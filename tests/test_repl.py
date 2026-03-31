"""Tests for minion/repl.py — slash commands, completer, command registry.

We do NOT test run_repl() (the full loop) because it requires a live
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

from minion.repl import REPL_COMMANDS, _SlashCompleter, _handle_slash_command


# ─── REPL_COMMANDS registry ───────────────────────────────────────────────────

class TestReplCommandsRegistry:
    def test_all_expected_commands_present(self):
        """Regression: /exit was missing from REPL_COMMANDS in an earlier version."""
        for cmd in ("/help", "/model", "/quit", "/exit"):
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
        if client is None:
            client = MagicMock()
        if conversation is None:
            conversation = MagicMock()
        with patch("minion.repl.console"), \
             patch("minion.repl.run_model_config"):
            return _handle_slash_command(raw, client, conversation)

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
        with patch("minion.repl.console"), \
             patch("minion.repl.run_model_config") as mock_config:
            _handle_slash_command("/model", client, MagicMock())
        mock_config.assert_called_once_with(client)

    def test_unknown_slash_command_returns_true(self):
        """Unknown /commands are handled (not forwarded to LLM), return True."""
        assert self._call("/banana") is True

    def test_quit_raises_system_exit(self):
        with patch("minion.repl.console"), pytest.raises(typer.Exit):
            _handle_slash_command("/quit", MagicMock(), MagicMock())

    def test_exit_raises_system_exit(self):
        with patch("minion.repl.console"), pytest.raises(typer.Exit):
            _handle_slash_command("/exit", MagicMock(), MagicMock())

    def test_commands_are_case_insensitive(self):
        """Slash commands must work regardless of capitalisation."""
        assert self._call("/HELP") is True
        assert self._call("/Help") is True

    def test_commands_ignore_surrounding_whitespace(self):
        assert self._call("  /help  ") is True
