"""Shared CSS, markup helpers, and primitives for TUI modal screens.

Every slash-command modal screen (model_config, future /context, /memory, …)
imports its CSS and markup helpers from here so the visual language stays
consistent across all wizard-style overlays.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Input

from ..theme import DIM, GOLD, GREEN, SILVER


# ── Shared color tokens (used by agents_screen and skills_screen) ─────────────

_ORANGE    = "#d97757"    # selected row, delete, warnings
_GOLD      = "#c8a84b"    # user-tier color
_GOLD_DIM  = "#b8a030"    # dim gold for labels
_GREEN     = "#7ec8a0"    # project-tier color
_GREEN_DIM = "#5a9070"    # dim green for unselected project names
_BLUE      = "#6aa3d4"    # model names, accents
_SILVER    = "#bbbbbb"    # body text, keycaps
_DIM       = "#888888"    # secondary text
_FAINT     = "#555555"    # tertiary, section labels
_RULE      = "#2a2820"    # border lines
_TEXT      = "#d8cfb8"    # primary warm-white text
_TINT_ORG  = "#1a0800"    # faint orange background (selected rows)
_BG        = "#0d0d0d"    # panel background

# ── Shared tool data (used by agents_screen and skills_screen) ────────────────

_TOOL_CATEGORIES: dict[str, str] = {
    "read_file":        "filesystem",
    "write_file":       "filesystem",
    "edit_file":        "filesystem",
    "list_directory":   "filesystem",
    "delete_file":      "filesystem",
    "glob":             "filesystem",
    "get_file_outline": "filesystem",
    "search_file":      "filesystem",
    "run_shell":        "shell",
    "web_fetch":        "network",
    "spawn_agent":      "agents",
    "todo_read":        "tasks",
    "todo_write":       "tasks",
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "read_file":        "read any file by path",
    "write_file":       "write or replace a file",
    "edit_file":        "edit a file in-place",
    "list_directory":   "enumerate a directory",
    "delete_file":      "remove a file",
    "glob":             "glob file patterns",
    "get_file_outline": "get code outline of a file",
    "search_file":      "search file for pattern",
    "run_shell":        "execute a shell command",
    "web_fetch":        "fetch URL contents",
    "spawn_agent":      "spawn a child subagent",
    "todo_read":        "read task list",
    "todo_write":       "write task list",
}

_TOOL_WARN: dict[str, str] = {
    "run_shell":   "⚠ broad",
    "delete_file": "⚠ destructive",
    "spawn_agent": "⚠ recursion",
}

_NATIVE_TOOLS: list[str] = list(_TOOL_DESCRIPTIONS.keys())

_TIER_ORDER: dict[str, int] = {"builtin": 0, "user": 1, "project": 2}

# ── Shared helper functions ───────────────────────────────────────────────────


def _age(path: Optional[Path]) -> str:
    """Return a human-readable relative age from file mtime."""
    if path is None or not path.exists():
        return "unknown"
    try:
        mtime = path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        secs = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except (OSError, OverflowError, ValueError):
        return "unknown"


def _highlight(text: str, query: str, base_style: str = _TEXT) -> Text:
    """Return a Rich Text with all occurrences of query highlighted in orange."""
    if not query:
        return Text(text, style=base_style)
    t = Text()
    lower_text = text.lower()
    lower_q = query.lower()
    pos = 0
    while True:
        idx = lower_text.find(lower_q, pos)
        if idx == -1:
            t.append(text[pos:], style=base_style)
            break
        t.append(text[pos:idx], style=base_style)
        t.append(text[idx : idx + len(query)], style=f"bold {_ORANGE}")
        pos = idx + len(query)
    return t


def _tier_color(tier: str) -> str:
    if tier == "builtin":
        return _FAINT
    if tier == "user":
        return _GOLD
    if tier == "project":
        return _GREEN
    return _DIM


def _hint(key: str, label: str) -> str:
    return f"[bold {_SILVER} on #2a2a2a] {key} [/] [{_DIM}]{label}[/]"


# ── Shared reusable widgets ───────────────────────────────────────────────────

class ModalSearchBar(Widget):
    """Reusable search bar for modal screens.

    Yields a single ``Input`` with consistent dark-theme styling: dimmed
    background, subtle border that turns gold on focus.  Placeholder text
    and widget id are configurable.

    Usage::

        yield ModalSearchBar(placeholder="search…", id="my-search")
        # listen for Input.Changed / Input.Submitted on the parent screen
    """

    DEFAULT_CSS = """
    ModalSearchBar {
        height: auto;
        padding: 0 2;
        border-bottom: solid #2e2e2e;
        background: #0d0d0d;
    }
    ModalSearchBar > Input {
        margin: 0;
        background: #1a1a1a;
        border: solid #3a3a3a;
        color: #e6e6e6;
        padding: 0 1;
        height: 3;
    }
    ModalSearchBar > Input:focus {
        border: solid #e5c46b;
    }
    """

    def __init__(self, placeholder: str = "search…", id: str | None = None) -> None:
        super().__init__(id=id)
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self._placeholder)

    @property
    def value(self) -> str:
        try:
            return self.query_one(Input).value
        except Exception:
            return ""

    def focus_input(self) -> None:
        self.query_one(Input).focus()

    def clear(self) -> None:
        self.query_one(Input).value = ""

# ── Shared wizard CSS ─────────────────────────────────────────────────────────

WIZARD_CSS = f"""
WizardScreen, ModelConfigScreen {{
    align: center middle;
    background: #000000 40%;
}}
#wizard-panel {{
    width: 90%;
    height: 90%;
    background: #0d0d0d;
    border: round #3a3a3a;
}}
#wizard-title {{
    height: auto;
    padding: 0 2;
    background: #0d0d0d;
    border-bottom: solid #2e2e2e;
}}
#currently-using {{
    height: auto;
    padding: 1 2;
    border-bottom: solid #2a2a26;
    background: #0f0f0d;
}}
#wizard-body {{
    height: 1fr;
    padding: 1 2;
    align: center top;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {DIM};
}}
#wizard-body > Static {{
    height: auto;
    width: auto;
}}
#wizard-foot {{
    height: 2;
    padding: 0 2;
    background: #0d0d0d;
    color: {DIM};
    border-top: solid #2e2e2e;
}}
Input {{
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 1 0;
}}
Input:focus {{
    border: solid {GOLD};
}}
"""

# ── Title bar ─────────────────────────────────────────────────────────────────

_STEP_LABELS = ["provider", "model", "api key"]


def _build_step_rail(step: int) -> str:
    parts: list[str] = []
    for i, label in enumerate(_STEP_LABELS):
        n = i + 1
        if n < step:
            parts.append(f"[bold #0d0d0d on {GREEN}] ✓ [/] [{GREEN}]{label}[/]")
        elif n == step:
            parts.append(f"[bold #0d0d0d on {GOLD}] {n} [/] [bold {GOLD}]{label}[/]")
        else:
            parts.append(f"[{DIM} on #252525] {n} [/] [{DIM}]{label}[/]")
        if i < 2:
            parts.append(f"  [{DIM}]────[/]  ")
    return "".join(parts)


def build_title_bar(step: int) -> Table:
    """Rich Table renderable: left title + right-aligned step rail in one row."""
    table = Table.grid(expand=True, padding=0)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, justify="right")
    left  = Text.from_markup(
        f"[{DIM}]┌─[/] [bold]/model[/] [{DIM}]— let's swap brains[/]"
    )
    right = Text.from_markup(_build_step_rail(step))
    table.add_row(left, right)
    return table


# ── Footer key hints ──────────────────────────────────────────────────────────

_STEP_FOOT: dict[int | str, list[tuple[list[str], str]]] = {
    1: [
        (["↑", "↓"], "navigate"),
        (["↵"], "choose provider"),
        (["esc"], "cancel"),
    ],
    2: [
        (["↑", "↓"], "navigate"),
        (["↵"], "select model"),
        (["shift+tab"], "back to providers"),
        (["esc"], "cancel"),
    ],
    3: [
        (["⌘V"], "paste"),
        (["↵"], "save"),
        (["shift+tab"], "prev"),
        (["esc"], "cancel"),
    ],
    "3-scope": [
        (["← →"], "switch scope"),
        (["↓", "tab"], "next section"),
        (["shift+tab"], "back to model"),
        (["esc"], "cancel"),
    ],
    "3-test": [
        (["← →"], "toggle yes/no"),
        (["↑", "↓"], "navigate sections"),
        (["shift+tab"], "prev"),
        (["esc"], "cancel"),
    ],
    "3-input": [
        (["⌘V"], "paste"),
        (["↑"], "prev section"),
        (["↵"], "save"),
        (["esc"], "cancel"),
    ],
    "3-confirm-skip": [(["↵"], "again to save"), (["esc"], "cancel")],
    "3-validating": [(["…"], "testing connection"), (["esc"], "cancel")],
    "3-success":    [(["↵"], "apply · close"), (["esc"], "cancel")],
    "3-error":      [(["↵"], "re-test"), (["⌫"], "edit"), (["esc"], "cancel")],
}


def build_footer_markup(step: int, sub: str = "") -> str:
    """Rich markup for the footer key-hint bar."""
    key   = f"{step}-{sub}" if sub else step
    items = _STEP_FOOT.get(key, _STEP_FOOT.get(step, []))  # type: ignore[call-overload]
    parts: list[str] = []
    for keys, label in items:
        key_spans = " ".join(f"[bold {SILVER}]{k}[/]" for k in keys)
        parts.append(f"{key_spans} [{DIM}]{label}[/]")
    return "  " + f"  [{DIM}]·[/]  ".join(parts)


# ── Currently-using strip ─────────────────────────────────────────────────────

def build_currently_using(provider: dict, model: dict) -> Table:
    """Two-column Rich Table: left = NOW + badge (centered); right = model info + tagline."""
    from ...config.model_catalog import fmt_ctx, fmt_price
    color  = provider.get("color", SILVER)
    ctx    = fmt_ctx(model["ctx"])
    in_p   = fmt_price(model["in_price"])
    out_p  = fmt_price(model["out_price"])

    badge = Text.from_markup(f"[bold {color} on #1e1900] {provider['mark']} [/]")
    left  = Text.assemble(Text("CURRENT ", style=f"bold {DIM}"), badge)

    # Right column: line 1 = provider › model + pills; line 2 = tagline
    ctx_pill   = Text.from_markup(f"[{DIM} on #1c1c1c] ctx {ctx} [/]")
    price_pill = Text.from_markup(f"[{DIM} on #1c1c1c] {in_p} / {out_p} per Mtok [/]")

    line1 = Text.assemble(
        Text.from_markup(f"[bold {color}]{provider['name']}[/]"),
        Text.from_markup(f"  [{DIM}]›[/]  "),
        Text.from_markup(f"[bold {GOLD}]{model['id']}[/]"),
        "  ",
        ctx_pill,
        "  ",
        price_pill,
    )
    line2 = Text.from_markup(f"[{DIM}]{model['tag']}[/]")
    right = Text.assemble(line1, "\n", line2)

    table = Table.grid(padding=(0, 2))
    # no_wrap keeps "NOW [A]" on one line; vertical="middle" centres it in the 2-row cell
    table.add_column(no_wrap=True, vertical="middle")
    table.add_column()
    table.add_row(left, right)
    return table
