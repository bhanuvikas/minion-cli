"""HookRunner — dispatches lifecycle events to registered handlers.

Errors in any individual handler are isolated: they are silently swallowed
and converted to a no-op HookResult. Hooks never crash the agent loop.
"""

from __future__ import annotations

import time
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
        from ..tracing import get_tracer
        results: list[HookResult] = []
        for handler in self._handlers:
            if handler.matches(event):
                _t0 = time.monotonic()
                try:
                    result = await handler.execute(event)
                except Exception:
                    result = HookResult()
                _latency_ms = int((time.monotonic() - _t0) * 1000)
                results.append(result)
                if result.tip:
                    self.pending_tips.append(result.tip)
                _is_shell = hasattr(handler, "_defn")
                get_tracer().emit(
                    "hook_fire",
                    hook_event=event.event_name,
                    tool_name=getattr(event, "tool_name", ""),
                    handler_type="shell" if _is_shell else f"builtin:{type(handler).__name__}",
                    command=handler._defn.command if _is_shell else "",  # type: ignore[union-attr]
                    action=result.action,
                    tip=result.tip,
                    blocked=result.action == "block",
                    exit_code=result.exit_code,
                    latency_ms=_latency_ms,
                )
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
                rows.append(h.hook_describe())  # type: ignore[union-attr]
            else:
                rows.append({"type": "builtin", "event": "—", "tool": "—",
                             "detail": type(h).__name__})
        return rows
