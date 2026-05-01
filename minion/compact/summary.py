"""Summary compaction: LLM summarizes the conversation into a compact stub."""

from .base import CompactionResult, CompactionStrategy
from ..conversation import Conversation
from ..llm.base import (
    ContentTextBlock, ContentToolResultBlock, ContentToolUseBlock,
    LLMClient, Message,
)

_COMPACT_SYSTEM = (
    "You are a conversation summarizer. Produce a compact context stub that "
    "preserves the essential information from the conversation so that the "
    "assistant can continue helping without the full history.\n\n"
    "Focus on:\n"
    "- What the user is trying to accomplish\n"
    "- What has been done (files created/modified, commands run, decisions made)\n"
    "- Current state and any open issues\n"
    "- Key facts the assistant must remember to continue correctly\n\n"
    "Format as tight bullet points grouped by topic. "
    "Target 150-350 words. Omit pleasantries, tool call details, and "
    "anything a competent assistant could re-derive from the files on disk."
)


def _conversation_to_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m.role.upper()
        if isinstance(m.content, str):
            parts.append(f"{role}: {m.content}")
        elif isinstance(m.content, list):
            for block in m.content:
                if isinstance(block, ContentTextBlock):
                    if block.text.strip():
                        parts.append(f"{role}: {block.text.strip()}")
                elif isinstance(block, ContentToolUseBlock):
                    inp = str(block.input)
                    if len(inp) > 300:
                        inp = inp[:300] + "..."
                    parts.append(f"{role} [tool:{block.name}]: {inp}")
                elif isinstance(block, ContentToolResultBlock):
                    result = str(block.content)
                    if len(result) > 300:
                        result = result[:300] + "..."
                    parts.append(f"TOOL_RESULT: {result}")
    return "\n\n".join(parts)


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


class SummaryStrategy(CompactionStrategy):
    name = "summary"
    description = "LLM summarizes the conversation into a compact context stub (default)"

    def compact(
        self,
        conversation: Conversation,
        client: LLMClient,
        system_prompt: str,
    ) -> CompactionResult:
        before = len(conversation.messages)
        tokens_before = _estimate_tokens(conversation)

        conv_text = _conversation_to_text(conversation.messages)
        prompt = (
            "Summarize the following conversation history into a compact context stub:\n\n"
            f"{conv_text}"
        )
        summary_response = client.complete(
            messages=[Message(role="user", content=prompt)],
            system=_COMPACT_SYSTEM,
        )
        summary = summary_response.content.strip()

        stub = (
            "[Compacted conversation history]\n\n"
            f"{summary}\n\n"
            "---\n"
            "The above is a summary of prior conversation. Continue from this context."
        )
        conversation.messages = [Message(role="user", content=stub)]
        conversation._snapshot = None

        tokens_after = len(stub) // 4
        return CompactionResult(
            messages_before=before,
            messages_after=len(conversation.messages),
            tokens_estimate_before=tokens_before,
            tokens_estimate_after=tokens_after,
        )
