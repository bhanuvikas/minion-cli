"""Tests for minion/runner.py — the core prompt → LLM → stream pipeline.

We mock the LLM client so no real API calls are made.
The key behaviours tested:
  - Correct message structure passed to client.stream()
  - Empty stream → error displayed, no crash
  - Exception from client → error displayed, no crash
  - Streaming chunks written to stdout
  - print_usage called after stream exhausted
"""

import pytest
from unittest.mock import MagicMock, call, patch

from minion.llm.base import LLMResponse, Message
from minion.runner import run_prompt


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_client(chunks=None, last_usage=None):
    """Return a mock LLMClient that streams the given chunks."""
    client = MagicMock()
    client.stream.return_value = iter(chunks if chunks is not None else ["Hello", " world"])
    client.last_usage = last_usage
    return client


def _make_status_ctx():
    """Return a MagicMock that behaves as a context manager (for console.status)."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestRunPromptArguments:
    def test_sends_user_message_to_stream(self):
        """client.stream() must receive exactly one user Message with the prompt text."""
        client = _make_client()
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("what is a closure?", client)

        messages = client.stream.call_args[0][0]
        assert len(messages) == 1
        assert messages[0] == Message(role="user", content="what is a closure?")

    def test_passes_system_prompt_as_kwarg(self):
        """The system prompt must be forwarded as the 'system' keyword argument."""
        client = _make_client()
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client)

        _, kwargs = client.stream.call_args
        assert "system" in kwargs
        assert len(kwargs["system"]) > 0  # non-empty system prompt


class TestRunPromptErrorHandling:
    def test_empty_stream_shows_error(self):
        """An empty response (no chunks) must show an error, not crash."""
        client = _make_client(chunks=[])
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_error") as mock_err:
            mc.status.return_value = ctx
            run_prompt("hello", client)

        mock_err.assert_called_once()
        assert "empty" in mock_err.call_args[0][0].lower()

    def test_exception_during_stream_init_shows_error(self):
        """If client.stream() raises, the error is shown cleanly."""
        client = MagicMock()
        client.stream.side_effect = ValueError("ANTHROPIC_API_KEY not set")
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_error") as mock_err:
            mc.status.return_value = ctx
            run_prompt("hello", client)

        mock_err.assert_called_once_with("ANTHROPIC_API_KEY not set")


class TestRunPromptOutput:
    def test_chunks_written_to_stdout(self, capsys):
        """Each streamed chunk must reach stdout."""
        client = _make_client(chunks=["Bello", " from", " Minion"])
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc:
            mc.status.return_value = ctx
            mc.print = MagicMock()
            run_prompt("hi", client)

        captured = capsys.readouterr()
        assert "Bello" in captured.out
        assert " from" in captured.out
        assert " Minion" in captured.out

    def test_print_usage_called_after_stream(self):
        """Token usage must be displayed after the stream is exhausted."""
        usage = LLMResponse(content="", input_tokens=10, output_tokens=5, model="test")
        client = _make_client(chunks=["hi"], last_usage=usage)
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_usage") as mock_usage, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hello", client)

        mock_usage.assert_called_once_with(usage)

    def test_print_usage_called_with_none_when_no_usage(self):
        """print_usage must still be called even when last_usage is None."""
        client = _make_client(last_usage=None)
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_usage") as mock_usage, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hello", client)

        mock_usage.assert_called_once_with(None)
