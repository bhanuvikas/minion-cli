import os

from .anthropic import AnthropicClient
from .base import LLMClient
from .openai import OpenAIClient, OpenRouterClient

SUPPORTED_PROVIDERS = ("anthropic", "openai", "openrouter")


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
