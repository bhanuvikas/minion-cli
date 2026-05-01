"""Tests for the async runner — run_prompt_async() and helpers (Phase 12 Step 2).

No live API calls. All LLM interactions mocked via async_stream stub.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minion.conversation import Conversation
from minion.llm.base import LLMResponse, Message, StreamComplete, TextChunk, ToolUseBlock
from minion.runner import _IterationResult, _stream_one_iteration_async, run_prompt_async


# ─── Async client stub ───────────────────────────────────────────────────────

def _make_async_client(events: list):
    """Build a minimal async LLM client stub that yields the given events."""

    async def _gen(*args, **kwargs):
        for e in events:
            yield e

    client = MagicMock()
    client.model_id = "stub-model"
    client.async_stream = _gen
    return client


def _text(text: str) -> TextChunk:
    return TextChunk(text=text)


def _tool(name: str, tool_id: str = "t1", input: dict | None = None) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=input or {})


def _done(stop_reason: str = "end_turn") -> StreamComplete:
    return StreamComplete(stop_reason=stop_reason, input_tokens=10, output_tokens=5, model="stub")


# ─── _stream_one_iteration_async ─────────────────────────────────────────────

class TestStreamOneIterationAsync:

    @pytest.mark.asyncio
    async def test_text_response(self):
        client = _make_async_client([_text("hello "), _text("world"), _done()])
        conv = Conversation()
        conv.add_user("hi")

        result = await _stream_one_iteration_async(client, conv, "sys", silent=True)

        assert result is not None
        assert result.full_text == "hello world"
        assert result.stop_reason == "end_turn"
        assert result.tool_blocks == []

    @pytest.mark.asyncio
    async def test_tool_use_response(self):
        tb = _tool("read_file", "t1", {"path": "foo.py"})
        client = _make_async_client([tb, _done("tool_use")])
        conv = Conversation()
        conv.add_user("read the file")

        result = await _stream_one_iteration_async(client, conv, "sys", silent=True)

        assert result is not None
        assert result.stop_reason == "tool_use"
        assert len(result.tool_blocks) == 1
        assert result.tool_blocks[0].name == "read_file"

    @pytest.mark.asyncio
    async def test_empty_stream_returns_none(self):
        client = _make_async_client([])
        conv = Conversation()
        conv.add_user("hi")

        result = await _stream_one_iteration_async(client, conv, "sys", silent=True)

        assert result is None

    @pytest.mark.asyncio
    async def test_captures_usage(self):
        done = StreamComplete(stop_reason="end_turn", input_tokens=42, output_tokens=13, model="stub")
        client = _make_async_client([_text("ok"), done])
        conv = Conversation()
        conv.add_user("hi")

        result = await _stream_one_iteration_async(client, conv, "sys", silent=True)

        assert result is not None
        assert result.usage is not None
        assert result.usage.input_tokens == 42
        assert result.usage.output_tokens == 13

    @pytest.mark.asyncio
    async def test_error_in_stream_returns_none_and_pops_message(self):
        async def _error_gen(*args, **kwargs):
            raise RuntimeError("network error")
            yield  # make it a generator

        client = MagicMock()
        client.model_id = "stub"
        client.async_stream = _error_gen
        conv = Conversation()
        conv.add_user("hi")

        result = await _stream_one_iteration_async(client, conv, "sys", silent=True)

        assert result is None
        # user message should be popped
        assert len(conv.messages) == 0


# ─── run_prompt_async ─────────────────────────────────────────────────────────

class TestRunPromptAsync:

    @pytest.mark.asyncio
    async def test_simple_text_response(self, capsys):
        client = _make_async_client([_text("The answer is 42."), _done()])
        conv = Conversation()

        result = await run_prompt_async(
            "What is 6×7?", client, conv, "be helpful",
            capture_output=True,
        )

        assert result == "The answer is 42."

    @pytest.mark.asyncio
    async def test_returns_none_in_streaming_mode(self, capsys):
        client = _make_async_client([_text("hi"), _done()])
        conv = Conversation()

        result = await run_prompt_async(
            "hello", client, conv, "sys",
            capture_output=False,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_tool_use_loop(self):
        """One tool_use iteration followed by end_turn."""
        tool_events = [_tool("read_file", "t1", {"path": "x.py"}), _done("tool_use")]
        final_events = [_text("File read."), _done()]

        call_count = 0

        async def _gen(*args, **kwargs):
            nonlocal call_count
            events = tool_events if call_count == 0 else final_events
            call_count += 1
            for e in events:
                yield e

        client = MagicMock()
        client.model_id = "stub"
        client.async_stream = _gen

        conv = Conversation()

        with patch("minion.runner.ToolExecutor") as MockExecutor:
            inst = MockExecutor.return_value
            inst.execute_async = AsyncMock(return_value="contents of x.py")
            result = await run_prompt_async(
                "read x.py", client, conv, "sys",
                capture_output=True,
            )

        assert result == "File read."
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_respects_max_iterations(self):
        """Loop exits at max_iterations without infinite looping."""
        async def _always_tool(*args, **kwargs):
            yield _tool("read_file", "tx")
            yield _done("tool_use")

        client = MagicMock()
        client.model_id = "stub"
        client.async_stream = _always_tool

        conv = Conversation()

        with patch("minion.runner.ToolExecutor") as MockExecutor:
            inst = MockExecutor.return_value
            inst.execute_async = AsyncMock(return_value="result")
            result = await run_prompt_async(
                "loop", client, conv, "sys",
                max_iterations=3, capture_output=True,
            )

        assert result is None  # never reached end_turn

    @pytest.mark.asyncio
    async def test_dry_run_stops_after_first_tool(self):
        client = _make_async_client([_tool("run_shell", "t1", {"command": "ls"}), _done("tool_use")])
        conv = Conversation()

        result = await run_prompt_async(
            "list files", client, conv, "sys",
            dry_run=True, capture_output=True,
        )

        # dry_run breaks immediately — no second LLM call, returns None
        assert result is None
