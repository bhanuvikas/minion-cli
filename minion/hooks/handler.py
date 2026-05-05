"""HookHandler Protocol.

Every hook handler — built-in Python or user-defined shell — implements this interface.
Open/Closed: new handler types (HTTP, MCP tool) are added by implementing this protocol
without modifying HookRunner or ToolExecutor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .events import HookEvent
from .result import HookResult


@runtime_checkable
class HookHandler(Protocol):
    def matches(self, event: HookEvent) -> bool:
        """Return True if this handler should fire for the given event."""
        ...

    async def execute(self, event: HookEvent) -> HookResult:
        """Execute the hook. Must not raise — convert errors to HookResult."""
        ...
