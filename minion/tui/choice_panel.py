"""ChoicePanel — generic choice prompt for the TUI.

Used by ConfirmationManager.choose_sync() to present a numbered list of options
inline in the InputSection (replacing InputRow while active), using the same
threading pattern as PermissionPanel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .app import MinionApp


@dataclass
class ChoiceRequest:
    prompt:  str
    choices: list[str]
    event:   asyncio.Event = field(default_factory=asyncio.Event)
    result:  Optional[int] = None


class ChoicePanel:
    """Renders a numbered choice list inline in InputSection."""

    def __init__(self, app_ref: "MinionApp") -> None:
        self._app     = app_ref
        self._pending: Optional[ChoiceRequest] = None
        self._cursor:  int = 0

    # ── Request API ───────────────────────────────────────────────────────────

    async def request(self, prompt: str, choices: list[str]) -> Optional[int]:
        """Show choice panel and wait for user selection. Returns index or None (cancel)."""
        req = ChoiceRequest(prompt=prompt, choices=choices)
        self._pending = req
        self._cursor  = 0
        self._app.show_choice()
        await req.event.wait()
        self._pending = None
        self._app.hide_choice()
        return req.result

    # ── Key handler helpers ───────────────────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        return self._pending is not None

    def move_cursor(self, delta: int) -> None:
        if self._pending is None:
            return
        self._cursor = max(0, min(len(self._pending.choices) - 1, self._cursor + delta))

    def confirm_by_index(self, index: int) -> None:
        if self._pending is None:
            return
        req = self._pending
        if 0 <= index < len(req.choices):
            req.result = index
        else:
            req.result = None
        req.event.set()

    def confirm_current(self) -> None:
        self.confirm_by_index(self._cursor)

    def cancel(self) -> None:
        if self._pending is None:
            return
        self._pending.result = None
        self._pending.event.set()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def get_rich_markup(self) -> str:
        if self._pending is None:
            return ""
        req = self._pending
        lines: list[str] = []
        lines.append("")
        lines.append(f"  {req.prompt}")
        lines.append("")
        for i, label in enumerate(req.choices):
            selected = i == self._cursor
            cursor_str = "❯" if selected else " "
            num = i + 1
            if selected:
                lines.append(f"[bold #FFD700]  {cursor_str} {num}. {label}[/]")
            else:
                lines.append(f"[#C0C0C0]  {cursor_str} {num}. {label}[/]")
        lines.append("")
        n = len(req.choices)
        lines.append(f"[#666666]  ↑↓ navigate  1-{n} select  Enter confirm  Esc cancel[/]")
        return "\n".join(lines)
