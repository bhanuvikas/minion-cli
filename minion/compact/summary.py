"""Summary compaction: LLM summarizes the conversation into a compact stub."""

from .base import CompactionResult, CompactionStrategy
from ..llm.conversation import Conversation
from ..llm.base import (
    ContentTextBlock, ContentToolResultBlock, ContentToolUseBlock,
    LLMClient, Message,
)

_COMPACT_SYSTEM = (
    "You are a conversation summarizer for a coding assistant session. "
    "Produce a context stub that lets the assistant continue accurately without "
    "the full history.\n\n"
    "Preserve specifics over narrative. Include:\n"
    "- The user's goal and current task\n"
    "- Every file created or modified (exact relative paths)\n"
    "- Key architectural and design decisions made, and why\n"
    "- Test / build status (exact counts, passing/failing)\n"
    "- Current state: what's done, what's in progress, what's next\n"
    "- Any errors encountered and how they were resolved\n"
    "- Things the assistant must NOT do (constraints established during the session)\n\n"
    "Format as bullet points grouped by topic. Write as much as needed to preserve "
    "accuracy — do not round down file paths to 'several files', test counts to "
    "'many tests', or decisions to vague summaries. "
    "Omit pleasantries, preamble, and anything re-derivable by reading the files."
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
                if isinstance(block, ContentTextBlock):
                    total += len(block.text) // 4
                elif isinstance(block, ContentToolResultBlock):
                    total += len(str(block.content)) // 4
                elif isinstance(block, ContentToolUseBlock):
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
        # Two messages so the conversation ends on an assistant turn — the next
        # user message stays valid (user → assistant → user alternation required by API).
        conversation.messages = [
            Message(role="user", content=stub),
            Message(role="assistant", content="Understood. I have full context of the prior session."),
        ]
        conversation._snapshot = None

        tokens_after = len(stub) // 4
        return CompactionResult(
            messages_before=before,
            messages_after=len(conversation.messages),
            tokens_estimate_before=tokens_before,
            tokens_estimate_after=tokens_after,
        )
