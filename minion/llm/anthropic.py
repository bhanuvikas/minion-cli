import os
from typing import Iterator

import anthropic

from .base import LLMClient, LLMResponse, Message

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

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def _format_messages(self, messages: list[Message]) -> list[dict]:
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

    def stream(self, messages: list[Message], system: str = "") -> Iterator[str]:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": self._format_messages(messages),
        }
        if system:
            kwargs["system"] = system

        # The `with` block keeps the HTTP connection open while we iterate.
        # text_stream yields decoded string chunks as they arrive from the API.
        with self._client.messages.stream(**kwargs) as stream:
            yield from stream.text_stream
