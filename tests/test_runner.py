"""Tests for minion/runner.py — the ReAct agent loop.

All LLM calls and tool executions are mocked — no real API calls or filesystem ops.
"""

import pytest
from unittest.mock import MagicMock, patch

from minion.conversation import Conversation
from minion.llm.base import LLMResponse, Message, StreamComplete, TextChunk, ToolUseBlock
from minion.reflection import ReflectionConfig, ReflectionResult, CritiqueResult
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

_SYSTEM_PROMPT = "You are a test assistant."


class TestRunPromptArguments:
    def test_sends_user_message_to_stream(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("what is a closure?", client, Conversation(), _SYSTEM_PROMPT)
        messages = client.stream.call_args[0][0]
        assert messages[0] == Message(role="user", content="what is a closure?")

    def test_passes_system_prompt_as_kwarg(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client, Conversation(), _SYSTEM_PROMPT)
        _, kwargs = client.stream.call_args
        assert "system" in kwargs
        assert kwargs["system"] == _SYSTEM_PROMPT

    def test_passes_tools_as_kwarg(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client, Conversation(), _SYSTEM_PROMPT)
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
            run_prompt("hello", client, Conversation(), _SYSTEM_PROMPT)
        mock_err.assert_called_once()
        assert "empty" in mock_err.call_args[0][0].lower()

    def test_exception_during_stream_shows_error(self):
        client = MagicMock()
        client.stream.side_effect = ValueError("ANTHROPIC_API_KEY not set")
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_error") as mock_err:
            mc.status.return_value = ctx
            run_prompt("hello", client, Conversation(), _SYSTEM_PROMPT)
        mock_err.assert_called_once_with("ANTHROPIC_API_KEY not set")

    def test_exception_emits_llm_error_trace(self):
        """A stream exception must emit an llm_error trace event with error text and latency_ms."""
        client = MagicMock()
        client.stream.side_effect = RuntimeError("bad API key")
        ctx = _make_status_ctx()
        emitted: list[dict] = []

        def _capture_emit(event_type, **kwargs):
            emitted.append({"type": event_type, **kwargs})

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.print_error"), \
             patch("minion.runner.get_tracer") as mock_tracer:
            mc.status.return_value = ctx
            mock_tracer.return_value.emit.side_effect = _capture_emit
            run_prompt("hello", client, Conversation(), _SYSTEM_PROMPT)

        error_events = [e for e in emitted if e["type"] == "llm_error"]
        assert len(error_events) == 1
        assert "bad API key" in error_events[0]["error"]
        assert "latency_ms" in error_events[0]
        assert isinstance(error_events[0]["latency_ms"], int)


# ─── Output ───────────────────────────────────────────────────────────────────

class TestRunPromptOutput:
    def test_text_chunks_written_to_stdout(self, capsys):
        client = _make_client(events=list(_text_stream("Bello", " from", " Minion")))
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc:
            mc.status.return_value = ctx
            mc.print = MagicMock()
            run_prompt("hi", client, Conversation(), _SYSTEM_PROMPT)
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
            run_prompt("hello", client, Conversation(), _SYSTEM_PROMPT)
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
            run_prompt("read test.txt", client, Conversation(), _SYSTEM_PROMPT)

        assert client.stream.call_count == 2
        mock_exec.execute.assert_called_once_with(tool_block)

    def test_dry_run_passed_to_executor(self):
        client = _make_client()
        ctx = _make_status_ctx()
        with patch("minion.runner.console") as mc, \
             patch("minion.runner.ToolExecutor") as MockExecutor, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client, Conversation(), _SYSTEM_PROMPT, dry_run=True)
        MockExecutor.assert_called_once_with(dry_run=True, mcp_manager=None)

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
            run_prompt("loop forever", client, Conversation(), _SYSTEM_PROMPT)

        mock_limit.assert_called_once_with(MAX_ITERATIONS)


# ─── Reflection integration ───────────────────────────────────────────────────

_SYSTEM_PROMPT_REFL = "You are Minion."


def _make_passing_reflection_result(response: str = "Hello world") -> ReflectionResult:
    return ReflectionResult(
        original_response=response,
        final_response=response,
        rounds=1,
        final_score=8,
        critiques=[CritiqueResult(score=8, response_type="GENERAL", critique="None", raw="")],
        was_refined=False,
    )


def _make_refined_reflection_result(
    original: str = "def foo(): pass",
    refined: str = "def foo(x): return x",
) -> ReflectionResult:
    return ReflectionResult(
        original_response=original,
        final_response=refined,
        rounds=1,
        final_score=8,
        critiques=[CritiqueResult(score=5, response_type="CODE_GENERATION", critique="Fix it.", raw="")],
        was_refined=True,
    )


class TestRunPromptReflection:
    def _run(self, reflect_config=None, verbose=False):
        client = _make_client()
        ctx = _make_status_ctx()
        conv = Conversation()
        with patch("minion.runner.console") as mc, patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client, conv, _SYSTEM_PROMPT_REFL,
                       reflect_config=reflect_config, verbose=verbose)
        return conv

    def test_reflection_not_called_when_config_is_none(self):
        with patch("minion.runner.reflect") as mock_reflect:
            self._run(reflect_config=None)
        mock_reflect.assert_not_called()

    def test_reflection_not_called_when_depth_zero(self):
        with patch("minion.runner.reflect") as mock_reflect:
            self._run(reflect_config=ReflectionConfig(depth=0))
        mock_reflect.assert_not_called()

    def test_reflection_called_after_end_turn(self):
        result = _make_passing_reflection_result()
        with patch("minion.runner.reflect", return_value=result) as mock_reflect:
            self._run(reflect_config=ReflectionConfig(depth=1))
        mock_reflect.assert_called_once()

    def test_reflection_not_called_during_tool_loop(self):
        """reflect() must not run on intermediate tool-use iterations."""
        tool_block = ToolUseBlock(id="t1", name="read_file", input={"path": "x.py"})
        tool_stream = [
            tool_block,
            StreamComplete(stop_reason="tool_use", input_tokens=5, output_tokens=2, model="test"),
        ]
        final_stream = [
            TextChunk(text="Done."),
            StreamComplete(stop_reason="end_turn", input_tokens=10, output_tokens=5, model="test"),
        ]
        client = MagicMock()
        client.stream.side_effect = [iter(tool_stream), iter(final_stream)]
        ctx = _make_status_ctx()
        reflect_result = _make_passing_reflection_result("Done.")

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.ToolExecutor") as MockExec, \
             patch("minion.runner.reflect", return_value=reflect_result) as mock_reflect, \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            MockExec.return_value.execute.return_value = "file contents"
            run_prompt("read x.py", client, Conversation(), _SYSTEM_PROMPT_REFL,
                       reflect_config=ReflectionConfig(depth=1))

        # reflect() called once — only after the final end_turn, not after tool_use
        mock_reflect.assert_called_once()

    def test_refined_response_replaces_conversation_message(self):
        """When was_refined=True the last conversation message should be the refined text."""
        refined_result = _make_refined_reflection_result(
            original="def foo(): pass",
            refined="def foo(x): return x",
        )
        conv = Conversation()
        client = _make_client(events=list(_text_stream("def foo(): pass")))
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.reflect", return_value=refined_result), \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("write foo", client, conv, _SYSTEM_PROMPT_REFL,
                       reflect_config=ReflectionConfig(depth=1))

        last_msg = conv.messages[-1]
        assert last_msg.role == "assistant"
        assert last_msg.content == "def foo(x): return x"

    def test_original_message_preserved_when_not_refined(self):
        passing_result = _make_passing_reflection_result("Hello world")
        conv = Conversation()
        client = _make_client(events=list(_text_stream("Hello world")))
        ctx = _make_status_ctx()

        with patch("minion.runner.console") as mc, \
             patch("minion.runner.reflect", return_value=passing_result), \
             patch("sys.stdout"):
            mc.status.return_value = ctx
            run_prompt("hi", client, conv, _SYSTEM_PROMPT_REFL,
                       reflect_config=ReflectionConfig(depth=1))

        last_msg = conv.messages[-1]
        assert last_msg.content == "Hello world"

    def test_verbose_false_suppresses_print_critique(self):
        result = _make_passing_reflection_result()
        with patch("minion.runner.reflect", return_value=result), \
             patch("minion.runner.print_critique") as mock_pc:
            self._run(reflect_config=ReflectionConfig(depth=1), verbose=False)
        mock_pc.assert_not_called()

    def test_verbose_true_calls_print_critique(self):
        result = _make_passing_reflection_result()
        with patch("minion.runner.reflect", return_value=result), \
             patch("minion.runner.print_critique") as mock_pc:
            self._run(reflect_config=ReflectionConfig(depth=1), verbose=True)
        mock_pc.assert_called_once()
