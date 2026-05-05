"""HookRunner — dispatches lifecycle events to registered handlers.

Errors in any individual handler are isolated: they are silently swallowed
and converted to a no-op HookResult. Hooks never crash the agent loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .events import HookEvent, PostToolUseEvent, PreToolUseEvent, UserPromptSubmitEvent
from .handler import HookHandler
from .result import HookResult

if TYPE_CHECKING:
    pass


class HookRunner:
    def __init__(self, handlers: list[HookHandler]) -> None:
        self._handlers = handlers
        self._enabled = True
        self.pending_tips: list[str] = []

    # ── Core dispatch ──────────────────────────────────────────────────────

    async def fire(self, event: HookEvent) -> list[HookResult]:
        """Fire all matching handlers. Tips are accumulated into pending_tips."""
        if not self._enabled:
            return []
        results: list[HookResult] = []
        for handler in self._handlers:
            if handler.matches(event):
                try:
                    result = await handler.execute(event)
                except Exception:
                    result = HookResult()
                results.append(result)
                if result.tip:
                    self.pending_tips.append(result.tip)
        return results

    # ── Specialised fire methods ───────────────────────────────────────────

    async def fire_pre_tool(self, event: PreToolUseEvent) -> HookResult | None:
        """Returns the first blocking result, or None to proceed."""
        results = await self.fire(event)
        return next((r for r in results if r.action == "block"), None)

    async def fire_post_tool(self, event: PostToolUseEvent) -> None:
        await self.fire(event)

    async def fire_prompt(self, event: UserPromptSubmitEvent) -> HookResult | None:
        """Returns blocking result if any handler cancels the prompt."""
        results = await self.fire(event)
        return next((r for r in results if r.action == "block"), None)

    # ── Tips ───────────────────────────────────────────────────────────────

    def drain_tips(self) -> list[str]:
        """Return deduplicated accumulated tips and clear the buffer."""
        tips = list(dict.fromkeys(self.pending_tips))   # preserve order, drop duplicates
        self.pending_tips = []
        return tips

    # ── Enable / disable ───────────────────────────────────────────────────

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def handler_count(self) -> int:
        return len(self._handlers)

    def describe(self) -> list[dict]:
        """Return a list of handler descriptions for /hooks list."""
        rows = []
        for h in self._handlers:
            if hasattr(h, "_defn"):
                defn = h._defn  # type: ignore[attr-defined]
                rows.append({
                    "type": "shell",
                    "event": defn.event,
                    "tool": defn.tool or "(all tools)",
                    "detail": defn.command,
                })
            elif hasattr(h, "hook_describe"):
                rows.append(h.hook_describe())
            else:
                rows.append({"type": "builtin", "event": "—", "tool": "—",
                             "detail": type(h).__name__})
        return rows
