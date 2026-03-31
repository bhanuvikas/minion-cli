"""Tests for minion/conversation.py — message history, token tracking, truncation.

No network calls, no API keys needed.
"""

import pytest
from minion.conversation import Conversation, _context_limit, DEFAULT_LIMIT
from minion.llm.base import LLMResponse, Message


def _usage(input_tokens=100, output_tokens=50, model="claude-3-5-sonnet"):
    return LLMResponse(content="", input_tokens=input_tokens,
                       output_tokens=output_tokens, model=model)


# ─── add_user / add_assistant ─────────────────────────────────────────────────

class TestConversationMessages:
    def test_add_user_appends_message(self):
        c = Conversation()
        c.add_user("hello")
        assert c.messages == [Message(role="user", content="hello")]

    def test_add_assistant_appends_message(self):
        c = Conversation()
        c.add_user("hi")
        c.add_assistant("hey there", _usage())
        assert c.messages[-1] == Message(role="assistant", content="hey there")

    def test_messages_alternate_roles(self):
        c = Conversation()
        c.add_user("q1")
        c.add_assistant("a1", _usage())
        c.add_user("q2")
        c.add_assistant("a2", _usage())
        roles = [m.role for m in c.messages]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_add_assistant_with_none_usage_does_not_crash(self):
        c = Conversation()
        c.add_user("hi")
        c.add_assistant("hey", None)
        assert len(c.messages) == 2


# ─── Token tracking ──────────────────────────────────────────────────────────

class TestTokenTracking:
    def test_total_tokens_zero_initially(self):
        assert Conversation().total_tokens == 0

    def test_total_tokens_accumulates(self):
        c = Conversation()
        c.add_user("q")
        c.add_assistant("a", _usage(input_tokens=100, output_tokens=50))
        assert c.total_tokens == 150

    def test_total_tokens_sums_across_turns(self):
        c = Conversation()
        c.add_user("q1")
        c.add_assistant("a1", _usage(input_tokens=100, output_tokens=50))
        c.add_user("q2")
        c.add_assistant("a2", _usage(input_tokens=200, output_tokens=80))
        assert c.total_tokens == 430

    def test_none_usage_does_not_add_tokens(self):
        c = Conversation()
        c.add_user("q")
        c.add_assistant("a", None)
        assert c.total_tokens == 0

    def test_model_updated_from_usage(self):
        c = Conversation()
        c.add_user("q")
        c.add_assistant("a", _usage(model="claude-3-5-sonnet-20241022"))
        assert c._model == "claude-3-5-sonnet-20241022"


# ─── clear ────────────────────────────────────────────────────────────────────

class TestClear:
    def test_clear_empties_messages(self):
        c = Conversation()
        c.add_user("hi")
        c.add_assistant("hey", _usage())
        c.clear()
        assert c.messages == []

    def test_clear_resets_token_count(self):
        c = Conversation()
        c.add_user("hi")
        c.add_assistant("hey", _usage(input_tokens=100, output_tokens=50))
        c.clear()
        assert c.total_tokens == 0


# ─── truncate_if_needed ───────────────────────────────────────────────────────

class TestTruncation:
    def _conversation_with_pairs(self, n: int) -> Conversation:
        """Build a Conversation with n user+assistant pairs."""
        c = Conversation(model="gpt-4o-mini")  # 128k limit → threshold = 108,800
        for i in range(n):
            c.add_user(f"question {i}")
            c.add_assistant(f"answer {i}", None)
        return c

    def test_no_truncation_when_under_threshold(self):
        c = self._conversation_with_pairs(4)
        dropped = c.truncate_if_needed(last_input_tokens=1_000, last_output_tokens=200)
        assert dropped == 0
        assert len(c.messages) == 8

    def test_truncation_drops_oldest_pair(self):
        c = self._conversation_with_pairs(4)
        # Simulate being well over the threshold for gpt-4o-mini (128k * 0.85 = 108,800)
        dropped = c.truncate_if_needed(last_input_tokens=100_000, last_output_tokens=20_000)
        assert dropped >= 1
        # First message should no longer be "question 0"
        assert c.messages[0].content != "question 0"

    def test_truncation_preserves_alternating_roles(self):
        c = self._conversation_with_pairs(6)
        c.truncate_if_needed(last_input_tokens=100_000, last_output_tokens=20_000)
        roles = [m.role for m in c.messages]
        for i in range(0, len(roles) - 1, 2):
            assert roles[i] == "user"
            assert roles[i + 1] == "assistant"

    def test_truncation_never_drops_below_two_messages(self):
        c = self._conversation_with_pairs(1)  # just 2 messages
        # Extreme overage — should not drop the last pair
        dropped = c.truncate_if_needed(last_input_tokens=500_000, last_output_tokens=100_000)
        assert dropped == 0
        assert len(c.messages) == 2

    def test_no_truncation_when_no_messages(self):
        c = Conversation(model="gpt-4o-mini")
        dropped = c.truncate_if_needed(last_input_tokens=200_000, last_output_tokens=50_000)
        assert dropped == 0

    def test_uses_input_plus_output_for_threshold_check(self):
        """input alone under threshold but input+output over → should truncate."""
        c = self._conversation_with_pairs(4)
        # gpt-4o-mini threshold = 128_000 * 0.85 = 108_800
        # input alone = 90,000 (under) but input + output = 110,000 (over)
        dropped = c.truncate_if_needed(last_input_tokens=90_000, last_output_tokens=20_000)
        assert dropped >= 1


# ─── _context_limit ──────────────────────────────────────────────────────────

class TestContextLimit:
    def test_known_model_returns_correct_limit(self):
        assert _context_limit("claude-3-5-sonnet-20241022") == 200_000

    def test_unknown_model_returns_default(self):
        assert _context_limit("some-future-model-xyz") == DEFAULT_LIMIT

    def test_case_insensitive_matching(self):
        assert _context_limit("GPT-4O") == 128_000
