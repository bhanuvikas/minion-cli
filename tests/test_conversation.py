"""Tests for minion/conversation.py — message history, token tracking, truncation.

No network calls, no API keys needed.
"""

import pytest
from minion.llm.conversation import Conversation, ContextSnapshot, _context_limit, DEFAULT_LIMIT
from minion.llm.base import ContentTextBlock, ContentToolResultBlock, ContentToolUseBlock, LLMResponse, Message


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


# ─── add_assistant_blocks / add_tool_result ───────────────────────────────────

class TestContentBlockMessages:
    def test_add_assistant_blocks_stores_typed_objects(self):
        """Conversation stores application ContentBlock objects, not provider dicts."""
        c = Conversation()
        blocks = [
            ContentTextBlock(text="I'll read that."),
            ContentToolUseBlock(id="toolu_01", name="read_file", input={"path": "x.py"}),
        ]
        c.add_assistant_blocks(blocks, None)
        msg = c.messages[-1]
        assert msg.role == "assistant"
        assert isinstance(msg.content, list)
        assert isinstance(msg.content[0], ContentTextBlock)
        assert isinstance(msg.content[1], ContentToolUseBlock)

    def test_add_assistant_blocks_updates_token_count(self):
        c = Conversation()
        c.add_assistant_blocks([], _usage(input_tokens=100, output_tokens=50))
        assert c.total_tokens == 150

    def test_add_tool_result_stores_content_tool_result_block(self):
        c = Conversation()
        c.add_tool_result("toolu_01", "file contents")
        msg = c.messages[-1]
        assert msg.role == "user"
        assert isinstance(msg.content, list)
        block = msg.content[0]
        assert isinstance(block, ContentToolResultBlock)
        assert block.tool_use_id == "toolu_01"
        assert block.content == "file contents"

    def test_tool_use_round_trip_in_messages(self):
        """A full tool-use turn: assistant blocks then tool result — roles and types correct."""
        c = Conversation()
        c.add_user("list the files")
        c.add_assistant_blocks(
            [ContentToolUseBlock(id="toolu_01", name="list_directory", input={"path": "."})],
            _usage(),
        )
        c.add_tool_result("toolu_01", "README.md\nmain.py")
        roles = [m.role for m in c.messages]
        assert roles == ["user", "assistant", "user"]
        # The last user message carries a tool result, not plain text
        assert isinstance(c.messages[-1].content[0], ContentToolResultBlock)


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

    def test_truncation_drops_orphaned_tool_results(self):
        """Truncation after a tool-use turn must not leave orphaned tool_result messages."""
        c = Conversation(model="unknown-model")
        # Simulate a tool-use turn:
        #   [0] user: regular message
        #   [1] assistant: tool_use block
        #   [2] user: tool_result block  ← would be orphaned if [0]+[1] dropped
        #   [3] assistant: final response
        #   [4] user: another message
        #   [5] assistant: another response
        c.add_user("user question 1")
        c.add_assistant_blocks(
            [ContentToolUseBlock(id="t1", name="read_file", input={"path": "main.py"})],
            usage=None,
        )
        c.add_tool_result("t1", "file content here")
        c.add_assistant("Got the file.", usage=None)
        c.add_user("user question 2")
        c.add_assistant("Second answer.", usage=None)

        # Force truncation (overage large enough to drop at least one pair)
        c.truncate_if_needed(last_input_tokens=12_000, last_output_tokens=3_000)

        # After truncation, the first message must NEVER be a tool_result
        from minion.llm.conversation import _is_tool_result_message
        assert not _is_tool_result_message(c.messages[0]), (
            "First message after truncation is an orphaned tool_result — "
            "this would cause a 400 API error"
        )

    def test_tool_result_detection(self):
        from minion.llm.conversation import _is_tool_result_message
        regular = Message(role="user", content="hello")
        tool_result = Message(role="user", content=[
            ContentToolResultBlock(tool_use_id="t1", content="result")
        ])
        assistant = Message(role="assistant", content="response")
        assert not _is_tool_result_message(regular)
        assert _is_tool_result_message(tool_result)
        assert not _is_tool_result_message(assistant)


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
