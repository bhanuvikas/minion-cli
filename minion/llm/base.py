from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Optional, Union


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
    content: Union[str, list[ContentBlock]]  # str for plain turns; list for tool turns


@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str


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


StreamEvent = Union[TextChunk, ToolUseBlock, StreamComplete]


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
        tools: Optional[list] = None,
    ) -> Iterator[StreamEvent]:
        """Streaming call. Yields typed StreamEvent objects.

        Callers iterate the stream and dispatch on event type:
          TextChunk     — write text to stdout immediately
          ToolUseBlock  — tool call to execute; inject result and loop
          StreamComplete — stop_reason + usage; signals end of this iteration
        """
        ...

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
