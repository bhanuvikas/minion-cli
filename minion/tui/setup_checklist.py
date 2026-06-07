"""Inline setup checklist shown in the TUI on first run or via /setup.

Follows the PermissionPanel pattern: a plain Python class that renders Rich
markup into a SetupChecklistZone Static widget.  Key handling lives in
MinionApp.on_key() and InputArea action overrides.
"""
from __future__ import annotations

from typing import Callable, Optional

# ── Palette (mirrors tui/theme.py) ───────────────────────────────────────────
_GOLD   = "#FFD700"
_BLUE   = "#1E90FF"
_GREEN  = "#4CAF50"
_SILVER = "#C0C0C0"
_DIM    = "#666666"
_ORANGE = "#FF8C00"

_ROWS = [
    {
        "id":     "brain",
        "title":  "Pick a brain",
        "sub":    "provider · model · API key",
        "action": "opens /model wizard",
    },
    {
        "id":     "completion",
        "title":  "Install shell tab completion",
        "sub":    "tab-complete minion commands in your shell",
        "action": "opens install prompt",
    },
    {
        "id":     "init",
        "title":  "Initialize this project",
        "sub":    "scan repo, write MINION.md",
        "action": "runs /init",
    },
]


class SetupChecklistPanel:
    """Tracks state and renders the first-run setup checklist.

    Row state: "todo" | "active" | "done"
    """

    def __init__(self) -> None:
        self._state: list[str] = ["active", "todo", "todo"]
        self._cursor: int = 0

        # Callbacks — set by _run_repl_tui before show_setup_checklist()
        self.on_brain:      Optional[Callable[[], None]] = None
        self.on_completion: Optional[Callable[[], None]] = None
        self.on_init:       Optional[Callable[[], None]] = None
        self.on_dismiss:    Optional[Callable[[], None]] = None

        # Per-row done summaries (set when each row completes)
        self._summaries: dict[str, str] = {
            "brain": "", "completion": "", "init": "",
        }

    # ── State queries ─────────────────────────────────────────────────────────

    @property
    def is_complete(self) -> bool:
        return all(s == "done" for s in self._state)

    def done_count(self) -> int:
        return sum(1 for s in self._state if s == "done")

    # ── Cursor movement ───────────────────────────────────────────────────────

    def move_cursor(self, delta: int) -> None:
        """Advance cursor to the next non-done row in direction delta (+1/-1)."""
        n = len(_ROWS)
        candidate = (self._cursor + delta) % n
        for _ in range(n):
            if self._state[candidate] != "done":
                self._cursor = candidate
                return
            candidate = (candidate + delta) % n

    # ── Row activation ────────────────────────────────────────────────────────

    def activate_current(self) -> None:
        """Fire the callback for the currently focused row."""
        if self._state[self._cursor] == "done":
            return
        row_id = _ROWS[self._cursor]["id"]
        cb = {"brain": self.on_brain, "completion": self.on_completion, "init": self.on_init}
        if cb.get(row_id):
            cb[row_id]()   # type: ignore[call-arg]

    # ── Mark done ─────────────────────────────────────────────────────────────

    def mark_done(self, row_id: str, summary: str = "") -> None:
        """Mark a row as done and advance cursor to the next pending row."""
        for i, row in enumerate(_ROWS):
            if row["id"] == row_id:
                self._state[i] = "done"
                self._summaries[row_id] = summary
                # Advance cursor to first remaining non-done row
                for j in range(len(_ROWS)):
                    if self._state[j] != "done":
                        self._cursor = j
                        return
                break

    # ── Reset (for /setup re-run) ─────────────────────────────────────────────

    def reset(self) -> None:
        """Reset incomplete rows so /setup can run them again."""
        for i in range(len(_ROWS)):
            if self._state[i] != "done":
                self._state[i] = "todo"
        for i in range(len(_ROWS)):
            if self._state[i] != "done":
                self._state[i] = "active"
                self._cursor = i
                return

    # ── Rich markup rendering ─────────────────────────────────────────────────

    # Visible-text column widths — padding is applied to the raw text so
    # Rich markup escape sequences don't inflate the field width.
    _NAME_W = 30
    _DESC_W = 44

    def get_header_markup(self) -> str:
        done  = self.done_count()
        total = len(_ROWS)
        hint  = (
            f"  [{_DIM}]Three quick things to get you running.[/]"  if done == 0 else
            f"  [{_DIM}]Nice. Two more — tab completion and a project scan.[/]" if done == 1 else
            f"  [{_DIM}]Almost there. One last step — let me read your repo.[/]"
        )
        return (
            f"  [bold {_GOLD}]first-run setup[/]  [{_DIM}]· {done} of {total} done[/]"
            f"        [{_DIM}]↑↓ navigate  ·  [{_SILVER}]↵ open[/]  ·  [{_SILVER}]x[/] [{_DIM}]dismiss[/]"
            f"\n{hint}\n"
        )

    def get_row_markup(self, i: int) -> str:
        row    = _ROWS[i]
        state  = self._state[i]
        is_cur = (i == self._cursor and state != "done")

        # Number / status (1 visible char)
        if state == "done":
            num = f"[bold {_GREEN}]✓[/]"
        elif is_cur:
            num = f"[bold {_GOLD}]{i + 1}[/]"
        else:
            num = f"[{_DIM}]{i + 1}[/]"

        # Name column
        visible_name = row["title"]
        name_pad = " " * max(0, self._NAME_W - len(visible_name))
        if state == "done":
            name_col = f"[{_DIM}]{visible_name}[/]{name_pad}"
        elif is_cur:
            name_col = f"[bold {_GOLD}]{visible_name}[/]{name_pad}"
        else:
            name_col = f"[{_SILVER}]{visible_name}[/]{name_pad}"

        # Description column — clamp so long text never bleeds into action hint.
        if state == "done" and self._summaries[row["id"]]:
            visible_desc = self._summaries[row["id"]]
        else:
            visible_desc = row["sub"]
        if len(visible_desc) > self._DESC_W:
            visible_desc = visible_desc[:self._DESC_W - 1] + "…"
        desc_pad = " " * max(0, self._DESC_W - len(visible_desc))
        desc_col = f"[{_DIM}]{visible_desc}[/]{desc_pad}"

        # Action hint
        if state == "done":
            hint_col = f"[{_GREEN}]done[/]"
        elif is_cur:
            hint_col = f"[bold {_GOLD}]↵[/] [{_GOLD}]{row['action']}[/]"
        else:
            hint_col = f"[{_DIM}]{row['action']}[/]"

        return f"  {num}  {name_col}  {desc_col}  {hint_col}"

    def get_footer_markup(self) -> str:
        return (
            f"\n  [{_DIM}]└ only step 1 is required · steps 2 & 3 are optional"
            f" · re-open with [/][{_SILVER}]/setup[/]"
        )

    def get_done_banner(self) -> str:
        return self._done_banner()

    def _done_banner(self) -> str:
        lines: list[str] = []
        lines.append(
            f"  [bold {_GREEN}]setup complete · 3 of 3[/]"
            f"   [{_SILVER}]x[/] [{_DIM}]dismiss[/]"
        )
        lines.append(f"  [bold]All set. [{_GREEN}]Let's build something.[/][/]")
        lines.append(
            f"  [{_DIM}]Try [bold {_GOLD}]\"what does this codebase do?\"[/]"
            f" · re-run setup with [{_SILVER}]/setup[/][/]"
        )

        # Summary items
        parts: list[str] = []
        if self._summaries["brain"]:
            parts.append(f"[{_GREEN}]✓[/] [{_DIM}]{self._summaries['brain']}[/]")
        if self._summaries["completion"]:
            parts.append(f"[{_GREEN}]✓[/] [{_DIM}]{self._summaries['completion']}[/]")
        if self._summaries["init"]:
            parts.append(f"[{_GREEN}]✓[/] [{_DIM}]{self._summaries['init']}[/]")
        if parts:
            lines.append("")
            lines.append("  " + f"  [{_DIM}]·[/]  ".join(parts))

        return "\n".join(lines)
