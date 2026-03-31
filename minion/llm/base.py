from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str


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
    def stream(self, messages: list[Message], system: str = "") -> Iterator[str]:
        """Streaming call. Yields text chunks as they arrive.

        Used for all interactive output so the terminal feels alive rather than
        showing a blank screen until the full response is ready.
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
