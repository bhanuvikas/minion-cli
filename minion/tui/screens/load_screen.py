"""LoadScreen — /load modal for the Textual TUI.

Three visual states:
  browse          — list of sessions with metadata, detail pane on right.
  search          — live-filter list as user types.
  confirm_delete  — inline confirmation; second d executes deletion.
  empty           — no saved sessions; shows helper text.

Layered esc:
  confirm_delete  → back to browse (no deletion)
  query active    → clear query → full list
  otherwise       → dismiss modal (returns None)

Enter with a session highlighted → dismiss(session_name) so the caller
can load the conversation.
"""

from __future__ import annotations

import re as _re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import RenderableType
from rich.markup import escape as _esc
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .base import ModalSearchBar

# ── Color tokens (aligned with help_screen / memories_screen palette) ─────────

_BRIGHT   = "#f5d76e"   # selected row name (warm gold)
_GOLD     = "#e5c46b"   # header, badge
_GOLD_DIM = "#b8a030"   # section labels (DETAILS / FIRST MESSAGE)
_GREEN    = "#6ed98e"   # detail values (like /help USAGE lines)
_CYAN     = "#6aa3d4"   # related links / model name
_ORANGE   = "#d97757"   # search match highlight, danger
_DIM      = "#7a7464"   # secondary text
_FAINT    = "#4a4639"   # very faint / column headers
_RULE     = "#2e2e2e"   # borders (more visible, matches /help)
_TEXT     = "#d8cfb8"   # primary text
_TINT_YEL = "#1a1400"   # focused row background
_TINT_RED = "#1a0000"   # delete row background
_SILVER   = "#c0c0c0"   # keybinding hints


def _age(iso: str) -> str:
    """Return a human-readable relative age from an ISO 8601 timestamp."""
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        return f"{int(secs / 86400)}d ago"
    except (ValueError, OverflowError):
        return "unknown"


def _model_short(model: str) -> str:
    """Strip 'claude-' prefix and date suffix: 'claude-haiku-4-5-20251001' → 'haiku-4-5'."""
    m = model.removeprefix("claude-") if model else ""
    if not m:
        return "—"
    return _re.sub(r"-\d{8}$", "", m)


class LoadScreen(ModalScreen):  # type: ignore[type-arg]
    """Full-screen session picker opened by /load and /resume."""

    CSS = f"""
LoadScreen {{
    align: center middle;
    background: #000000 40%;
}}
#ld-panel {{
    width: 85%;
    height: 88%;
    background: #0d0d0d;
    border: round #3a3a3a;
}}
#ld-title {{
    height: auto;
    padding: 0 2;
    background: #0d0d0d;
    border-bottom: solid {_RULE};
}}
#ld-body {{
    height: 1fr;
}}
#ld-list-pane {{
    width: 50%;
    border-right: solid {_RULE};
}}
#ld-list-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
}}
#ld-list {{
    height: auto;
    padding: 0 0;
}}
#ld-preview-pane {{
    width: 50%;
    padding: 1 2;
}}
#ld-preview {{
    height: auto;
}}
#ld-footer {{
    height: 2;
    padding: 0 2;
    background: #0d0d0d;
    color: {_DIM};
    border-top: solid {_RULE};
}}
"""

    BINDINGS = [
        Binding("escape", "esc_action", show=False, priority=True),
        Binding("up",     "nav_up",     show=False, priority=True),
        Binding("down",   "nav_down",   show=False, priority=True),
        Binding("enter",  "confirm",    show=False, priority=True),
    ]

    def __init__(self, initial_query: str = "") -> None:
        super().__init__()
        self._initial_query = initial_query
        self._sessions: list = []   # list[SessionMeta]
        self._visible:  list = []   # filtered subset
        self._selected: int  = 0
        self._query:    str  = ""
        self._mode:     str  = "browse"  # browse | search | confirm_delete | empty

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="ld-panel"):
            yield Static("", id="ld-title")
            with Horizontal(id="ld-body"):
                with Vertical(id="ld-list-pane"):
                    yield ModalSearchBar(placeholder="filter sessions…", id="ld-search")
                    with VerticalScroll(id="ld-list-scroll"):
                        yield Static("", id="ld-list")
                with Vertical(id="ld-preview-pane"):
                    yield Static("", id="ld-preview")
            yield Static("", id="ld-footer")

    def on_mount(self) -> None:
        self._load_sessions()
        if self._initial_query:
            self._query = self._initial_query
            search = self.query_one("#ld-search", ModalSearchBar)
            search.query_one(Input).value = self._initial_query
            self._rebuild_visible()
        panel = self.query_one("#ld-panel", Vertical)
        panel.can_focus = True
        panel.focus()
        self._refresh()

    # ── Data ──────────────────────────────────────────────────────────────────

    def _load_sessions(self) -> None:
        from ...runner.session import list_sessions_with_metadata
        self._sessions = list_sessions_with_metadata()
        self._rebuild_visible()

    def _rebuild_visible(self) -> None:
        q = self._query.lower()
        self._visible = (
            [s for s in self._sessions if q in s.name.lower()]
            if q else list(self._sessions)
        )
        if self._visible:
            self._selected = min(self._selected, len(self._visible) - 1)
        else:
            self._selected = 0
        if not self._sessions:
            self._mode = "empty"
        elif self._mode not in ("confirm_delete",):
            self._mode = "search" if q else "browse"

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self.query_one("#ld-title",   Static).update(self._build_title())
        self.query_one("#ld-list",    Static).update(self._build_list())
        self.query_one("#ld-preview", Static).update(self._build_preview())
        self.query_one("#ld-footer",  Static).update(self._build_footer())

    # ── Renderers ─────────────────────────────────────────────────────────────

    def _build_title(self) -> RenderableType:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column(no_wrap=True)
        tbl.add_column(no_wrap=True, justify="right")

        left = Text.from_markup(f"[{_DIM}]⌐ /load[/] [{_DIM}]— sessions[/]")

        if self._mode == "confirm_delete" and self._visible:
            right = Text.from_markup(f"[bold {_ORANGE}]delete pending[/]")
        elif self._visible and self._mode in ("browse", "search"):
            sel = self._visible[self._selected]
            right = Text.from_markup(
                f"[{_DIM}]↵ load:[/] [bold {_GREEN}]{_esc(sel.name)}[/]"
            )
        elif self._sessions:
            n = len(self._sessions)
            label = f"{n} session{'s' if n != 1 else ''}"
            if self._query:
                label += f"  ·  {len(self._visible)} match{'es' if len(self._visible) != 1 else ''}"
            right = Text.from_markup(f"[{_DIM}]{label}[/]")
        else:
            right = Text.from_markup(f"[{_DIM}]no sessions[/]")

        tbl.add_row(left, right)
        return tbl

    def _build_greeting(self) -> str:
        kc = "bold white on #4a4a4a"   # style only — no brackets
        dim = _DIM
        if self._mode == "confirm_delete":
            return (
                f"[bold {_ORANGE}]⚠[/]  "
                f"[{dim}]Press [{kc}] d [/] again to confirm deletion, "
                f"or [{kc}] esc [/] to cancel.[/]"
            )
        if self._mode == "empty":
            return (
                f"[bold {_GOLD}]Bello![/]  "
                f"[{dim}]No saved sessions yet. Use [{kc}]/save <name>[/] to create one.[/]"
            )
        return (
            f"[bold {_GOLD}]Bello![/]  "
            f"[{dim}]Navigate [{kc}] ↑↓ [/]  ·  "
            f"[{kc}] ↵ [/] load  ·  "
            f"[{kc}] d [/] delete  ·  "
            f"type to filter[/]"
        )

    def _build_list(self) -> RenderableType:
        if self._mode == "empty":
            t = Text()
            t.append("\n  No saved sessions found.\n", style=_DIM)
            t.append("  Use ", style=_FAINT)
            t.append("/save <name>", style=_GOLD)
            t.append(" to create one.", style=_FAINT)
            return t

        if not self._visible:
            t = Text()
            t.append(f"\n  No sessions match ", style=_DIM)
            t.append(f'"{self._query}"', style=_ORANGE)
            return t

        outer = Table.grid(expand=True, padding=0)
        outer.add_column()

        # Faint column header row
        hdr = Table.grid(expand=True, padding=0)
        hdr.add_column(width=5,  no_wrap=True)
        hdr.add_column(ratio=1,  no_wrap=True)
        hdr.add_column(width=10, no_wrap=True)
        hdr.add_column(width=12, no_wrap=True)
        hdr.add_column(width=14, no_wrap=True)
        hdr.add_column(width=8,  no_wrap=True)
        hdr.add_column(width=2,  no_wrap=True)
        hdr.add_row(
            Text(""),
            Text("name", style=_FAINT, no_wrap=True),
            Text("msgs", style=_FAINT, no_wrap=True),
            Text("tokens", style=_FAINT, no_wrap=True),
            Text("model", style=_FAINT, no_wrap=True),
            Text("age", style=_FAINT, no_wrap=True),
            Text(""),
        )
        outer.add_row(hdr)

        # Columns: ptr | name (fills) | msgs | tokens | model | age | pad
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column(width=5,  no_wrap=True)
        tbl.add_column(ratio=1,  no_wrap=True)
        tbl.add_column(width=10, no_wrap=True)
        tbl.add_column(width=12, no_wrap=True)
        tbl.add_column(width=14, no_wrap=True)
        tbl.add_column(width=8,  no_wrap=True)
        tbl.add_column(width=2,  no_wrap=True)

        for i, s in enumerate(self._visible):
            is_sel = (i == self._selected)
            is_del = (is_sel and self._mode == "confirm_delete")

            if is_del:
                row_style  = f"on {_TINT_RED}"
                name_style = f"bold {_ORANGE}"
                meta_style = f"strike {_DIM}"
                ptr_text   = Text(" ✕ ", style=f"bold {_ORANGE}", no_wrap=True)
            elif is_sel:
                row_style  = f"on {_TINT_YEL}"
                name_style = f"bold {_BRIGHT}"
                meta_style = _DIM
                ptr_text   = Text(" ▸ ", style=f"bold {_BRIGHT}", no_wrap=True)
            else:
                row_style  = ""
                name_style = _TEXT
                meta_style = _FAINT
                ptr_text   = Text("   ", no_wrap=True)

            # Session name with search highlight
            if self._query and self._query.lower() in s.name.lower():
                name_t = Text(no_wrap=True, overflow="ellipsis")
                lower = s.name.lower()
                q = self._query.lower()
                pos = 0
                while True:
                    idx = lower.find(q, pos)
                    if idx == -1:
                        name_t.append(s.name[pos:], style=name_style)
                        break
                    name_t.append(s.name[pos:idx], style=name_style)
                    name_t.append(s.name[idx:idx + len(q)], style=f"bold {_ORANGE}")
                    pos = idx + len(q)
            else:
                name_t = Text(s.name, style=name_style, no_wrap=True, overflow="ellipsis")

            msgs_str = f"{s.message_count} msg{'s' if s.message_count != 1 else ''}"
            tok_str  = (f"{s.total_tokens / 1000:.1f}k tok"
                        if s.total_tokens >= 1000 else f"{s.total_tokens} tok")
            mod_str  = _model_short(s.model)
            age_str  = _age(s.saved_at)

            tbl.add_row(
                ptr_text,
                name_t,
                Text(msgs_str, style=meta_style, no_wrap=True),
                Text(tok_str,  style=meta_style, no_wrap=True),
                Text(mod_str,  style=meta_style, no_wrap=True),
                Text(age_str,  style=meta_style, no_wrap=True),
                Text(""),
                style=row_style,
            )

        outer.add_row(tbl)
        return outer

    def _build_preview(self) -> str:
        if self._mode == "empty" or not self._visible:
            return f"[{_FAINT}]\n  Select a session to preview.[/]"

        s = self._visible[self._selected]

        if self._mode == "confirm_delete":
            lines = [
                "",
                f"[bold {_ORANGE}]Delete this session?[/]",
                "",
                f"[bold {_TEXT}]  {_esc(s.name)}[/]",
                "",
                f"[{_DIM}]This cannot be undone. The session file will be[/]",
                f"[{_DIM}]permanently removed from ~/.minion/sessions/.[/]",
                "",
                f"[bold white on #4a4a4a] d [/] [{_DIM}]confirm delete[/]   "
                f"[bold white on #4a4a4a] esc [/] [{_DIM}]cancel[/]",
            ]
            return "\n".join(lines)

        # Format saved_at
        saved_fmt = s.saved_at
        try:
            dt = datetime.fromisoformat(s.saved_at)
            saved_fmt = dt.strftime("%Y-%m-%d  %H:%M")
        except (ValueError, AttributeError):
            pass

        tok_fmt = f"{s.total_tokens:,}" if s.total_tokens else "0"
        mod_full = s.model or "—"
        age_str = _age(s.saved_at)

        lines: list[str] = [
            f"[bold {_BRIGHT}]{_esc(s.name)}[/]  [{_DIM} on #1a1a1a]  {_esc(_model_short(s.model))}  [/]",
            "",
            f"[{_DIM}]{_esc(age_str)} · {s.message_count} messages · {_esc(tok_fmt)} tokens[/]",
            "",
            f"[bold {_GOLD_DIM}]DETAILS[/]",
            f"[{_GREEN}]  Saved      {_esc(saved_fmt or '—')}[/]",
            f"[{_GREEN}]  Model      {_esc(mod_full)}[/]",
            f"[{_GREEN}]  Messages   {s.message_count}[/]",
            f"[{_GREEN}]  Tokens     {_esc(tok_fmt)}[/]",
        ]

        if s.first_user_msg:
            preview = s.first_user_msg.replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:157] + "…"
            lines += [
                "",
                f"[bold {_GOLD_DIM}]FIRST MESSAGE[/]",
                f"[{_DIM}]  {_esc(preview)}[/]",
            ]

        if s.last_user_msg:
            preview = s.last_user_msg.replace("\n", " ")
            if len(preview) > 160:
                preview = preview[:157] + "…"
            lines += [
                "",
                f"[bold {_GOLD_DIM}]LAST MESSAGE[/]",
                f"[{_DIM}]  {_esc(preview)}[/]",
            ]

        return "\n".join(lines)

    def _build_footer(self) -> str:
        dot = f"[{_DIM}]·[/]"
        s = f"[{_SILVER}]"
        d = f"[{_DIM}]"
        if self._mode == "confirm_delete":
            return (
                f"  [{_SILVER}]d[/] {d}confirm delete[/]  {dot}  "
                f"[{_SILVER}]esc[/] {d}cancel[/]"
            )
        if self._mode == "empty":
            return f"  [{_SILVER}]esc[/] {d}dismiss[/]"
        return (
            f"  {s}↑ ↓[/] {d}navigate[/]  {dot}  "
            f"{s}↵[/] {d}load[/]  {dot}  "
            f"{s}d[/] {d}delete[/]  {dot}  "
            f"{s}type[/] {d}to filter[/]  {dot}  "
            f"{s}esc[/] {d}dismiss[/]"
        )

    # ── Input handling ────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        self._query = event.value
        self._rebuild_visible()
        self._refresh()

    def on_input_submitted(self, _: Input.Submitted) -> None:
        self.query_one("#ld-panel", Vertical).focus()

    def on_key(self, event: Key) -> None:
        if event.key == "d":
            if self._mode == "confirm_delete":
                self._do_delete()
                event.stop()
            elif self._visible and self._mode in ("browse", "search"):
                self._mode = "confirm_delete"
                self._refresh()
                event.stop()
        elif (
            event.character
            and event.character.isprintable()
            and event.key not in ("escape", "enter", "up", "down", "tab", "d")
        ):
            search = self.query_one("#ld-search", ModalSearchBar)
            inp = search.query_one(Input)
            inp.focus()
            if event.character != "/":
                # Pre-fill with the typed char; / just focuses without inserting
                inp.value = event.character
                inp.cursor_position = len(inp.value)
            event.stop()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_nav_up(self) -> None:
        if self._visible:
            self._selected = max(0, self._selected - 1)
            self._mode = "search" if self._query else "browse"
            self._refresh()

    def action_nav_down(self) -> None:
        if self._visible:
            self._selected = min(len(self._visible) - 1, self._selected + 1)
            self._mode = "search" if self._query else "browse"
            self._refresh()

    def action_esc_action(self) -> None:
        if self._mode == "confirm_delete":
            self._mode = "search" if self._query else "browse"
            self._refresh()
        elif self._query:
            self._query = ""
            search = self.query_one("#ld-search", ModalSearchBar)
            search.clear()
            self._rebuild_visible()
            self._refresh()
        else:
            self.dismiss(None)

    def action_confirm(self) -> None:
        if self._visible and self._mode in ("browse", "search"):
            self.dismiss(self._visible[self._selected].name)

    # ── Delete ────────────────────────────────────────────────────────────────

    def _do_delete(self) -> None:
        if not self._visible:
            return
        s = self._visible[self._selected]
        try:
            (Path.home() / ".minion" / "sessions" / f"{s.name}.json").unlink(missing_ok=True)
        except Exception:
            pass
        self._sessions = [x for x in self._sessions if x.name != s.name]
        self._selected = min(self._selected, max(0, len(self._sessions) - 1))
        self._rebuild_visible()
        self._mode = "browse" if self._sessions else "empty"
        self._refresh()
