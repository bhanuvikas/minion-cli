"""ExtractionTrigger — strategy pattern for controlling when memory extraction runs.

Swap the trigger in MemoryConfig to change extraction behaviour without
touching MemoryStore or MemoryExtractor.

  SubstantialContentTrigger  — default; fires when response exceeds min_words
  EveryNTurnsTrigger         — fires every N turns regardless of content length
  AlwaysTrigger              — fires on every turn (useful for testing)
  ManualOnlyTrigger          — never auto-fires; extraction only via /remember
"""

from abc import ABC, abstractmethod


class ExtractionTrigger(ABC):
    """Base class for all extraction trigger strategies."""

    @abstractmethod
    def should_extract(self, prompt: str, response: str) -> bool:
        """Return True if memory extraction should run for this turn."""
        ...


class SubstantialContentTrigger(ExtractionTrigger):
    """Fire when the response contains at least min_words words.

    Skips extraction for short acknowledgements ("OK", "Done", "Sure!") that
    are unlikely to contain memorable facts.
    """

    def __init__(self, min_words: int = 50) -> None:
        self.min_words = min_words

    def should_extract(self, prompt: str, response: str) -> bool:
        return len(response.split()) >= self.min_words


class EveryNTurnsTrigger(ExtractionTrigger):
    """Fire on every Nth turn, counting from the first call.

    Stateful: maintains an internal turn counter. Creates one counter per
    instance — do not share an instance across multiple REPL sessions.
    """

    def __init__(self, n: int = 5) -> None:
        if n < 1:
            raise ValueError(f"n must be >= 1, got {n}")
        self.n = n
        self._turn_count = 0

    def should_extract(self, prompt: str, response: str) -> bool:
        self._turn_count += 1
        return self._turn_count % self.n == 0


class AlwaysTrigger(ExtractionTrigger):
    """Fire on every turn. Useful for testing and maximum memory capture."""

    def should_extract(self, prompt: str, response: str) -> bool:
        return True


class ManualOnlyTrigger(ExtractionTrigger):
    """Never auto-fire. Extraction only happens via explicit /remember commands.

    Used when memory_enabled is False or for private mode sessions.
    """

    def should_extract(self, prompt: str, response: str) -> bool:
        return False
