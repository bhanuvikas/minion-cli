"""Tests for minion/memory/embedder.py — Embedder ABC and OpenAIEmbedder.

All OpenAI API calls are mocked. No real network requests.
"""

import pytest
from unittest.mock import MagicMock, patch

from minion.memory.embedder import OpenAIEmbedder, build_embedder


# ─── build_embedder() ─────────────────────────────────────────────────────────

class TestBuildEmbedder:
    def test_returns_none_when_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert build_embedder() is None

    def test_returns_openai_embedder_when_key_set(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        with patch("minion.memory.embedder.OpenAIEmbedder.__init__", return_value=None):
            embedder = build_embedder()
        assert isinstance(embedder, OpenAIEmbedder)

    def test_uses_openai_api_key_env_var(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-123")
        with patch("openai.OpenAI") as mock_openai:
            build_embedder()
        mock_openai.assert_called_once_with(api_key="sk-test-key-123")


# ─── OpenAIEmbedder ───────────────────────────────────────────────────────────

class TestOpenAIEmbedder:
    def _make_embedder(self) -> OpenAIEmbedder:
        with patch("openai.OpenAI"):
            embedder = OpenAIEmbedder(api_key="sk-test")
        return embedder

    def test_is_available_true(self):
        embedder = self._make_embedder()
        assert embedder.is_available is True

    def test_embed_calls_embeddings_create(self):
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.embeddings.create.return_value = MagicMock(
                data=[MagicMock(embedding=[0.1] * 1536)]
            )
            embedder = OpenAIEmbedder(api_key="sk-test")
            result = embedder.embed("hello world")

        mock_client.embeddings.create.assert_called_once_with(
            model=OpenAIEmbedder.MODEL,
            input="hello world",
        )
        assert result == [0.1] * 1536

    def test_embed_returns_list_of_floats(self):
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.embeddings.create.return_value = MagicMock(
                data=[MagicMock(embedding=[0.5, 0.3, 0.2])]
            )
            embedder = OpenAIEmbedder(api_key="sk-test")
            result = embedder.embed("test")

        assert isinstance(result, list)
        assert all(isinstance(x, float) for x in result)

    def test_embed_uses_correct_model(self):
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.embeddings.create.return_value = MagicMock(
                data=[MagicMock(embedding=[0.0])]
            )
            embedder = OpenAIEmbedder(api_key="sk-test")
            embedder.embed("text")

        call_kwargs = mock_client.embeddings.create.call_args
        assert call_kwargs.kwargs["model"] == "text-embedding-3-small"
