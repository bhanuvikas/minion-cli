import json
import os
from typing import Iterator, Optional

import anthropic

from .base import LLMClient, LLMResponse, Message, StreamComplete, StreamEvent, TextChunk, ToolUseBlock

DEFAULT_MODEL = "claude-sonnet-4-5"


class AnthropicClient(LLMClient):
    def __init__(self, model: str | None = None) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
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
        # content is str for normal turns, list of blocks for tool turns — both
        # are accepted by the Anthropic API without transformation.
        return [{"role": m.role, "content": m.content} for m in messages]

    def complete(self, messages: list[Message], system: str = "") -> LLMResponse:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": self._format_messages(messages),
        }
        if system:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)
        return LLMResponse(
            content=response.content[0].text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
        )

    def stream(
        self,
        messages: list[Message],
        system: str = "",
        tools: Optional[list] = None,
    ) -> Iterator[StreamEvent]:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": self._format_messages(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        # current_tool accumulates state for the tool_use content block being
        # streamed — id and name come from ContentBlockStart; input JSON arrives
        # as incremental deltas and is assembled here before yielding.
        current_tool: Optional[dict] = None

        with self._client.messages.stream(**kwargs) as stream_ctx:
            for event in stream_ctx:
                event_type = event.type

                if event_type == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        current_tool = {"id": block.id, "name": block.name, "json_buf": ""}

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
            self._last_usage = LLMResponse(
                content="",
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
                model=final.model,
            )
            yield StreamComplete(
                stop_reason=final.stop_reason,
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
                model=final.model,
            )
