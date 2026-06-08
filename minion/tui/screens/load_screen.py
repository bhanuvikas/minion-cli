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

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import RenderableType
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .base import ModalSearchBar

# ── Color tokens (aligned with memories_screen palette) ──────────────────────

_GOLD     = "#e5c46b"
_GOLD_DIM = "#b8a030"
_BLUE     = "#6aa3d4"
_ORANGE   = "#d97757"
_DIM      = "#7a7464"
_FAINT    = "#4a4639"
_RULE     = "#2a2820"
_TEXT     = "#d8cfb8"
_TINT_YEL = "#1a1400"
_TINT_ORG = "#1a0800"
_TINT_RED = "#1a0000"
_SILVER   = "#c0c0c0"
_RED      = "#e05555"


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
    """Strip 'claude-' prefix for compact display (e.g. 'sonnet-4-6')."""
    return model.removeprefix("claude-") if model else "—"


def _keycap(key: str) -> Text:
    return Text(f" {key} ", style=f"bold {_SILVER} on #2a2a2a")


class LoadScreen(ModalScreen):  # type: ignore[type-arg]
    """Full-screen session picker opened by /load and /resume."""

    CSS = f"""
LoadScreen {{
    align: center middle;
    background: #000000 40%;
}}
#ld-panel {{
    width: 90%;
    height: 85%;
    background: #0d0d0d;
    border: round {_RULE};
}}
#ld-header {{
    height: auto;
    padding: 0 2;
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
    scrollbar-color: #2a2a2a;
}}
#ld-list {{
    height: auto;
}}
#ld-preview-pane {{
    width: 50%;
    padding: 0 2;
}}
#ld-preview {{
    height: auto;
}}
#ld-footer {{
    height: 2;
    padding: 0 2;
    background: #0d0d0d;
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
            yield Static("", id="ld-header")
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
        self.query_one("#ld-header",  Static).update(self._build_header())
        self.query_one("#ld-list",    Static).update(self._build_list())
        self.query_one("#ld-preview", Static).update(self._build_preview())
        self.query_one("#ld-footer",  Static).update(self._build_footer())

    # ── Renderers ─────────────────────────────────────────────────────────────

    def _build_header(self) -> Text:
        t = Text()
        t.append("  Load Session", style=f"bold {_GOLD}")
        if self._sessions:
            t.append(f"  {len(self._sessions)} session{'s' if len(self._sessions) != 1 else ''}",
                     style=_DIM)
        if self._query:
            t.append(f"  ·  ", style=_FAINT)
            t.append(f"{len(self._visible)} match{'es' if len(self._visible) != 1 else ''}",
                     style=_ORANGE if not self._visible else _DIM)
        return t

    def _build_list(self) -> Text:
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

        t = Text()
        for i, s in enumerate(self._visible):
            is_sel = (i == self._selected)
            is_del = (is_sel and self._mode == "confirm_delete")

            if is_del:
                bg = _TINT_RED
                name_style = f"bold {_RED}"
                meta_style = _DIM
                ptr = "  ✕  "
                ptr_style = f"bold {_RED}"
            elif is_sel:
                bg = _TINT_YEL
                name_style = f"bold {_GOLD}"
                meta_style = _DIM
                ptr = "  ▸  "
                ptr_style = f"bold {_GOLD}"
            else:
                bg = ""
                name_style = _TEXT
                meta_style = _FAINT
                ptr = "     "
                ptr_style = _FAINT

            line = Text(no_wrap=True, overflow="ellipsis", end="\n")
            line.append(ptr, style=ptr_style)

            # Session name (with search highlight)
            if self._query and self._query.lower() in s.name.lower():
                lower = s.name.lower()
                q = self._query.lower()
                pos = 0
                while True:
                    idx = lower.find(q, pos)
                    if idx == -1:
                        line.append(s.name[pos:], style=name_style)
                        break
                    line.append(s.name[pos:idx], style=name_style)
                    line.append(s.name[idx:idx + len(q)], style=f"bold {_ORANGE}")
                    pos = idx + len(q)
            else:
                line.append(s.name, style=name_style)

            # Compact metadata
            msgs = f"{s.message_count} msg{'s' if s.message_count != 1 else ''}"
            tok  = f"{s.total_tokens / 1000:.1f}k tok" if s.total_tokens >= 1000 else f"{s.total_tokens} tok"
            mod  = _model_short(s.model)
            age  = _age(s.saved_at)
            line.append(f"  ·  {msgs}  {tok}  {mod}  {age}", style=meta_style)

            if bg:
                line.stylize(f"on {bg}")
            t.append_text(line)

        return t

    def _build_preview(self) -> RenderableType:
        if self._mode == "empty" or not self._visible:
            t = Text()
            t.append("\n  Select a session to preview.", style=_FAINT)
            return t

        s = self._visible[self._selected]

        if self._mode == "confirm_delete":
            t = Text()
            t.append("\n  Delete this session?\n\n", style=f"bold {_RED}")
            t.append(f"  {s.name}\n", style=f"bold {_TEXT}")
            t.append(f"\n  This cannot be undone.\n\n", style=_DIM)
            t.append("  Press ", style=_FAINT)
            t.append(" d ", style=f"bold {_RED} on #2a0000")
            t.append(" again to confirm, or ", style=_FAINT)
            t.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
            t.append(" to cancel.", style=_FAINT)
            return t

        # Normal detail view
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style=_DIM,  no_wrap=True, min_width=10)
        tbl.add_column(style=_TEXT, no_wrap=True)

        # Format saved_at nicely
        saved_fmt = s.saved_at
        try:
            dt = datetime.fromisoformat(s.saved_at)
            saved_fmt = dt.strftime("%Y-%m-%d  %H:%M")
        except (ValueError, AttributeError):
            pass

        tbl.add_row("", "")
        tbl.add_row(Text(s.name, style=f"bold {_GOLD}"), Text(""))
        tbl.add_row(Text("─" * 30, style=_RULE), Text(""))
        tbl.add_row("Saved",    saved_fmt or "—")
        tbl.add_row("Age",      _age(s.saved_at))
        tbl.add_row("Model",    s.model or "—")
        tbl.add_row("Messages", str(s.message_count))
        tbl.add_row("Tokens",   f"{s.total_tokens:,}" if s.total_tokens else "0")

        # Message previews
        if s.first_user_msg:
            tbl.add_row("", "")
            tbl.add_row(Text("── first message", style=_FAINT), Text(""))
            preview = s.first_user_msg.replace("\n", " ")
            tbl.add_row("", Text(preview, style=_DIM, overflow="fold"))

        if s.last_user_msg:
            tbl.add_row("", "")
            tbl.add_row(Text("── last message", style=_FAINT), Text(""))
            preview = s.last_user_msg.replace("\n", " ")
            tbl.add_row("", Text(preview, style=_DIM, overflow="fold"))

        return tbl

    def _build_footer(self) -> Text:
        t = Text()
        t.append("  ")
        if self._mode == "confirm_delete":
            t.append_text(_keycap("d"))
            t.append(" confirm delete", style=_DIM)
            t.append("  ·  ", style=_FAINT)
            t.append_text(_keycap("esc"))
            t.append(" cancel", style=_DIM)
        elif self._mode == "empty":
            t.append_text(_keycap("esc"))
            t.append(" dismiss", style=_DIM)
        else:
            t.append_text(_keycap("↑↓"))
            t.append(" navigate", style=_DIM)
            t.append("  ·  ", style=_FAINT)
            t.append_text(_keycap("↵"))
            t.append(" load", style=_DIM)
            t.append("  ·  ", style=_FAINT)
            t.append_text(_keycap("d"))
            t.append(" delete", style=_DIM)
            t.append("  ·  ", style=_FAINT)
            t.append(" type to filter ", style=_DIM)
            t.append("  ·  ", style=_FAINT)
            t.append_text(_keycap("esc"))
            t.append(" dismiss", style=_DIM)
        return t

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
            # Any printable char (not already handled) → jump to search bar
            search = self.query_one("#ld-search", ModalSearchBar)
            inp = search.query_one(Input)
            inp.focus()
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
