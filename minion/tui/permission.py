"""Permission panel for the TUI bottom zone.

When a dangerous tool needs confirmation, ConfirmationManager calls
PermissionPanel.request() from the TUI event loop (via run_coroutine_threadsafe).
The panel replaces the input bar in the bottom zone until the user responds.

Scope options match _interactive_confirm() in tools/executor.py:
  1. Yes, once
  2. Yes, this session
  3. Yes, this project
  4. No
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from prompt_toolkit.formatted_text import FormattedText

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
    name:   str
    inputs: dict
    event:  asyncio.Event = field(default_factory=asyncio.Event)
    result: bool = False
    scope:  str  = "no"    # set when user confirms


class PermissionPanel:
    """Renders the scope-selector permission prompt in the TUI bottom zone."""

    def __init__(self, app_ref: "MinionApp") -> None:
        self._app     = app_ref
        self._pending: Optional[PermissionRequest] = None
        self._cursor:  int = 0

    # ── Request API (called from TUI event loop via run_coroutine_threadsafe) ─

    async def request(self, name: str, inputs: dict) -> bool:
        """Show the permission panel and wait for user response.

        Runs in the TUI event loop. The calling agent thread blocks on
        future.result() until this coroutine resolves.
        """
        req = PermissionRequest(name=name, inputs=inputs)
        self._pending = req
        self._cursor  = 0
        self._app.invalidate()
        await req.event.wait()
        self._pending = None
        self._app.invalidate()
        return req.result

    # ── Key handler helpers (called from app.py key bindings) ─────────────────

    @property
    def is_visible(self) -> bool:
        return self._pending is not None

    def move_cursor(self, delta: int) -> None:
        self._cursor = max(0, min(len(_SCOPE_LABELS) - 1, self._cursor + delta))

    def confirm_by_index(self, index: int) -> None:
        """Confirm with a specific scope index (0-3)."""
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
        """Confirm with the currently-highlighted cursor position."""
        self.confirm_by_index(self._cursor)

    def deny(self) -> None:
        """Deny (same as selecting 'No')."""
        self.confirm_by_index(3)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def get_formatted_text(self) -> FormattedText:
        if self._pending is None:
            return FormattedText([])

        req   = self._pending
        frags: list[tuple[str, str]] = []

        # Tool name header
        frags.append(("class:perm-tool", f"  {req.name}"))

        # Detail line (path / command)
        detail = _permission_detail(req.name, req.inputs)
        if detail:
            frags.append(("class:perm-detail", f"  {detail}"))
        frags.append(("", "\n"))

        # Scope options — one per line
        for i, label in enumerate(_SCOPE_LABELS):
            selected = i == self._cursor
            cursor_str = "  ❯" if selected else "   "
            style = "class:perm-selected" if selected else "class:perm-option"
            frags.append(("class:perm-cursor", cursor_str))
            frags.append((style, f" {i + 1}.{label}"))
            frags.append(("", "\n"))
        frags.append(("class:perm-detail", "  ↑↓ move  1-4 select  Enter confirm  Esc deny"))

        return FormattedText(frags)


def _permission_detail(name: str, inputs: dict) -> str:
    """Return a brief detail string for the permission prompt header."""
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
