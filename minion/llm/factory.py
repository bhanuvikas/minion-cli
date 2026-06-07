import os
from typing import AsyncIterator, Iterator, Optional

from .anthropic import AnthropicClient
from .base import LLMClient, LLMResponse, Message, StreamEvent
from .openai import OpenAIClient, OpenRouterClient

SUPPORTED_PROVIDERS = ("anthropic", "openai", "openrouter")


class _PlaceholderClient(LLMClient):
    """Stub used during first-run before an API key is configured.

    All API-call methods raise RuntimeError. The TUI onboarding wizard
    replaces this with a real client once the user enters their key.
    """

    @property
    def model_id(self) -> str:
        return os.getenv("MINION_MODEL", "claude-sonnet-4-6")

    @property
    def provider_name(self) -> str:
        return os.getenv("MINION_PROVIDER", "anthropic")

    def complete(self, messages: list[Message], system: str = "") -> LLMResponse:
        raise RuntimeError("API key not configured — type /model to set up")

    def stream(
        self,
        messages: list[Message],
        system: str = "",
        system_dynamic: str = "",
        tools: Optional[list] = None,
    ) -> Iterator[StreamEvent]:
        raise RuntimeError("API key not configured — type /model to set up")
        yield  # type: ignore[misc]

    async def async_stream(
        self,
        messages: list[Message],
        system: str = "",
        system_dynamic: str = "",
        tools: Optional[list] = None,
    ) -> AsyncIterator[StreamEvent]:
        raise RuntimeError("API key not configured — type /model to set up")
        yield  # type: ignore[misc]

    async def async_complete(
        self,
        messages: list[Message],
        system: str = "",
    ) -> LLMResponse:
        raise RuntimeError("API key not configured — type /model to set up")


def get_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    """Return the appropriate LLMClient for the given provider.

    Provider resolution order:
      1. `provider` argument (e.g. from --provider CLI flag)
      2. MINION_PROVIDER environment variable
      3. Default: "anthropic"
    """
    resolved = provider or os.getenv("MINION_PROVIDER", "anthropic")

    match resolved:
        case "anthropic":
            return AnthropicClient(model)
        case "openai":
            return OpenAIClient(model)
        case "openrouter":
            return OpenRouterClient(model)
        case _:
            raise ValueError(
                f"Unknown provider '{resolved}'. "
                f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}"
            )
