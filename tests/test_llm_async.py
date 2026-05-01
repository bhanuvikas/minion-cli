"""Tests for the async LLM interface (Step 1 of Phase 12).

All tests mock the Anthropic API — no live API calls.
Verifies that:
  - AnthropicClient.async_stream() yields the same StreamEvent types as stream()
  - AnthropicClient.async_complete() returns an LLMResponse
  - OpenAIClient stubs raise NotImplementedError
  - The base class abstract methods are wired correctly
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minion.llm.anthropic import AnthropicClient
from minion.llm.base import (
    LLMResponse, Message, StreamComplete, TextChunk, ToolUseBlock,
)
from minion.llm.openai import OpenAIClient


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _msg(text: str) -> Message:
    return Message(role="user", content=text)


def _make_text_event(text: str):
    """Fake content_block_delta event with text_delta."""
    e = MagicMock()
    e.type = "content_block_delta"
    e.delta = MagicMock()
    e.delta.type = "text_delta"
    e.delta.text = text
    return e


def _make_tool_start_event(tool_id: str, name: str):
    e = MagicMock()
    e.type = "content_block_start"
    e.content_block = MagicMock()
    e.content_block.type = "tool_use"
    e.content_block.id = tool_id
    e.content_block.name = name
    return e


def _make_tool_delta_event(partial_json: str):
    e = MagicMock()
    e.type = "content_block_delta"
    e.delta = MagicMock()
    e.delta.type = "input_json_delta"
    e.delta.partial_json = partial_json
    return e


def _make_block_stop_event():
    e = MagicMock()
    e.type = "content_block_stop"
    return e


def _make_final_message(stop_reason: str = "end_turn", input_tok: int = 10, output_tok: int = 5):
    fm = MagicMock()
    fm.stop_reason = stop_reason
    fm.usage = MagicMock()
    fm.usage.input_tokens = input_tok
    fm.usage.output_tokens = output_tok
    fm.model = "claude-sonnet-4-6"
    return fm


async def _collect(ait) -> list:
    """Collect all items from an async iterator."""
    result = []
    async for item in ait:
        result.append(item)
    return result


# ─── AnthropicClient async_stream ────────────────────────────────────────────

class TestAnthropicAsyncStream:
    """async_stream() yields same event types as sync stream()."""

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        return AnthropicClient(model="claude-sonnet-4-6")

    @pytest.mark.asyncio
    async def test_yields_text_chunks(self, client):
        events = [_make_text_event("hello "), _make_text_event("world")]
        final = _make_final_message()

        async_stream_ctx = AsyncMock()
        async_stream_ctx.__aenter__ = AsyncMock(return_value=async_stream_ctx)
        async_stream_ctx.__aexit__ = AsyncMock(return_value=None)
        async_stream_ctx.__aiter__ = MagicMock(return_value=aiter_from(events))
        async_stream_ctx.get_final_message = AsyncMock(return_value=final)

        with patch.object(client._async_client.messages, "stream", return_value=async_stream_ctx):
            collected = await _collect(client.async_stream([_msg("hi")]))

        text_chunks = [e for e in collected if isinstance(e, TextChunk)]
        assert [c.text for c in text_chunks] == ["hello ", "world"]

    @pytest.mark.asyncio
    async def test_yields_stream_complete(self, client):
        final = _make_final_message(stop_reason="end_turn", input_tok=20, output_tok=10)

        async_stream_ctx = AsyncMock()
        async_stream_ctx.__aenter__ = AsyncMock(return_value=async_stream_ctx)
        async_stream_ctx.__aexit__ = AsyncMock(return_value=None)
        async_stream_ctx.__aiter__ = MagicMock(return_value=aiter_from([]))
        async_stream_ctx.get_final_message = AsyncMock(return_value=final)

        with patch.object(client._async_client.messages, "stream", return_value=async_stream_ctx):
            collected = await _collect(client.async_stream([_msg("hi")]))

        completes = [e for e in collected if isinstance(e, StreamComplete)]
        assert len(completes) == 1
        assert completes[0].stop_reason == "end_turn"
        assert completes[0].input_tokens == 20
        assert completes[0].output_tokens == 10

    @pytest.mark.asyncio
    async def test_yields_tool_use_block(self, client):
        events = [
            _make_tool_start_event("toolu_01", "read_file"),
            _make_tool_delta_event('{"path": "'),
            _make_tool_delta_event('foo.py"}'),
            _make_block_stop_event(),
        ]
        final = _make_final_message(stop_reason="tool_use")

        async_stream_ctx = AsyncMock()
        async_stream_ctx.__aenter__ = AsyncMock(return_value=async_stream_ctx)
        async_stream_ctx.__aexit__ = AsyncMock(return_value=None)
        async_stream_ctx.__aiter__ = MagicMock(return_value=aiter_from(events))
        async_stream_ctx.get_final_message = AsyncMock(return_value=final)

        with patch.object(client._async_client.messages, "stream", return_value=async_stream_ctx):
            collected = await _collect(client.async_stream([_msg("read foo.py")], tools=[]))

        tools = [e for e in collected if isinstance(e, ToolUseBlock)]
        assert len(tools) == 1
        assert tools[0].id == "toolu_01"
        assert tools[0].name == "read_file"
        assert tools[0].input == {"path": "foo.py"}

    @pytest.mark.asyncio
    async def test_updates_last_usage(self, client):
        final = _make_final_message(input_tok=30, output_tok=15)

        async_stream_ctx = AsyncMock()
        async_stream_ctx.__aenter__ = AsyncMock(return_value=async_stream_ctx)
        async_stream_ctx.__aexit__ = AsyncMock(return_value=None)
        async_stream_ctx.__aiter__ = MagicMock(return_value=aiter_from([]))
        async_stream_ctx.get_final_message = AsyncMock(return_value=final)

        with patch.object(client._async_client.messages, "stream", return_value=async_stream_ctx):
            await _collect(client.async_stream([_msg("hi")]))

        assert client.last_usage is not None
        assert client.last_usage.input_tokens == 30
        assert client.last_usage.output_tokens == 15

    @pytest.mark.asyncio
    async def test_passes_system_prompt(self, client):
        final = _make_final_message()

        async_stream_ctx = AsyncMock()
        async_stream_ctx.__aenter__ = AsyncMock(return_value=async_stream_ctx)
        async_stream_ctx.__aexit__ = AsyncMock(return_value=None)
        async_stream_ctx.__aiter__ = MagicMock(return_value=aiter_from([]))
        async_stream_ctx.get_final_message = AsyncMock(return_value=final)

        with patch.object(client._async_client.messages, "stream", return_value=async_stream_ctx) as mock_stream:
            await _collect(client.async_stream([_msg("hi")], system="Be concise"))

        call_kwargs = mock_stream.call_args[1]
        system = call_kwargs.get("system")
        assert isinstance(system, list) and system[0]["text"] == "Be concise"


# ─── AnthropicClient async_complete ──────────────────────────────────────────

class TestAnthropicAsyncComplete:

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        return AnthropicClient(model="claude-sonnet-4-6")

    @pytest.mark.asyncio
    async def test_returns_llm_response(self, client):
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="The answer is 42.")]
        fake_resp.usage = MagicMock(input_tokens=5, output_tokens=8)
        fake_resp.model = "claude-sonnet-4-6"

        with patch.object(client._async_client.messages, "create", new=AsyncMock(return_value=fake_resp)):
            result = await client.async_complete([_msg("What is 6×7?")])

        assert isinstance(result, LLMResponse)
        assert result.content == "The answer is 42."
        assert result.input_tokens == 5
        assert result.output_tokens == 8

    @pytest.mark.asyncio
    async def test_passes_system_prompt(self, client):
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="ok")]
        fake_resp.usage = MagicMock(input_tokens=1, output_tokens=1)
        fake_resp.model = "claude-sonnet-4-6"

        with patch.object(client._async_client.messages, "create", new=AsyncMock(return_value=fake_resp)) as mock_create:
            await client.async_complete([_msg("hi")], system="You are helpful")

        kwargs = mock_create.call_args[1]
        assert kwargs.get("system") == "You are helpful"


# ─── OpenAI async stubs ───────────────────────────────────────────────────────

class TestOpenAIAsyncStubs:

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        from unittest.mock import patch as p
        with p("openai.OpenAI"):
            return OpenAIClient(model="gpt-4o")

    @pytest.mark.asyncio
    async def test_async_complete_raises(self, client):
        with pytest.raises(NotImplementedError):
            await client.async_complete([_msg("hi")])

    @pytest.mark.asyncio
    async def test_async_stream_raises(self, client):
        with pytest.raises(NotImplementedError):
            async for _ in client.async_stream([_msg("hi")]):
                pass


# ─── Async iterator helper ────────────────────────────────────────────────────

def aiter_from(items):
    """Return an object that supports __aiter__ / __anext__ over a list."""

    class _Iter:
        def __init__(self):
            self._iter = iter(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    return _Iter()
