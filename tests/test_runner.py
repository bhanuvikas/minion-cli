"""Tests for minion/runner.py — the ReAct agent loop.

All LLM calls and tool executions are mocked — no real API calls or filesystem ops.
"""

import pytest
from unittest.mock import MagicMock, patch

from minion.llm.base import LLMResponse, Message, StreamComplete, TextChunk, ToolUseBlock
from minion.runner import run_prompt, MAX_ITERATIONS


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _usage(input_tokens=10, output_tokens=5, model="test-model"):
    return LLMResponse(content="", input_tokens=input_tokens, output_tokens=output_tokens, model=model)


def _text_stream(*texts: str, stop_reason: str = "end_turn", model: str = "test-model"):
    """Return a stream of TextChunk events followed by StreamComplete."""
    usage = _usage(model=model)
    events = [TextChunk(text=t) for t in texts]
    events.append(StreamComplete(
        stop_reason=stop_reason,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        model=model,
    ))
    return iter(events)


def _make_client(events=None, last_usage=None):
    """Return a mock LLMClient that streams the given events."""
    client = MagicMock()
    client.stream.return_value = _text_stream("Hello", " world") if events is None else iter(events)
    client.last_usage = last_usage
    return client


def _make_status_ctx():
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=None)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ─── Message structure ────────────────────────────────────────────────────────

class TestRunPromptArguments:
    def test_sends_user_message_to_stream(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("what is a closure?", client)
        messages = client.stream.call_args[0][0]
        assert len(messages) == 1
        assert messages[0] == Message(role="user", content="what is a closure?")

    def test_passes_system_prompt_as_kwarg(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client)
        _, kwargs = client.stream.call_args
        assert "system" in kwargs
        assert len(kwargs["system"]) > 0

    def test_passes_tools_as_kwarg(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client)
        _, kwargs = client.stream.call_args
        assert "tools" in kwargs
        assert isinstance(kwargs["tools"], list)
        assert len(kwargs["tools"]) > 0


# ─── Error handling ───────────────────────────────────────────────────────────

class TestRunPromptErrorHandling:
    def test_empty_stream_shows_error(self):
        client = _make_client(events=[])
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_error") as mock_err:
            mc.status.return_value = ctx
            run_prompt("hello", client)
        mock_err.assert_called_once()
        assert "empty" in mock_err.call_args[0][0].lower()

    def test_exception_during_stream_shows_error(self):
        client = MagicMock()
        client.stream.side_effect = ValueError("ANTHROPIC_API_KEY not set")
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_error") as mock_err:
            mc.status.return_value = ctx
            run_prompt("hello", client)
        mock_err.assert_called_once_with("ANTHROPIC_API_KEY not set")


# ─── Output ───────────────────────────────────────────────────────────────────

class TestRunPromptOutput:
    def test_text_chunks_written_to_stdout(self, capsys):
        client = _make_client(events=list(_text_stream("Bello", " from", " Minion")))
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
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_usage") as mock_usage, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hello", client)
        mock_usage.assert_called_once()


# ─── Agent loop: tool use ─────────────────────────────────────────────────────

class TestAgentLoop:
    def test_tool_call_followed_by_end_turn(self):
        """One tool use iteration then a final text response."""
        tool_block = ToolUseBlock(id="toolu_01", name="read_file", input={"path": "test.txt"})
        tool_stream = [
            TextChunk(text="Let me check that file."),
            tool_block,
            StreamComplete(stop_reason="tool_use", input_tokens=10, output_tokens=5, model="test"),
        ]
        final_stream = [
            TextChunk(text="The file contains: hello"),
            StreamComplete(stop_reason="end_turn", input_tokens=20, output_tokens=8, model="test"),
        ]

        client = MagicMock()
        client.stream.side_effect = [iter(tool_stream), iter(final_stream)]
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.ToolExecutor") as MockExecutor, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            mock_exec = MockExecutor.return_value
            mock_exec.execute.return_value = "file contents here"
            run_prompt("read test.txt", client)

        assert client.stream.call_count == 2
        mock_exec.execute.assert_called_once_with(tool_block)

    def test_dry_run_passed_to_executor(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, \
             patch("minion.runner.ToolExecutor") as MockExecutor, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client, dry_run=True)
        MockExecutor.assert_called_once_with(dry_run=True)

    def test_max_iterations_shows_limit_message(self):
        """Loop should stop and show a message after MAX_ITERATIONS tool-use responses."""
        tool_block = ToolUseBlock(id="toolu_loop", name="read_file", input={"path": "x"})

        def _tool_stream():
            return iter([
                tool_block,
                StreamComplete(stop_reason="tool_use", input_tokens=5, output_tokens=2, model="test"),
            ])

        client = MagicMock()
        client.stream.side_effect = [_tool_stream() for _ in range(MAX_ITERATIONS)]
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.ToolExecutor") as MockExecutor, \
             patch("minion.runner.print_iteration_limit") as mock_limit, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            MockExecutor.return_value.execute.return_value = "result"
            run_prompt("loop forever", client)

        mock_limit.assert_called_once_with(MAX_ITERATIONS)
