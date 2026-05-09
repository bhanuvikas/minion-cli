"""Conversation history management.

Single responsibility: own the messages[] list, track cumulative token usage,
and apply sliding-window truncation when the context window fills up.

The LLM is stateless — we simulate memory by replaying the full message history
on every API call. This class manages that list.
"""

import math
from dataclasses import dataclass, field
from typing import Optional

from .llm.base import ContentBlock, ContentToolResultBlock, ContentToolUseBlock, LLMResponse, Message


def _is_tool_result_message(msg: Message) -> bool:
    """True if this message is an orphaned tool_result (user role, tool_result content).

    Used by truncate_if_needed() to avoid leaving tool_result messages at the
    front of history after their corresponding tool_use block has been dropped.
    """
    if msg.role != "user":
        return False
    if isinstance(msg.content, list) and msg.content:
        return isinstance(msg.content[0], ContentToolResultBlock)
    return False

# Context window limits by model name fragment (matched with `in`).
# These are the INPUT limits; we target 85% to leave headroom for new messages.
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku":  200_000,
    "claude-3-haiku":    200_000,
    "claude-sonnet-4":   200_000,
    "claude-haiku-4":    200_000,
    "claude-opus-4":     200_000,
    "gpt-4o":            128_000,
    "gpt-4o-mini":       128_000,
    "gpt-4-turbo":       128_000,
}
DEFAULT_LIMIT        = 16_000
TRUNCATION_THRESHOLD = 0.85


def _context_limit(model: str) -> int:
    """Return context window size for the given model ID."""
    model_lower = model.lower()
    for fragment, limit in MODEL_CONTEXT_LIMITS.items():
        if fragment in model_lower:
            return limit
    return DEFAULT_LIMIT


@dataclass
class ContextSnapshot:
    """Everything needed to display context usage — built after each LLM response.

    This is the single data structure consumed by print_usage() (footer line)
    and print_context() (/context command). Adding new context components in
    future phases (tool_tokens, memory_tokens, etc.) means adding fields here
    and updating the display functions — nothing else changes.

    Accuracy:
      - input_tokens, output_tokens, context_limit, session_total: exact
      - system_prompt_tokens: estimated (chars // 4); labeled ~ in display
      - message_tokens: derived (input_tokens - system_prompt_tokens)
    """
    model: str
    input_tokens: int           # total input this turn (uncached + cache_read + cache_creation)
    output_tokens: int          # generated this turn
    context_limit: int          # model's context window size
    session_total: int          # cumulative (input + output) across all turns
    turn_count: int             # completed turns this session
    system_prompt_tokens: int = 0   # estimated from char count; 0 if unavailable
    memory_tokens: int = 0          # estimated from injected memory block size
    cache_read_tokens: int = 0      # tokens served from prompt cache this turn
    cache_creation_tokens: int = 0  # tokens written to prompt cache this turn

    @property
    def current_context_tokens(self) -> int:
        """Tokens currently occupying the context window.

        = input_tokens (sent this turn) + output_tokens (reply, now in messages[])
        This is what will be sent on the NEXT call before the new user message.
        Consistent with truncate_if_needed which uses the same sum.
        """
        return self.input_tokens + self.output_tokens

    @property
    def message_tokens(self) -> int:
        return max(0, self.current_context_tokens - self.system_prompt_tokens)

    @property
    def context_pct(self) -> float:
        if self.context_limit == 0:
            return 0.0
        return self.current_context_tokens / self.context_limit * 100


class Conversation:
    """Maintains message history and tracks cumulative token usage for a session."""

    def __init__(self, model: str = "") -> None:
        self.messages: list[Message] = []
        # total_tokens survives clear() — it's billing history, not window state.
        self.total_tokens: int = 0      # cumulative (input + output) across all turns
        self._model = model
        self._turn_count: int = 0
        self._snapshot: Optional[ContextSnapshot] = None

    def set_model(self, model: str) -> None:
        """Update the model used for context-limit lookups (e.g. after /model change)."""
        self._model = model

    def add_user(self, text: str) -> None:
        # Plain string content — no tool involvement, so no typed ContentBlocks needed.
        self.messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str, usage: Optional[LLMResponse]) -> None:
        """Append a plain-text assistant reply and update running token totals."""
        self.messages.append(Message(role="assistant", content=text))
        if usage:
            self.total_tokens += (usage.input_tokens + usage.cache_read_tokens
                                  + usage.cache_creation_tokens + usage.output_tokens)
            self._turn_count += 1
            if usage.model:
                self._model = usage.model

    def add_assistant_blocks(self, content_blocks: list[ContentBlock], usage: Optional[LLMResponse]) -> None:
        """Append an assistant turn that contains tool-use content blocks.

        Stores typed ContentBlock objects — provider wire format is the adapter's
        concern, not the conversation's. Subsequent calls reference tool IDs via
        ContentToolUseBlock.id when matching tool results.
        """
        self.messages.append(Message(role="assistant", content=content_blocks))
        if usage:
            self.total_tokens += (usage.input_tokens + usage.cache_read_tokens
                                  + usage.cache_creation_tokens + usage.output_tokens)
            self._turn_count += 1
            if usage.model:
                self._model = usage.model

    def add_tool_result(self, tool_use_id: str, result: str) -> None:
        """Inject a tool result as a user-role message.

        Tool results are user-role messages per the LLM conversation protocol —
        there is no separate tool role. ContentToolResultBlock carries the ID
        that links this result back to its ContentToolUseBlock.
        """
        self.messages.append(Message(
            role="user",
            content=[ContentToolResultBlock(tool_use_id=tool_use_id, content=result)],
        ))

    def build_snapshot(
        self,
        usage: Optional[LLMResponse],
        system_prompt_tokens: int = 0,
        memory_tokens: int = 0,
    ) -> Optional["ContextSnapshot"]:
        """Build and store a ContextSnapshot from the latest response.

        system_prompt_tokens: estimated token count for the base system prompt.
        memory_tokens: estimated token count for the injected memory block.
        Both computed by the caller (runner.py) as len(text) // 4.
        """
        # Snapshot is built once per turn, after add_assistant*() has already updated total_tokens.
        if usage is None:
            return None
        total_input = (usage.input_tokens + usage.cache_read_tokens
                       + usage.cache_creation_tokens)
        self._snapshot = ContextSnapshot(
            model=usage.model,
            input_tokens=total_input,
            output_tokens=usage.output_tokens,
            context_limit=_context_limit(usage.model),
            session_total=self.total_tokens,
            turn_count=self._turn_count,
            system_prompt_tokens=system_prompt_tokens,
            memory_tokens=memory_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
        )
        return self._snapshot

    def context_display(self) -> Optional[ContextSnapshot]:
        """Return snapshot for display.

        If a snapshot exists (post-response), return it.
        If history was cleared (snapshot is None but model is known), return a
        zero-context snapshot so /context can still show model + limit + session total.
        """
        if self._snapshot is not None:
            return self._snapshot
        if self._model:
            return ContextSnapshot(
                model=self._model,
                input_tokens=0,
                output_tokens=0,
                context_limit=_context_limit(self._model),
                session_total=self.total_tokens,
                turn_count=self._turn_count,
            )
        return None

    def clear(self) -> None:
        """Clear message history and snapshot. total_tokens is billing history — never reset."""
        self.messages.clear()
        self._snapshot = None

    def truncate_if_needed(self, last_input_tokens: int, last_output_tokens: int) -> int:
        """Slide the window if the current context size exceeds the threshold.

        Returns the number of message pairs dropped (0 if no truncation needed).

        current_context_size = last_input_tokens + last_output_tokens
          last_input_tokens  = tokens sent on the last call (system + messages up to user_N)
          last_output_tokens = tokens in the response (assistant_N, now added to messages[])
          Their sum is the actual size of the context right now. The next call will send
          all of this plus the new user message, so this is the minimum we know today.
        """
        limit = _context_limit(self._model)
        target = limit * TRUNCATION_THRESHOLD

        current_context_size = last_input_tokens + last_output_tokens
        if current_context_size <= target or len(self.messages) <= 2:
            return 0

        # Estimate pairs to drop using average tokens-per-message as a heuristic.
        # No per-message token storage needed — if the average is slightly off, the
        # next API response will give corrected counts and we re-evaluate then.
        overage = current_context_size - target
        avg_tokens_per_message = current_context_size / len(self.messages)
        pairs_to_drop = math.ceil(overage / (avg_tokens_per_message * 2))
        pairs_to_drop = min(pairs_to_drop, len(self.messages) // 2)

        dropped = 0
        for _ in range(pairs_to_drop):
            if len(self.messages) <= 2:
                break
            self.messages.pop(0)   # oldest user message
            self.messages.pop(0)   # oldest assistant reply (may contain tool_use blocks)
            dropped += 1
            # After dropping an assistant+tool_use message, its corresponding
            # tool_result user messages are now orphaned at the front of history.
            # Drop them too — keeping them would cause a 400 from the API.
            while self.messages and _is_tool_result_message(self.messages[0]):
                self.messages.pop(0)

        return dropped
