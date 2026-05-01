"""Base types for conversation compaction strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..conversation import Conversation
    from ..llm.base import LLMClient


@dataclass
class CompactionResult:
    messages_before: int
    messages_after: int
    tokens_estimate_before: int   # rough estimate (chars // 4)
    tokens_estimate_after: int


class CompactionStrategy(ABC):
    name: str
    description: str

    @abstractmethod
    def compact(
        self,
        conversation: "Conversation",
        client: "LLMClient",
        system_prompt: str,
    ) -> CompactionResult:
        """Compact the conversation in-place. Returns before/after stats."""
