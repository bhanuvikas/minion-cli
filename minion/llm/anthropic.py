import asyncio
import json
import os
import time
from typing import AsyncIterator, Iterator, Optional

import anthropic

from .base import (
    ContentTextBlock, ContentToolResultBlock, ContentToolUseBlock,
    InputTokenRateLimitError,
    LLMClient, LLMResponse, Message, StreamComplete, StreamEvent, TextChunk,
    ToolAccumulationStart, ToolUseBlock,
)

DEFAULT_MODEL = "claude-sonnet-4-5"

_MAX_RETRY = 3
_RETRY_WAIT_SECONDS = 60


def _rate_limit_wait() -> None:
    from ..theme import console as _console
    with _console.status("", spinner="dots") as status:
        for remaining in range(_RETRY_WAIT_SECONDS, 0, -1):
            status.update(f"[yellow]⚠ Rate limited — retrying in {remaining}s...[/]")
            time.sleep(1)


async def _rate_limit_wait_async() -> None:
    from ..theme import console as _console
    with _console.status("", spinner="dots") as status:
        for remaining in range(_RETRY_WAIT_SECONDS, 0, -1):
            status.update(f"[yellow]⚠ Rate limited — retrying in {remaining}s...[/]")
            await asyncio.sleep(1)

# Per-model output token ceilings (Anthropic API limits).
# Models not listed fall back to 8192 (conservative safe default).
_MODEL_MAX_TOKENS: dict[str, int] = {
    "claude-opus-4-6":              32000,
    "claude-sonnet-4-6":            64000,
    "claude-haiku-4-5-20251001":     8192,
    "claude-sonnet-4-5":            64000,
    "claude-opus-4-5":              32000,
}

def _max_tokens_for(model: str) -> int:
    """Return the output token ceiling for a given model ID."""
    for prefix, limit in _MODEL_MAX_TOKENS.items():
        if model.startswith(prefix):
            return limit
    return 8192


class AnthropicClient(LLMClient):
    def __init__(self, model: str | None = None) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._async_client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model or os.getenv("MINION_MODEL", DEFAULT_MODEL)
        self._last_usage: Optional[LLMResponse] = None

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def last_usage(self) -> Optional[LLMResponse]:
        return self._last_usage

    def _format_messages(self, messages: list[Message]) -> list[dict]:
        """Translate application-level Messages to Anthropic wire format.

        Plain-text content passes through unchanged. ContentBlock lists are
        translated to Anthropic's typed dict format here — the only place in
        the codebase that knows about Anthropic's specific message structure.
        """
        return [{"role": m.role, "content": self._format_content(m.content)} for m in messages]

    def _format_content(self, content) -> str | list[dict]:
        if isinstance(content, str):
            return content
        result = []
        for block in content:
            if isinstance(block, ContentTextBlock):
                result.append({"type": "text", "text": block.text})
            elif isinstance(block, ContentToolUseBlock):
                result.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
            elif isinstance(block, ContentToolResultBlock):
                result.append({"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content})
        return result

    def complete(self, messages: list[Message], system: str = "") -> LLMResponse:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": _max_tokens_for(self._model),
            "messages": self._format_messages(messages),
        }
        if system:
            kwargs["system"] = system

        for attempt in range(_MAX_RETRY):
            try:
                response = self._client.messages.create(**kwargs)
                return LLMResponse(
                    content=response.content[0].text,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model=response.model,
                )
            except anthropic.RateLimitError as e:
                if "input tokens" in str(e).lower():
                    raise InputTokenRateLimitError(str(e)) from e
                if attempt < _MAX_RETRY - 1:
                    _rate_limit_wait()
                else:
                    raise

    def stream(
        self,
        messages: list[Message],
        system: str = "",
        system_dynamic: str = "",
        tools: Optional[list] = None,
    ) -> Iterator[StreamEvent]:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": _max_tokens_for(self._model),
            "messages": self._format_messages(messages),
        }
        if system and system_dynamic:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system_dynamic},
            ]
        elif system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
            ]
        if tools:
            tools_cached = [dict(t) for t in tools]
            tools_cached[-1] = {**tools_cached[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tools_cached

        for attempt in range(_MAX_RETRY):
            # current_tool accumulates state for the tool_use content block being
            # streamed — reset on each attempt so partial state doesn't carry over.
            current_tool: Optional[dict] = None
            try:
                with self._client.messages.stream(**kwargs) as stream_ctx:
                    for event in stream_ctx:
                        event_type = event.type

                        if event_type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                current_tool = {"id": block.id, "name": block.name, "json_buf": ""}
                                yield ToolAccumulationStart(name=block.name)

                        elif event_type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                yield TextChunk(text=delta.text)
                            elif delta.type == "input_json_delta" and current_tool is not None:
                                current_tool["json_buf"] += delta.partial_json

                        elif event_type == "content_block_stop":
                            if current_tool is not None:
                                tool_input = json.loads(current_tool["json_buf"]) if current_tool["json_buf"] else {}
                                yield ToolUseBlock(
                                    id=current_tool["id"],
                                    name=current_tool["name"],
                                    input=tool_input,
                                )
                                current_tool = None

                    final = stream_ctx.get_final_message()
                    _cache_read = getattr(final.usage, "cache_read_input_tokens", 0) or 0
                    _cache_creation = getattr(final.usage, "cache_creation_input_tokens", 0) or 0
                    self._last_usage = LLMResponse(
                        content="",
                        input_tokens=final.usage.input_tokens,
                        output_tokens=final.usage.output_tokens,
                        model=final.model,
                        cache_read_tokens=_cache_read,
                        cache_creation_tokens=_cache_creation,
                    )
                    yield StreamComplete(
                        stop_reason=final.stop_reason,
                        input_tokens=final.usage.input_tokens,
                        output_tokens=final.usage.output_tokens,
                        model=final.model,
                        cache_read_tokens=_cache_read,
                        cache_creation_tokens=_cache_creation,
                    )
                return  # success
            except anthropic.RateLimitError as e:
                if "input tokens" in str(e).lower():
                    raise InputTokenRateLimitError(str(e)) from e
                if attempt < _MAX_RETRY - 1:
                    _rate_limit_wait()
                else:
                    raise

    async def async_complete(
        self,
        messages: list[Message],
        system: str = "",
    ) -> LLMResponse:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": _max_tokens_for(self._model),
            "messages": self._format_messages(messages),
        }
        if system:
            kwargs["system"] = system

        for attempt in range(_MAX_RETRY):
            try:
                response = await self._async_client.messages.create(**kwargs)
                return LLMResponse(
                    content=response.content[0].text,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    model=response.model,
                )
            except anthropic.RateLimitError as e:
                if "input tokens" in str(e).lower():
                    raise InputTokenRateLimitError(str(e)) from e
                if attempt < _MAX_RETRY - 1:
                    await _rate_limit_wait_async()
                else:
                    raise

    async def async_stream(
        self,
        messages: list[Message],
        system: str = "",
        system_dynamic: str = "",
        tools: Optional[list] = None,
    ) -> AsyncIterator[StreamEvent]:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": _max_tokens_for(self._model),
            "messages": self._format_messages(messages),
        }
        if system and system_dynamic:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": system_dynamic},
            ]
        elif system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
            ]
        if tools:
            tools_cached = [dict(t) for t in tools]
            tools_cached[-1] = {**tools_cached[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tools_cached

        for attempt in range(_MAX_RETRY):
            current_tool: Optional[dict] = None
            try:
                async with self._async_client.messages.stream(**kwargs) as stream_ctx:
                    async for event in stream_ctx:
                        event_type = event.type

                        if event_type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                current_tool = {"id": block.id, "name": block.name, "json_buf": ""}
                                yield ToolAccumulationStart(name=block.name)

                        elif event_type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                yield TextChunk(text=delta.text)
                            elif delta.type == "input_json_delta" and current_tool is not None:
                                current_tool["json_buf"] += delta.partial_json

                        elif event_type == "content_block_stop":
                            if current_tool is not None:
                                tool_input = json.loads(current_tool["json_buf"]) if current_tool["json_buf"] else {}
                                yield ToolUseBlock(
                                    id=current_tool["id"],
                                    name=current_tool["name"],
                                    input=tool_input,
                                )
                                current_tool = None

                    final = await stream_ctx.get_final_message()
                    _cache_read = getattr(final.usage, "cache_read_input_tokens", 0) or 0
                    _cache_creation = getattr(final.usage, "cache_creation_input_tokens", 0) or 0
                    self._last_usage = LLMResponse(
                        content="",
                        input_tokens=final.usage.input_tokens,
                        output_tokens=final.usage.output_tokens,
                        model=final.model,
                        cache_read_tokens=_cache_read,
                        cache_creation_tokens=_cache_creation,
                    )
                    yield StreamComplete(
                        stop_reason=final.stop_reason,
                        input_tokens=final.usage.input_tokens,
                        output_tokens=final.usage.output_tokens,
                        model=final.model,
                        cache_read_tokens=_cache_read,
                        cache_creation_tokens=_cache_creation,
                    )
                return  # success
            except anthropic.RateLimitError as e:
                if "input tokens" in str(e).lower():
                    raise InputTokenRateLimitError(str(e)) from e
                if attempt < _MAX_RETRY - 1:
                    await _rate_limit_wait_async()
                else:
                    raise
