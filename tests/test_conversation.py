"""Tests for minion/conversation.py — message history, token tracking, truncation.

No network calls, no API keys needed.
"""

import pytest
from minion.conversation import Conversation, ContextSnapshot, _context_limit, DEFAULT_LIMIT
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

    def test_clear_preserves_token_count(self):
        """Billing history is never erased — /clear only resets message history."""
        c = Conversation()
        c.add_user("hi")
        c.add_assistant("hey", _usage(input_tokens=100, output_tokens=50))
        c.clear()
        assert c.total_tokens == 150


# ─── truncate_if_needed ───────────────────────────────────────────────────────

class TestTruncation:
    def _conversation_with_pairs(self, n: int) -> Conversation:
        """Build a Conversation with n user+assistant pairs.
        Uses unknown model so DEFAULT_LIMIT (currently 500) applies.
        """
        c = Conversation(model="unknown-model")
        for i in range(n):
            c.add_user(f"question {i}")
            c.add_assistant(f"answer {i}", None)
        return c

    def test_no_truncation_when_under_threshold(self):
        c = self._conversation_with_pairs(4)
        # Well under DEFAULT_LIMIT (500) threshold (425)
        dropped = c.truncate_if_needed(last_input_tokens=100, last_output_tokens=50)
        assert dropped == 0
        assert len(c.messages) == 8

    def test_truncation_drops_oldest_pair(self):
        c = self._conversation_with_pairs(4)
        # DEFAULT_LIMIT=16_000, threshold=13_600 — simulate being over
        dropped = c.truncate_if_needed(last_input_tokens=12_000, last_output_tokens=3_000)
        assert dropped >= 1
        # First message should no longer be "question 0"
        assert c.messages[0].content != "question 0"

    def test_truncation_preserves_alternating_roles(self):
        c = self._conversation_with_pairs(6)
        dropped = c.truncate_if_needed(last_input_tokens=12_000, last_output_tokens=3_000)
        roles = [m.role for m in c.messages]
        for i in range(0, len(roles) - 1, 2):
            assert roles[i] == "user"
            assert roles[i + 1] == "assistant"

    def test_truncation_never_drops_below_two_messages(self):
        c = self._conversation_with_pairs(1)  # just 2 messages
        # Extreme overage — guard (len <= 2) prevents dropping the last pair
        dropped = c.truncate_if_needed(last_input_tokens=10_000, last_output_tokens=10_000)
        assert dropped == 0
        assert len(c.messages) == 2

    def test_no_truncation_when_no_messages(self):
        c = Conversation(model="unknown-model")
        dropped = c.truncate_if_needed(last_input_tokens=10_000, last_output_tokens=10_000)
        assert dropped == 0

    def test_uses_current_context_size_for_threshold_check(self):
        """input alone under threshold but input+output (current_context_size) over → truncate."""
        c = self._conversation_with_pairs(4)
        # DEFAULT_LIMIT=16_000, threshold=13_600
        # input alone = 10_000 (under 13_600), but 10_000 + 5_000 = 15_000 (over 13_600)
        dropped = c.truncate_if_needed(last_input_tokens=10_000, last_output_tokens=5_000)
        assert dropped >= 1


# ─── _context_limit ──────────────────────────────────────────────────────────

class TestContextLimit:
    def test_known_model_returns_correct_limit(self):
        assert _context_limit("claude-3-5-sonnet-20241022") == 200_000

    def test_unknown_model_returns_default(self):
        assert _context_limit("some-future-model-xyz") == DEFAULT_LIMIT

    def test_case_insensitive_matching(self):
        assert _context_limit("GPT-4O") == 128_000


# ─── ContextSnapshot ─────────────────────────────────────────────────────────

class TestContextSnapshot:
    def test_build_snapshot_returns_none_without_usage(self):
        c = Conversation(model="claude-3-5-sonnet")
        c.add_user("hi")
        c.add_assistant("hey", None)
        assert c.build_snapshot(None) is None

    def test_build_snapshot_populates_fields(self):
        c = Conversation()
        c.add_user("hi")
        c.add_assistant("hey", _usage(input_tokens=500, output_tokens=100, model="claude-3-5-sonnet"))
        snap = c.build_snapshot(_usage(input_tokens=500, output_tokens=100, model="claude-3-5-sonnet"), system_prompt_tokens=50)
        assert snap.model == "claude-3-5-sonnet"
        assert snap.input_tokens == 500
        assert snap.output_tokens == 100
        assert snap.context_limit == 200_000
        assert snap.session_total == 600
        assert snap.turn_count == 1
        assert snap.system_prompt_tokens == 50

    def test_snapshot_message_tokens_derived(self):
        snap = ContextSnapshot(
            model="m", input_tokens=500, output_tokens=100,
            context_limit=200_000, session_total=600, turn_count=1,
            system_prompt_tokens=50,
        )
        # message_tokens = current_context_tokens - system_prompt_tokens
        #                = (500 + 100) - 50 = 550
        assert snap.message_tokens == 550

    def test_snapshot_current_context_tokens(self):
        snap = ContextSnapshot(
            model="m", input_tokens=190, output_tokens=221,
            context_limit=16_000, session_total=411, turn_count=1,
        )
        assert snap.current_context_tokens == 411

    def test_snapshot_context_pct(self):
        snap = ContextSnapshot(
            model="m", input_tokens=900, output_tokens=100,
            context_limit=100_000, session_total=1_000, turn_count=1,
        )
        assert snap.context_pct == pytest.approx(1.0)  # 1000/100000 = 1%

    def test_snapshot_context_pct_zero_when_no_limit(self):
        snap = ContextSnapshot(
            model="m", input_tokens=100, output_tokens=10,
            context_limit=0, session_total=110, turn_count=1,
        )
        assert snap.context_pct == 0.0

    def test_snapshot_stored_on_conversation(self):
        c = Conversation()
        c.add_user("q")
        usage = _usage(input_tokens=100, output_tokens=50, model="gpt-4o")
        c.add_assistant("a", usage)
        c.build_snapshot(usage)
        assert c._snapshot is not None
        assert c._snapshot.model == "gpt-4o"

    def test_clear_resets_snapshot(self):
        c = Conversation()
        c.add_user("q")
        usage = _usage(input_tokens=100, output_tokens=50, model="gpt-4o")
        c.add_assistant("a", usage)
        c.build_snapshot(usage)
        c.clear()
        assert c._snapshot is None

    def test_context_display_returns_snapshot_when_available(self):
        c = Conversation()
        c.add_user("q")
        usage = _usage(input_tokens=100, output_tokens=50, model="gpt-4o")
        c.add_assistant("a", usage)
        snap = c.build_snapshot(usage)
        assert c.context_display() is snap

    def test_context_display_returns_zero_snapshot_after_clear(self):
        c = Conversation()
        c.add_user("q")
        usage = _usage(input_tokens=100, output_tokens=50, model="gpt-4o")
        c.add_assistant("a", usage)
        c.build_snapshot(usage)
        c.clear()
        display = c.context_display()
        assert display is not None
        assert display.input_tokens == 0
        assert display.model == "gpt-4o"
        assert display.session_total == 150  # billing history preserved

    def test_context_display_returns_none_when_no_model(self):
        c = Conversation()   # no model set, no turns
        assert c.context_display() is None
