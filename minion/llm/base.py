from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator, Optional, Union


# ─── Tool definition (provider-neutral) ──────────────────────────────────────

@dataclass
class ToolDefinition:
    """Provider-neutral tool schema.

    ``parameters`` holds a JSON Schema object dict (same structure as Anthropic's
    ``input_schema`` or OpenAI's ``parameters``). Each LLM adapter converts this
    to its own wire format internally — callers never build provider-specific dicts.
    """
    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


# ─── Rate-limit exceptions ────────────────────────────────────────────────────

class InputTokenRateLimitError(Exception):
    """Raised when a 429 is caused by exceeding the input-token-per-minute limit.

    Distinct from other RateLimitErrors (requests/min, output tokens/min) because
    the right response is NOT to wait and retry — the context is still the same
    size after 60s. The caller should compact the conversation and then retry.
    """


# ─── Application-level content block types ───────────────────────────────────
# These are the canonical types the application uses to represent message content.
# Adapters translate these to provider-specific wire formats in _format_messages().
# The application layer (conversation.py, runner.py) never sees provider dicts.

@dataclass
class ContentTextBlock:
    """A text segment within an assistant message."""
    text: str


@dataclass
class ContentToolUseBlock:
    """A tool call made by the model within an assistant message."""
    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class ContentToolResultBlock:
    """The result of a tool call, injected back as a user-role message."""
    tool_use_id: str
    content: str


ContentBlock = Union[ContentTextBlock, ContentToolUseBlock, ContentToolResultBlock]


@dataclass
class Message:
    role: str                           # "user" | "assistant"
    # str when the turn has no tool involvement; list[ContentBlock] when it does.
    # Adapters must branch on isinstance(content, str) in _format_messages().
    content: Union[str, list[ContentBlock]]  # str for plain turns; list for tool turns


@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


# ─── Typed stream events (Phase 3) ───────────────────────────────────────────
# stream() yields a sequence of these instead of bare strings.
# The runner inspects each event type to decide what to render vs execute.

@dataclass
class TextChunk:
    """A fragment of the model's text response, ready to write to stdout."""
    text: str


@dataclass
class ToolUseBlock:
    """A fully-assembled tool call emitted by the model.

    Arrives after all input_json_delta events for a content block have been
    accumulated — the runner never sees partial JSON.
    """
    id: str
    name: str
    input: dict = field(default_factory=dict)


@dataclass
class ToolAccumulationStart:
    """Emitted when the model begins streaming a tool call's JSON input.

    Arrives before the corresponding ToolUseBlock (which is only emitted after the
    full input JSON has been assembled). Lets the runner show a progress indicator
    while the model is generating potentially large tool inputs (e.g. write_file).
    """
    name: str  # tool name, e.g. "write_file"


@dataclass
class StreamComplete:
    """Signals the end of a streaming response, carrying stop reason and usage.

    stop_reason values:
      "end_turn"  — model finished responding; no further LLM calls needed
      "tool_use"  — model emitted ≥1 ToolUseBlock; execute them and loop
    """
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


StreamEvent = Union[TextChunk, ToolAccumulationStart, ToolUseBlock, StreamComplete]


class LLMClient(ABC):
    """Provider-agnostic interface for LLM calls.

    Every adapter (Anthropic, OpenAI, OpenRouter) implements these two methods.
    The rest of the codebase only ever calls this interface — never importing
    provider SDKs directly. Swapping models is a one-line config change.
    """

    @abstractmethod
    def complete(self, messages: list[Message], system: str = "") -> LLMResponse:
        """Non-streaming call. Returns the full response at once.

        Used internally by phases that need the full text before proceeding
        (e.g., reflection critique, planner). Not used for interactive output.
        """
        ...

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        system: str = "",
        system_dynamic: str = "",
        tools: Optional[list["ToolDefinition"]] = None,
    ) -> Iterator[StreamEvent]:
        """Streaming call. Yields typed StreamEvent objects.

        Callers iterate the stream and dispatch on event type:
          TextChunk     — write text to stdout immediately
          ToolUseBlock  — tool call to execute; inject result and loop
          StreamComplete — stop_reason + usage; signals end of this iteration
        """
        ...

    @abstractmethod
    async def async_stream(
        self,
        messages: list[Message],
        system: str = "",
        system_dynamic: str = "",
        tools: Optional[list["ToolDefinition"]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """Async streaming call. Yields typed StreamEvent objects.

        Async equivalent of stream() for use inside an asyncio event loop.
        Adapters that don't support async raise NotImplementedError.
        """
        raise NotImplementedError
        # mypy needs this to recognise the return type as AsyncIterator
        yield  # type: ignore[misc]

    @abstractmethod
    async def async_complete(
        self,
        messages: list[Message],
        system: str = "",
    ) -> LLMResponse:
        """Async non-streaming call. Async equivalent of complete()."""
        raise NotImplementedError

    @property
    @abstractmethod
    def model_id(self) -> str:
        """The model identifier string, e.g. 'claude-sonnet-4-5'."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name, e.g. 'anthropic'."""
        ...

    @property
    def last_usage(self) -> Optional[LLMResponse]:
        """Usage metadata from the most recent stream() call.

        Returns None until stream() has been fully consumed at least once.
        Populated by each adapter after the stream is exhausted.
        """
        return None
