"""Tests for minion/runner.py — the ReAct agent loop.

All LLM calls and tool executions are mocked — no real API calls or filesystem ops.
Tests call run_prompt_async() directly since run_prompt() is now a thin asyncio.run() wrapper.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minion.conversation import Conversation
from minion.llm.base import LLMResponse, Message, StreamComplete, TextChunk, ToolUseBlock
from minion.output import OutputRenderer
from minion.reflection import ReflectionConfig, ReflectionResult, CritiqueResult
from minion.runner import run_prompt_async, MAX_ITERATIONS


def _mock_renderer():
    r = MagicMock(spec=OutputRenderer)
    r.spinner.return_value.__enter__ = MagicMock(return_value=None)
    r.spinner.return_value.__exit__ = MagicMock(return_value=False)
    r.parallel_display = None
    return r


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _done(stop_reason: str = "end_turn", model: str = "test-model") -> StreamComplete:
    return StreamComplete(stop_reason=stop_reason, input_tokens=10, output_tokens=5, model=model)


def _text_stream(*texts: str, stop_reason: str = "end_turn", model: str = "test-model") -> list:
    events = [TextChunk(text=t) for t in texts]
    events.append(_done(stop_reason=stop_reason, model=model))
    return events


def _make_async_client(events: list | None = None, exception: Exception | None = None):
    """Build an async LLM client stub. Tracks calls via MagicMock wrapping."""
    if exception is not None:
        async def _raise_gen(*args, **kwargs):
            raise exception
            yield  # make it an async generator function
        client = MagicMock()
        client.model_id = "test-model"
        client.async_stream = MagicMock(side_effect=_raise_gen)
        return client

    _events = events if events is not None else _text_stream("Hello", " world")
    async def _gen(*args, **kwargs):
        for e in _events:
            yield e
    client = MagicMock()
    client.model_id = "test-model"
    client.async_stream = MagicMock(side_effect=_gen)
    return client


def _make_multi_call_client(*event_lists):
    """Build an async client that returns different events on successive calls."""
    calls = list(event_lists)
    call_idx = [0]
    async def _gen(*args, **kwargs):
        idx = call_idx[0]
        evs = calls[min(idx, len(calls) - 1)]
        call_idx[0] += 1
        for e in evs:
            yield e
    client = MagicMock()
    client.model_id = "test-model"
    client.async_stream = MagicMock(side_effect=_gen)
    return client


_SYSTEM_PROMPT = "You are a test assistant."


# ─── Message structure ────────────────────────────────────────────────────────

class TestRunPromptArguments:
    @pytest.mark.asyncio
    async def test_sends_user_message_to_stream(self):
        client = _make_async_client()
        with patch("minion.runner.console"), patch("sys.stdout"):
            await run_prompt_async("what is a closure?", client, Conversation(), _SYSTEM_PROMPT)
        messages = client.async_stream.call_args[0][0]
        assert messages[0] == Message(role="user", content="what is a closure?")

    @pytest.mark.asyncio
    async def test_passes_system_prompt_as_kwarg(self):
        client = _make_async_client()
        with patch("minion.runner.console"), patch("sys.stdout"):
            await run_prompt_async("hi", client, Conversation(), _SYSTEM_PROMPT)
        _, kwargs = client.async_stream.call_args
        assert "system" in kwargs
        assert kwargs["system"] == _SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_passes_tools_as_kwarg(self):
        client = _make_async_client()
        with patch("minion.runner.console"), patch("sys.stdout"):
            await run_prompt_async("hi", client, Conversation(), _SYSTEM_PROMPT)
        _, kwargs = client.async_stream.call_args
        assert "tools" in kwargs
        assert isinstance(kwargs["tools"], list)
        assert len(kwargs["tools"]) > 0


# ─── Error handling ───────────────────────────────────────────────────────────

class TestRunPromptErrorHandling:
    @pytest.mark.asyncio
    async def test_empty_stream_shows_error(self):
        client = _make_async_client(events=[])
        mock_r = _mock_renderer()
        with patch("minion.runner.console"):
            await run_prompt_async("hello", client, Conversation(), _SYSTEM_PROMPT, renderer=mock_r)
        mock_r.on_error.assert_called_once()
        assert "empty" in mock_r.on_error.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_exception_during_stream_shows_error(self):
        client = _make_async_client(exception=ValueError("ANTHROPIC_API_KEY not set"))
        mock_r = _mock_renderer()
        with patch("minion.runner.console"):
            await run_prompt_async("hello", client, Conversation(), _SYSTEM_PROMPT, renderer=mock_r)
        mock_r.on_error.assert_called_once_with("ANTHROPIC_API_KEY not set")

    @pytest.mark.asyncio
    async def test_exception_emits_llm_error_trace(self):
        """A stream exception must emit an llm_error trace event with error text and latency_ms."""
        client = _make_async_client(exception=RuntimeError("bad API key"))
        emitted: list[dict] = []

        def _capture_emit(event_type, **kwargs):
            emitted.append({"type": event_type, **kwargs})

        with patch("minion.runner.console"), \
             patch("minion.runner.print_error"), \
             patch("minion.runner.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit.side_effect = _capture_emit
            await run_prompt_async("hello", client, Conversation(), _SYSTEM_PROMPT)

        error_events = [e for e in emitted if e["type"] == "llm_error"]
        assert len(error_events) == 1
        assert "bad API key" in error_events[0]["error"]
        assert "latency_ms" in error_events[0]
        assert isinstance(error_events[0]["latency_ms"], int)


# ─── Output ───────────────────────────────────────────────────────────────────

class TestRunPromptOutput:
    @pytest.mark.asyncio
    async def test_text_chunks_written_to_stdout(self, capsys):
        client = _make_async_client(events=_text_stream("Bello", " from", " Minion"))
        with patch("minion.runner.console") as mc:
            mc.print = MagicMock()
            await run_prompt_async("hi", client, Conversation(), _SYSTEM_PROMPT)
        captured = capsys.readouterr()
        assert "Bello" in captured.out
        assert " from" in captured.out
        assert " Minion" in captured.out

    @pytest.mark.asyncio
    async def test_print_usage_called_after_stream(self):
        client = _make_async_client()
        mock_r = _mock_renderer()
        with patch("minion.runner.console"), patch("sys.stdout"):
            await run_prompt_async("hello", client, Conversation(), _SYSTEM_PROMPT, renderer=mock_r)
        mock_r.on_session_summary.assert_called_once()


# ─── Agent loop: tool use ─────────────────────────────────────────────────────

class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_tool_call_followed_by_end_turn(self):
        """One tool use iteration then a final text response."""
        tool_block = ToolUseBlock(id="toolu_01", name="read_file", input={"path": "test.txt"})
        tool_stream = [
            TextChunk(text="Let me check that file."),
            tool_block,
            _done(stop_reason="tool_use"),
        ]
        final_stream = [
            TextChunk(text="The file contains: hello"),
            _done(),
        ]
        client = _make_multi_call_client(tool_stream, final_stream)

        with patch("minion.runner.console"), \
             patch("minion.runner.ToolExecutor") as MockExecutor, \
             patch("sys.stdout"):
            mock_exec = MockExecutor.return_value
            mock_exec.execute_async = AsyncMock(return_value="file contents here")
            await run_prompt_async("read test.txt", client, Conversation(), _SYSTEM_PROMPT)

        assert client.async_stream.call_count == 2
        mock_exec.execute_async.assert_called_once_with(tool_block)

    @pytest.mark.asyncio
    async def test_dry_run_passed_to_executor(self):
        client = _make_async_client()
        mock_r = _mock_renderer()
        with patch("minion.runner.console"), \
             patch("minion.runner.ToolExecutor") as MockExecutor, \
             patch("sys.stdout"):
            await run_prompt_async("hi", client, Conversation(), _SYSTEM_PROMPT,
                                   dry_run=True, renderer=mock_r)
        MockExecutor.assert_called_once_with(
            dry_run=True, mcp_manager=None, agent_runner=None, agent_label=None,
            remote_task_runner=None, confirm_callback=None,
            approval_mode="off", permission_store=None, hook_runner=None,
            confirmation_manager=None,
            renderer=mock_r,
        )

    @pytest.mark.asyncio
    async def test_max_iterations_shows_limit_message(self):
        """Loop should stop and show a message after MAX_ITERATIONS tool-use responses."""
        tool_block = ToolUseBlock(id="toolu_loop", name="read_file", input={"path": "x"})

        async def _always_tool(*args, **kwargs):
            yield tool_block
            yield _done(stop_reason="tool_use")

        client = MagicMock()
        client.model_id = "test-model"
        client.async_stream = _always_tool
        mock_r = _mock_renderer()

        with patch("minion.runner.console"), \
             patch("minion.runner.ToolExecutor") as MockExecutor, \
             patch("sys.stdout"):
            MockExecutor.return_value.execute_async = AsyncMock(return_value="result")
            await run_prompt_async("loop forever", client, Conversation(), _SYSTEM_PROMPT,
                                   renderer=mock_r)

        mock_r.on_iteration_limit.assert_called_once_with(MAX_ITERATIONS)


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
    async def _run(self, reflect_config=None, verbose=False):
        client = _make_async_client()
        conv = Conversation()
        with patch("minion.runner.console"), patch("sys.stdout"):
            await run_prompt_async("hi", client, conv, _SYSTEM_PROMPT_REFL,
                                   reflect_config=reflect_config, verbose=verbose)
        return conv

    @pytest.mark.asyncio
    async def test_reflection_not_called_when_config_is_none(self):
        with patch("minion.runner.reflect") as mock_reflect:
            await self._run(reflect_config=None)
        mock_reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflection_not_called_when_depth_zero(self):
        with patch("minion.runner.reflect") as mock_reflect:
            await self._run(reflect_config=ReflectionConfig(depth=0))
        mock_reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_reflection_called_after_end_turn(self):
        result = _make_passing_reflection_result()
        with patch("minion.runner.reflect", return_value=result) as mock_reflect:
            await self._run(reflect_config=ReflectionConfig(depth=1))
        mock_reflect.assert_called_once()

    @pytest.mark.asyncio
    async def test_reflection_not_called_during_tool_loop(self):
        """reflect() must not run on intermediate tool-use iterations."""
        tool_block = ToolUseBlock(id="t1", name="read_file", input={"path": "x.py"})
        tool_stream = [tool_block, _done(stop_reason="tool_use")]
        final_stream = [TextChunk(text="Done."), _done()]
        client = _make_multi_call_client(tool_stream, final_stream)
        reflect_result = _make_passing_reflection_result("Done.")

        with patch("minion.runner.console"), \
             patch("minion.runner.ToolExecutor") as MockExec, \
             patch("minion.runner.reflect", return_value=reflect_result) as mock_reflect, \
             patch("sys.stdout"):
            MockExec.return_value.execute_async = AsyncMock(return_value="file contents")
            await run_prompt_async("read x.py", client, Conversation(), _SYSTEM_PROMPT_REFL,
                                   reflect_config=ReflectionConfig(depth=1))

        # reflect() called once — only after the final end_turn, not after tool_use
        mock_reflect.assert_called_once()

    @pytest.mark.asyncio
    async def test_refined_response_replaces_conversation_message(self):
        """When was_refined=True the last conversation message should be the refined text."""
        refined_result = _make_refined_reflection_result(
            original="def foo(): pass",
            refined="def foo(x): return x",
        )
        conv = Conversation()
        client = _make_async_client(events=_text_stream("def foo(): pass"))

        with patch("minion.runner.console"), \
             patch("minion.runner.reflect", return_value=refined_result), \
             patch("sys.stdout"):
            await run_prompt_async("write foo", client, conv, _SYSTEM_PROMPT_REFL,
                                   reflect_config=ReflectionConfig(depth=1))

        last_msg = conv.messages[-1]
        assert last_msg.role == "assistant"
        assert last_msg.content == "def foo(x): return x"

    @pytest.mark.asyncio
    async def test_original_message_preserved_when_not_refined(self):
        passing_result = _make_passing_reflection_result("Hello world")
        conv = Conversation()
        client = _make_async_client(events=_text_stream("Hello world"))

        with patch("minion.runner.console"), \
             patch("minion.runner.reflect", return_value=passing_result), \
             patch("sys.stdout"):
            await run_prompt_async("hi", client, conv, _SYSTEM_PROMPT_REFL,
                                   reflect_config=ReflectionConfig(depth=1))

        last_msg = conv.messages[-1]
        assert last_msg.content == "Hello world"

    @pytest.mark.asyncio
    async def test_verbose_false_suppresses_print_critique(self):
        result = _make_passing_reflection_result()
        with patch("minion.runner.reflect", return_value=result), \
             patch("minion.runner.print_critique") as mock_pc:
            await self._run(reflect_config=ReflectionConfig(depth=1), verbose=False)
        mock_pc.assert_not_called()

    @pytest.mark.asyncio
    async def test_verbose_true_calls_print_critique(self):
        result = _make_passing_reflection_result()
        with patch("minion.runner.reflect", return_value=result), \
             patch("minion.runner.print_critique") as mock_pc:
            await self._run(reflect_config=ReflectionConfig(depth=1), verbose=True)
        mock_pc.assert_called_once()
