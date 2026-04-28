import os
from typing import AsyncIterator, Iterator, Optional

from openai import OpenAI

from .base import LLMClient, LLMResponse, Message, StreamEvent

OPENAI_DEFAULT_MODEL = "gpt-4o"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"


class OpenAIClient(LLMClient):
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "OPENAI_API_KEY is not set. Add it to your .env file."
            )
        # base_url=None uses OpenAI's default endpoint
        self._client = OpenAI(api_key=resolved_key, base_url=base_url)
        self._model = model or os.getenv("MINION_MODEL", OPENAI_DEFAULT_MODEL)
        self._provider_name = "openai"
        self._last_usage: Optional[LLMResponse] = None

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def last_usage(self) -> Optional[LLMResponse]:
        return self._last_usage

    def _build_messages(self, messages: list[Message], system: str) -> list[dict]:
        """Translate application-level Messages to OpenAI wire format.

        OpenAI tool use (tool_calls, role="tool") is deferred — tool content
        blocks are not yet translated. Plain-text messages pass through unchanged.
        """
        result = []
        if system:
            result.append({"role": "system", "content": system})
        for m in messages:
            content = m.content if isinstance(m.content, str) else str(m.content)
            result.append({"role": m.role, "content": content})
        return result

    def complete(self, messages: list[Message], system: str = "") -> LLMResponse:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=self._build_messages(messages, system),
            max_tokens=8192,
        )
        return LLMResponse(
            content=response.choices[0].message.content or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            model=response.model,
        )

    def stream(
        self,
        messages: list[Message],
        system: str = "",
        tools: Optional[list] = None,
    ) -> Iterator:
        # tools parameter accepted but ignored — OpenAI tool use deferred to a later phase.
        # stream_options={"include_usage": True} makes the final chunk carry
        # usage data (prompt_tokens, completion_tokens). Without this flag,
        # OpenAI streaming gives no usage info at all.
        from .base import StreamComplete, TextChunk
        response = self._client.chat.completions.create(
            model=self._model,
            messages=self._build_messages(messages, system),
            max_tokens=8192,
            stream=True,
            stream_options={"include_usage": True},
        )
        usage_data = None
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield TextChunk(text=chunk.choices[0].delta.content)
            # The final chunk has no choices but carries usage
            if chunk.usage:
                usage_data = chunk.usage
                self._last_usage = LLMResponse(
                    content="",
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                    model=chunk.model,
                )
        if usage_data:
            yield StreamComplete(
                stop_reason="end_turn",
                input_tokens=usage_data.prompt_tokens,
                output_tokens=usage_data.completion_tokens,
                model=self._model,
            )

    async def async_complete(
        self,
        messages: list[Message],
        system: str = "",
    ) -> LLMResponse:
        raise NotImplementedError("OpenAI async support is deferred to Phase 13")

    async def async_stream(
        self,
        messages: list[Message],
        system: str = "",
        tools: Optional[list] = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("OpenAI async support is deferred to Phase 13")
        yield  # type: ignore[misc]


class OpenRouterClient(OpenAIClient):
    """OpenRouter exposes an OpenAI-compatible API that proxies 100+ models.

    Using it is identical to OpenAIClient — just point at a different base_url
    with an OpenRouter API key. This is a common pattern: many LLM providers
    offer OpenAI-compatible endpoints so you get broad model access for free.
    """

    def __init__(self, model: str | None = None) -> None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is not set. Add it to your .env file."
            )
        base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        super().__init__(
            model=model or os.getenv("MINION_MODEL", OPENROUTER_DEFAULT_MODEL),
            api_key=api_key,
            base_url=base_url,
        )
        self._provider_name = "openrouter"
