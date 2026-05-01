"""Conversation compaction strategies.

Usage:
    from minion.compact import get_strategy, compact_conversation

    strategy = get_strategy("summary")   # or "truncate"
    result = compact_conversation(strategy, conversation, client, system_prompt)
"""

from .base import CompactionResult, CompactionStrategy
from .summary import SummaryStrategy
from .truncate import TruncateStrategy

# Registry: name → class (not instance — some strategies take constructor args)
STRATEGIES: dict[str, type[CompactionStrategy]] = {
    SummaryStrategy.name:  SummaryStrategy,
    TruncateStrategy.name: TruncateStrategy,
}

DEFAULT_STRATEGY = SummaryStrategy.name


def get_strategy(name: str, **kwargs) -> CompactionStrategy:
    """Return a strategy instance by name. Raises ValueError for unknown names."""
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(f"Unknown compaction strategy '{name}'. Available: {list(STRATEGIES)}")
    return cls(**kwargs)


__all__ = [
    "CompactionResult",
    "CompactionStrategy",
    "SummaryStrategy",
    "TruncateStrategy",
    "STRATEGIES",
    "DEFAULT_STRATEGY",
    "get_strategy",
]
