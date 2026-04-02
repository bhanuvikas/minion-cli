"""MemoryConfig — user-controllable settings for the memory subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .triggers import ExtractionTrigger


@dataclass
class MemoryConfig:
    """Settings for the memory subsystem.

    enabled=False disables both retrieval (injection) and extraction
    for the session — equivalent to private mode.

    top_k: maximum number of memories injected per turn.
    similarity_threshold: minimum cosine score (0–1) to include a memory in results.
    consolidation_threshold: cosine score above which two memories are considered
        conflicting and are sent to the LLM for resolution.
    global_memory_dir: root of the global memory store (~/.minion/memory/).
    trigger: strategy controlling when auto-extraction runs after a turn.
    """

    enabled: bool = True
    top_k: int = 5
    similarity_threshold: float = 0.70
    consolidation_threshold: float = 0.92
    global_memory_dir: Path = field(
        default_factory=lambda: Path.home() / ".minion" / "memory"
    )
    # ExtractionTrigger instance — default set in __post_init__ to avoid
    # importing triggers.py at class definition time.
    trigger: Any = field(default=None)

    def __post_init__(self) -> None:
        if self.trigger is None:
            from .triggers import SubstantialContentTrigger
            self.trigger = SubstantialContentTrigger()
