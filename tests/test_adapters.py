"""Tests for LLM adapter message-formatting logic.

AnthropicClient and OpenAIClient both transform our Message dataclasses
into the provider-specific dict format before calling the API. These
transformations are pure functions — no network, no API keys needed.

We patch the provider SDKs at construction time so the clients can be
instantiated without real credentials.
"""

import pytest
from unittest.mock import patch, MagicMock

from minion.llm.base import Message


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def anthropic_client():
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
         patch("minion.llm.anthropic.anthropic.Anthropic"):
        from minion.llm.anthropic import AnthropicClient
        return AnthropicClient()


@pytest.fixture
def openai_client():
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}), \
         patch("minion.llm.openai.OpenAI"):
        from minion.llm.openai import OpenAIClient
        return OpenAIClient()


# ─── AnthropicClient._format_messages ────────────────────────────────────────

class TestAnthropicFormatMessages:
    def test_single_user_message(self, anthropic_client):
        msgs = [Message(role="user", content="hello")]
        result = anthropic_client._format_messages(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_preserves_role(self, anthropic_client):
        msgs = [Message(role="assistant", content="hi there")]
        result = anthropic_client._format_messages(msgs)
        assert result[0]["role"] == "assistant"

    def test_preserves_message_order(self, anthropic_client):
        msgs = [
            Message(role="user", content="first"),
            Message(role="assistant", content="second"),
            Message(role="user", content="third"),
        ]
        result = anthropic_client._format_messages(msgs)
        assert [m["content"] for m in result] == ["first", "second", "third"]

    def test_empty_messages(self, anthropic_client):
        assert anthropic_client._format_messages([]) == []


# ─── OpenAIClient._build_messages ────────────────────────────────────────────

class TestOpenAIBuildMessages:
    def test_prepends_system_message_when_given(self, openai_client):
        msgs = [Message(role="user", content="hello")]
        result = openai_client._build_messages(msgs, system="You are helpful")
        assert result[0] == {"role": "system", "content": "You are helpful"}
        assert result[1] == {"role": "user", "content": "hello"}

    def test_no_system_message_when_empty_string(self, openai_client):
        msgs = [Message(role="user", content="hello")]
        result = openai_client._build_messages(msgs, system="")
        assert result[0]["role"] == "user"
        assert len(result) == 1

    def test_preserves_message_order_with_system(self, openai_client):
        msgs = [
            Message(role="user", content="first"),
            Message(role="assistant", content="reply"),
            Message(role="user", content="second"),
        ]
        result = openai_client._build_messages(msgs, system="sys")
        # system is index 0, then messages in order
        assert result[0]["role"] == "system"
        assert [m["content"] for m in result[1:]] == ["first", "reply", "second"]

    def test_preserves_message_order_without_system(self, openai_client):
        msgs = [
            Message(role="user", content="a"),
            Message(role="assistant", content="b"),
        ]
        result = openai_client._build_messages(msgs, system="")
        assert [m["content"] for m in result] == ["a", "b"]

    def test_total_length_with_system(self, openai_client):
        msgs = [Message(role="user", content="x")] * 3
        result = openai_client._build_messages(msgs, system="sys")
        assert len(result) == 4  # 1 system + 3 user
