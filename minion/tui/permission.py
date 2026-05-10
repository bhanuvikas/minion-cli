"""Permission panel for the TUI.

When a dangerous tool needs confirmation, ConfirmationManager calls
PermissionPanel.request() from the TUI event loop (via run_coroutine_threadsafe).
The panel replaces the input bar until the user responds.

Scope options match _interactive_confirm() in tools/executor.py:
  1. Yes, once
  2. Yes, this session
  3. Yes, this project
  4. No
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .app import MinionApp


_SCOPE_LABELS = [
    " Yes, once",
    " Yes, this session",
    " Yes, this project",
    " No",
]
_SCOPE_VALUES = ["once", "session", "project", "no"]


@dataclass
class PermissionRequest:
    name:       str
    inputs:     dict
    diff_lines: str = ""         # raw ANSI string for diff preview
    event:      asyncio.Event = field(default_factory=asyncio.Event)
    result:     bool = False
    scope:      str  = "no"


class PermissionPanel:
    """Renders the scope-selector permission prompt in the TUI permission zone."""

    def __init__(self, app_ref: "MinionApp") -> None:
        self._app     = app_ref
        self._pending: Optional[PermissionRequest] = None
        self._cursor:  int = 0

    # ── Request API (called from TUI event loop via run_coroutine_threadsafe) ─

    async def request(self, name: str, inputs: dict, diff_lines: str = "") -> bool:
        """Show the permission panel and wait for user response."""
        req = PermissionRequest(name=name, inputs=inputs, diff_lines=diff_lines)
        self._pending = req
        self._cursor  = 0
        self._app.show_permission()
        await req.event.wait()
        self._pending = None
        self._app.hide_permission()
        return req.result

    # ── Key handler helpers ───────────────────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        return self._pending is not None

    def move_cursor(self, delta: int) -> None:
        self._cursor = max(0, min(len(_SCOPE_LABELS) - 1, self._cursor + delta))

    def confirm_by_index(self, index: int) -> None:
        if self._pending is None:
            return
        req = self._pending
        if index < 0 or index >= len(_SCOPE_VALUES):
            req.result = False
            req.scope  = "no"
        else:
            scope      = _SCOPE_VALUES[index]
            req.result = scope != "no"
            req.scope  = scope
        req.event.set()

    def confirm_current(self) -> None:
        self.confirm_by_index(self._cursor)

    def deny(self) -> None:
        self.confirm_by_index(3)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def get_rich_markup(self) -> str:
        """Return Rich markup string for the PermissionZone Static widget."""
        if self._pending is None:
            return ""

        req   = self._pending
        lines: list[str] = []

        # Tool name + path/command header
        lines.append(f"[bold #FFD700]  {req.name}[/]")
        detail = _permission_detail(req.name, req.inputs)
        if detail:
            lines.append(f"[#C0C0C0]  {detail}[/]")

        # Diff preview (raw ANSI — strip to plain for markup rendering)
        if req.diff_lines:
            import re as _re
            plain_diff = _re.sub(r"\x1b\[[0-9;]*m", "", req.diff_lines)
            for dline in plain_diff.rstrip("\n").split("\n")[:30]:
                if dline.startswith("+"):
                    lines.append(f"[#4CAF50]{dline}[/]")
                elif dline.startswith("-"):
                    lines.append(f"[red]{dline}[/]")
                else:
                    lines.append(f"[#C0C0C0]{dline}[/]")

        # Scope options
        for i, label in enumerate(_SCOPE_LABELS):
            selected   = i == self._cursor
            cursor_str = "  ❯" if selected else "   "
            style      = "bold #FFD700" if selected else ""
            lines.append(f"[{style}]{cursor_str} {i + 1}.{label}[/]")

        lines.append("[#666666]  ↑↓ move  1-4 select  Enter confirm  Esc deny[/]")
        return "\n".join(lines)


def _permission_detail(name: str, inputs: dict) -> str:
    if name in ("write_file", "edit_file"):
        path = inputs.get("path", "")
        return path[:80] if path else ""
    if name == "run_shell":
        cmd = inputs.get("command", "")
        return cmd[:80] if cmd else ""
    if name == "web_fetch":
        url = inputs.get("url", "")
        return url[:80] if url else ""
    return ""
