"""Truncation compaction: keep the last N turns, drop everything older."""

from .base import CompactionResult, CompactionStrategy
from ..llm.conversation import Conversation
from ..llm.base import ContentBlock, LLMClient


def _estimate_tokens(conversation: Conversation) -> int:
    total = 0
    for m in conversation.messages:
        if isinstance(m.content, str):
            total += len(m.content) // 4
        elif isinstance(m.content, list):
            for block in m.content:
                if hasattr(block, "text"):
                    total += len(block.text) // 4
                elif hasattr(block, "content"):
                    total += len(str(block.content)) // 4
                elif hasattr(block, "input"):
                    total += len(str(block.input)) // 4
    return total


class TruncateStrategy(CompactionStrategy):
    name = "truncate"
    description = "Drop oldest messages, keep last N turns (fast, no LLM call)"

    def __init__(self, keep_turns: int = 5) -> None:
        self.keep_turns = keep_turns

    def compact(
        self,
        conversation: Conversation,
        client: LLMClient,
        system_prompt: str,
    ) -> CompactionResult:
        before = len(conversation.messages)
        tokens_before = _estimate_tokens(conversation)

        # Keep last keep_turns * 2 messages (each turn = user + assistant).
        # If we're in the middle of a tool sequence, we may keep a few more
        # to avoid orphaned tool_result messages at the start.
        keep = self.keep_turns * 2
        if len(conversation.messages) > keep:
            conversation.messages = conversation.messages[-keep:]
            # Drop orphaned tool_result messages that now appear at the front.
            from ..llm.conversation import _is_tool_result_message
            while conversation.messages and _is_tool_result_message(conversation.messages[0]):
                conversation.messages.pop(0)
            conversation._snapshot = None

        tokens_after = _estimate_tokens(conversation)
        return CompactionResult(
            messages_before=before,
            messages_after=len(conversation.messages),
            tokens_estimate_before=tokens_before,
            tokens_estimate_after=tokens_after,
        )
