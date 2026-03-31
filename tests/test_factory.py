"""Tests for the LLM provider factory.

The factory is pure logic — it reads env vars and returns the right client type.
We mock the client constructors so no real API keys or network calls are needed.
"""

import pytest
from unittest.mock import patch, MagicMock

from minion.llm.factory import get_client, SUPPORTED_PROVIDERS
from minion.llm.anthropic import AnthropicClient
from minion.llm.openai import OpenAIClient, OpenRouterClient


class TestGetClient:
    def test_default_provider_is_anthropic(self, monkeypatch):
        monkeypatch.delenv("MINION_PROVIDER", raising=False)
        with patch.object(AnthropicClient, "__init__", return_value=None) as mock:
            client = get_client()
        mock.assert_called_once_with(None)
        assert isinstance(client, AnthropicClient)

    def test_explicit_anthropic(self):
        with patch.object(AnthropicClient, "__init__", return_value=None):
            client = get_client(provider="anthropic")
        assert isinstance(client, AnthropicClient)

    def test_explicit_openai(self):
        with patch.object(OpenAIClient, "__init__", return_value=None):
            client = get_client(provider="openai")
        assert isinstance(client, OpenAIClient)

    def test_explicit_openrouter(self):
        with patch.object(OpenRouterClient, "__init__", return_value=None):
            client = get_client(provider="openrouter")
        assert isinstance(client, OpenRouterClient)

    def test_env_var_provider(self, monkeypatch):
        monkeypatch.setenv("MINION_PROVIDER", "openai")
        with patch.object(OpenAIClient, "__init__", return_value=None):
            client = get_client()
        assert isinstance(client, OpenAIClient)

    def test_explicit_arg_overrides_env(self, monkeypatch):
        """--provider flag should win over MINION_PROVIDER env var."""
        monkeypatch.setenv("MINION_PROVIDER", "openai")
        with patch.object(AnthropicClient, "__init__", return_value=None):
            client = get_client(provider="anthropic")
        assert isinstance(client, AnthropicClient)

    def test_model_forwarded_to_client(self):
        with patch.object(AnthropicClient, "__init__", return_value=None) as mock:
            get_client(provider="anthropic", model="claude-opus-4-5")
        mock.assert_called_once_with("claude-opus-4-5")

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            get_client(provider="banana-ai")

    def test_supported_providers_constant(self):
        assert set(SUPPORTED_PROVIDERS) == {"anthropic", "openai", "openrouter"}
