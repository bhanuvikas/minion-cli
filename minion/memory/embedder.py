"""Embedder — convert text to vectors for semantic similarity search.

  Embedder        — abstract base class
  OpenAIEmbedder  — uses text-embedding-3-small (requires OPENAI_API_KEY)
  build_embedder()— factory: returns OpenAIEmbedder when key is set, else None

When build_embedder() returns None, MemoryStore falls back to keyword search
over file content. All episodic memory features remain fully functional;
semantic search is the only degraded capability.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod


class Embedder(ABC):
    """Abstract base for all embedding strategies."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Return the embedding vector for text."""
        ...

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """True if this embedder is configured and can make calls."""
        ...


class OpenAIEmbedder(Embedder):
    """Embeds text using OpenAI's text-embedding-3-small model (1536 dims).

    Uses the openai library already present in the project's dependencies.
    """

    MODEL = "text-embedding-3-small"
    DIMENSIONS = 1536

    def __init__(self, api_key: str) -> None:
        import openai
        self._client = openai.OpenAI(api_key=api_key)

    def embed(self, text: str) -> list[float]:
        """Call the OpenAI embeddings API and return the vector."""
        response = self._client.embeddings.create(
            model=self.MODEL,
            input=text,
        )
        return response.data[0].embedding

    @property
    def is_available(self) -> bool:
        return True


def build_embedder() -> Embedder | None:
    """Return an OpenAIEmbedder if OPENAI_API_KEY is set in the environment.

    Returns None when no key is found. Callers must handle the None case
    by falling back to keyword search.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        return OpenAIEmbedder(api_key)
    return None
