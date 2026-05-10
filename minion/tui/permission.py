"""Permission panel for the TUI.

When a dangerous tool needs confirmation, ConfirmationManager calls
PermissionPanel.request() from the TUI event loop (via call_from_thread).
The panel replaces the input area until the user responds.

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

_TOOL_ICONS = {
    "write_file":  "✎",
    "edit_file":   "✎",
    "run_shell":   "$",
    "web_fetch":   "◎",
}

_TOOL_QUESTIONS = {
    "write_file":  "Allow this file write?",
    "edit_file":   "Allow this edit?",
    "run_shell":   "Allow this command?",
    "web_fetch":   "Allow this fetch?",
}

# Tools where we replay the diff into the conversation after approval
_DIFF_TOOLS = {"write_file", "edit_file"}


@dataclass
class PermissionRequest:
    name:       str
    inputs:     dict
    diff_lines: str = ""
    event:      asyncio.Event = field(default_factory=asyncio.Event)
    result:     bool = False
    scope:      str  = "no"


class PermissionPanel:
    """Renders the permission prompt inline in the InputSection."""

    def __init__(self, app_ref: "MinionApp") -> None:
        self._app     = app_ref
        self._pending: Optional[PermissionRequest] = None
        self._cursor:  int = 0

        # Retained after request() completes so hide_permission() can read them
        self._last_result: bool = False
        self._last_diff:   str  = ""
        self._last_name:   str  = ""
        self._last_detail: str  = ""

    # ── Request API ───────────────────────────────────────────────────────────

    async def request(self, name: str, inputs: dict, diff_lines: str = "") -> bool:
        """Show the permission panel and wait for user response."""
        req = PermissionRequest(name=name, inputs=inputs, diff_lines=diff_lines)
        self._pending = req
        self._cursor  = 0
        self._app.show_permission()
        await req.event.wait()
        # Persist for hide_permission() before clearing _pending
        self._last_result = req.result
        self._last_diff   = req.diff_lines
        self._last_name   = req.name
        self._last_detail = _permission_detail(req.name, req.inputs)
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
        """Return Rich markup string for the PermissionContent Static widget."""
        if self._pending is None:
            return ""

        from rich.markup import escape as _esc

        req      = self._pending
        icon     = _TOOL_ICONS.get(req.name, "⬡")
        question = _TOOL_QUESTIONS.get(req.name, "Allow this action?")
        detail   = _permission_detail(req.name, req.inputs)
        lines: list[str] = []

        # ── Tool header ───────────────────────────────────────────────────────
        lines.append(f"[bold #1E90FF]  {icon} {req.name}[/]")
        if detail:
            lines.append(f"[#C0C0C0]  {_esc(detail)}[/]")

        # ── Diff / file preview ───────────────────────────────────────────────
        if req.diff_lines:
            import re as _re
            plain = _re.sub(r"\x1b\[[0-9;]*m", "", req.diff_lines)
            rows  = [r for r in plain.rstrip("\n").split("\n")]
            if rows:
                lines.append(f"[#333333]  {'─' * 60}[/]")
                for row in rows:
                    er = _esc(row)
                    if row.startswith("+"):
                        lines.append(f"[#4CAF50]  {er}[/]")
                    elif row.startswith("-"):
                        lines.append(f"[red]  {er}[/]")
                    elif row.startswith("@@"):
                        lines.append(f"[#888888]  {er}[/]")
                    else:
                        lines.append(f"[#666666]  {er}[/]")
                lines.append(f"[#333333]  {'─' * 60}[/]")

        # ── Question ──────────────────────────────────────────────────────────
        lines.append("")
        lines.append(f"  {question}")
        lines.append("")

        # ── Scope choices ─────────────────────────────────────────────────────
        for i, label in enumerate(_SCOPE_LABELS):
            selected   = i == self._cursor
            cursor_str = "❯" if selected else " "
            if selected:
                lines.append(f"[bold #FFD700]  {cursor_str} {i + 1}.{label}[/]")
            else:
                lines.append(f"[#C0C0C0]  {cursor_str} {i + 1}.{label}[/]")

        lines.append("")
        lines.append("[#666666]  ↑↓ navigate  1-4 select  Enter confirm  Esc deny[/]")
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
