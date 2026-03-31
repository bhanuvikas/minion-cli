"""Conversation history management.

Single responsibility: own the messages[] list, track cumulative token usage,
and apply sliding-window truncation when the context window fills up.

The LLM is stateless — we simulate memory by replaying the full message history
on every API call. This class manages that list.
"""

import math
from typing import Optional

from .llm.base import LLMResponse, Message

# Context window limits by model name fragment (matched with `in`).
# These are the INPUT limits; we target 85% to leave headroom for new messages.
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku":  200_000,
    "claude-3-haiku":    200_000,
    "claude-sonnet-4":   200_000,
    "claude-opus-4":     200_000,
    "gpt-4o":            128_000,
    "gpt-4o-mini":       128_000,
    "gpt-4-turbo":       128_000,
}
DEFAULT_LIMIT      = 16_000
TRUNCATION_THRESHOLD = 0.85


def _context_limit(model: str) -> int:
    """Return context window size for the given model ID."""
    model_lower = model.lower()
    for fragment, limit in MODEL_CONTEXT_LIMITS.items():
        if fragment in model_lower:
            return limit
    return DEFAULT_LIMIT


class Conversation:
    """Maintains message history and tracks cumulative token usage for a session."""

    def __init__(self, model: str = "") -> None:
        self.messages: list[Message] = []
        self.total_tokens: int = 0   # cumulative (input + output) across all turns
        self._model = model

    def set_model(self, model: str) -> None:
        """Update the model used for context-limit lookups (e.g. after /model change)."""
        self._model = model

    def add_user(self, text: str) -> None:
        self.messages.append(Message(role="user", content=text))

    def add_assistant(self, text: str, usage: Optional[LLMResponse]) -> None:
        """Append assistant reply and update running token totals."""
        self.messages.append(Message(role="assistant", content=text))
        if usage:
            self.total_tokens += usage.input_tokens + usage.output_tokens
            # Update model from usage in case it wasn't set at construction time
            if usage.model:
                self._model = usage.model

    def clear(self) -> None:
        self.messages.clear()
        self.total_tokens = 0

    def truncate_if_needed(self, last_input_tokens: int, last_output_tokens: int) -> int:
        """Slide the window if the estimated next-call input exceeds the threshold.

        Returns the number of message pairs dropped (0 if no truncation needed).

        Why input + output:
          last_input_tokens  = tokens sent on the last call (system + messages up to user_N)
          last_output_tokens = tokens in the response (assistant_N, now in messages[])
          The NEXT call will include both, so their sum is the minimum context size estimate.
        """
        limit = _context_limit(self._model)
        target = limit * TRUNCATION_THRESHOLD

        estimated_next_input = last_input_tokens + last_output_tokens
        if estimated_next_input <= target or len(self.messages) <= 2:
            return 0

        # Estimate pairs to drop using average tokens-per-message as a heuristic.
        # No per-message token storage needed — if the average is slightly off, the
        # next API response will give corrected counts and we re-evaluate then.
        overage = estimated_next_input - target
        avg_tokens_per_message = estimated_next_input / len(self.messages)
        pairs_to_drop = math.ceil(overage / (avg_tokens_per_message * 2))
        pairs_to_drop = min(pairs_to_drop, len(self.messages) // 2)

        for _ in range(pairs_to_drop):
            self.messages.pop(0)   # oldest user message
            self.messages.pop(0)   # oldest assistant reply

        return pairs_to_drop
