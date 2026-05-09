"""Tests for minion/compact/ — compaction strategies and registry.

All LLM calls mocked. No async needed — both strategies are synchronous.
"""

from unittest.mock import MagicMock

import pytest

from minion.compact import DEFAULT_STRATEGY, STRATEGIES, get_strategy
from minion.compact.base import CompactionResult
from minion.compact.summary import SummaryStrategy, _conversation_to_text
from minion.compact.truncate import TruncateStrategy, _estimate_tokens
from minion.llm.conversation import Conversation
from minion.llm.base import (
    ContentTextBlock,
    ContentToolResultBlock,
    ContentToolUseBlock,
    LLMResponse,
    Message,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_conversation(*messages: tuple[str, str]) -> Conversation:
    """Build a Conversation from (role, content) pairs."""
    conv = Conversation()
    for role, content in messages:
        conv.messages.append(Message(role=role, content=content))
    return conv


def _make_client(summary_text: str = "Summary of conversation.") -> MagicMock:
    client = MagicMock()
    client.complete.return_value = LLMResponse(
        content=summary_text,
        input_tokens=10,
        output_tokens=20,
        model="test",
    )
    return client


def _alternating(n_turns: int) -> Conversation:
    """Build a conversation with n_turns user+assistant message pairs."""
    conv = Conversation()
    for i in range(n_turns):
        conv.messages.append(Message(role="user", content=f"Question {i}"))
        conv.messages.append(Message(role="assistant", content=f"Answer {i}"))
    return conv


# ─── TestCompactRegistry ──────────────────────────────────────────────────────

class TestCompactRegistry:

    def test_get_strategy_returns_summary_instance(self):
        s = get_strategy("summary")
        assert isinstance(s, SummaryStrategy)

    def test_get_strategy_returns_truncate_instance(self):
        s = get_strategy("truncate")
        assert isinstance(s, TruncateStrategy)

    def test_get_strategy_raises_for_unknown(self):
        with pytest.raises(ValueError, match="Unknown compaction strategy"):
            get_strategy("bogus")

    def test_default_strategy_is_summary(self):
        assert DEFAULT_STRATEGY == "summary"

    def test_strategies_registry_has_both(self):
        assert "summary" in STRATEGIES
        assert "truncate" in STRATEGIES


# ─── TestTruncateStrategy ─────────────────────────────────────────────────────

class TestTruncateStrategy:

    def test_keeps_last_n_turns(self):
        conv = _alternating(10)  # 20 messages
        assert len(conv.messages) == 20

        strategy = TruncateStrategy(keep_turns=5)
        result = strategy.compact(conv, MagicMock(), "sys")

        assert result.messages_before == 20
        assert len(conv.messages) == 10  # keep_turns * 2

    def test_short_conversation_unchanged(self):
        conv = _alternating(3)  # 6 messages — below keep_turns*2=10
        strategy = TruncateStrategy(keep_turns=5)
        result = strategy.compact(conv, MagicMock(), "sys")

        assert len(conv.messages) == 6
        assert result.messages_before == result.messages_after == 6

    def test_returns_compaction_result(self):
        conv = _alternating(10)
        strategy = TruncateStrategy(keep_turns=3)
        result = strategy.compact(conv, MagicMock(), "sys")

        assert isinstance(result, CompactionResult)
        assert result.messages_before > result.messages_after

    def test_custom_keep_turns(self):
        conv = _alternating(10)  # 20 messages
        strategy = TruncateStrategy(keep_turns=2)
        strategy.compact(conv, MagicMock(), "sys")

        assert len(conv.messages) == 4  # keep_turns * 2

    def test_drops_orphaned_tool_result_at_front(self):
        conv = Conversation()
        # Tool result at index 0 after truncation would be orphaned
        for i in range(8):
            conv.messages.append(Message(role="user", content=f"Q{i}"))
            conv.messages.append(Message(role="assistant", content=f"A{i}"))
        # Insert a tool result so after truncation it appears at the start
        conv.messages.insert(10, Message(role="user", content=[
            ContentToolResultBlock(tool_use_id="t1", content="result")
        ]))

        strategy = TruncateStrategy(keep_turns=3)
        strategy.compact(conv, MagicMock(), "sys")

        # First message must NOT be a tool_result
        first = conv.messages[0]
        if isinstance(first.content, list):
            assert not any(isinstance(b, ContentToolResultBlock) for b in first.content)

    def test_estimate_tokens_positive_for_nonempty(self):
        conv = _make_conversation(("user", "hello world"), ("assistant", "hi there"))
        tokens = _estimate_tokens(conv)
        assert tokens > 0

    def test_estimate_tokens_zero_for_empty(self):
        conv = Conversation()
        assert _estimate_tokens(conv) == 0


# ─── TestSummaryStrategy ──────────────────────────────────────────────────────

class TestSummaryStrategy:

    def test_compact_replaces_with_exactly_two_messages(self):
        conv = _alternating(5)  # 10 messages
        client = _make_client("The user asked questions and got answers.")

        strategy = SummaryStrategy()
        strategy.compact(conv, client, "sys")

        assert len(conv.messages) == 2

    def test_compact_calls_client_complete_once(self):
        conv = _alternating(5)
        client = _make_client()

        strategy = SummaryStrategy()
        strategy.compact(conv, client, "sys")

        assert client.complete.call_count == 1

    def test_stub_starts_with_compacted_marker(self):
        conv = _alternating(5)
        client = _make_client("summary content")

        strategy = SummaryStrategy()
        strategy.compact(conv, client, "sys")

        first_content = conv.messages[0].content
        assert isinstance(first_content, str)
        assert first_content.startswith("[Compacted")

    def test_second_message_is_assistant_ack(self):
        conv = _alternating(3)
        client = _make_client()

        strategy = SummaryStrategy()
        strategy.compact(conv, client, "sys")

        assert conv.messages[1].role == "assistant"
        assert "Understood" in conv.messages[1].content

    def test_returns_compaction_result_with_correct_counts(self):
        conv = _alternating(5)  # 10 messages
        client = _make_client()

        strategy = SummaryStrategy()
        result = strategy.compact(conv, client, "sys")

        assert isinstance(result, CompactionResult)
        assert result.messages_before == 10
        assert result.messages_after == 2

    def test_conversation_to_text_includes_role_labels(self):
        messages = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi there"),
        ]
        text = _conversation_to_text(messages)

        assert "USER:" in text
        assert "ASSISTANT:" in text
        assert "Hello" in text
        assert "Hi there" in text

    def test_conversation_to_text_handles_content_blocks(self):
        messages = [
            Message(role="user", content=[ContentTextBlock(text="Block text")]),
            Message(role="assistant", content=[
                ContentToolUseBlock(id="t1", name="read_file", input={"path": "foo.py"})
            ]),
        ]
        text = _conversation_to_text(messages)

        assert "Block text" in text
        assert "read_file" in text

    def test_summary_text_appears_in_stub(self):
        conv = _alternating(3)
        client = _make_client("The user is building a REST API with FastAPI.")

        strategy = SummaryStrategy()
        strategy.compact(conv, client, "sys")

        stub = conv.messages[0].content
        assert "FastAPI" in stub
