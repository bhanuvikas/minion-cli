"""MemoriesScreen — /memories modal for the Textual TUI.

Seven visual states:
  browse          — full list, preview of focused record, action chips.
  search          — live-filter list as user types; highlights keyword matches.
  search_empty    — no matches found; ascii doodle + hints.
  edit            — TextArea replaces preview body; ↵ saves, esc cancels.
  confirm_delete  — inline confirm strip in preview; focused row gets orange tint.
  confirm_delete_all — full overlay card with type-to-confirm input.
  empty           — no memories at all; ascii doodle + /remember example.

Layered esc:
  confirm/edit mode → back to browse
  query active    → clear query → full list
  otherwise       → dismiss modal
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea

from .base import ModalSearchBar

if TYPE_CHECKING:
    from ...memory.store import MemoryStore
    from ...memory.record import MemoryRecord

# ── Color tokens ──────────────────────────────────────────────────────────────

_GOLD     = "#e5c46b"    # active, focused, primary yellow
_GOLD_DIM = "#b8a030"    # dim yellow for labels / section headers
_CYAN     = "#5cc8ff"    # memory IDs
_GREEN    = "#6ed98e"    # project scope badge
_ORANGE   = "#d97757"    # danger, delete, selected row
_DIM      = "#7a7464"    # secondary text
_FAINT    = "#4a4639"    # very faint tertiary / ascii art
_RULE     = "#2a2820"    # borders
_TEXT     = "#d8cfb8"    # primary text
_TINT_YEL = "#1a1400"    # faint yellow tint background (focused row)
_TINT_ORG = "#1a0800"    # faint orange tint background (danger row)
_SILVER   = "#c0c0c0"    # keycap hint color


def _age(iso: str) -> str:
    """Return a human-readable relative age string from an ISO 8601 timestamp."""
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
            m = int(secs / 60)
            return f"{m}m ago"
        if secs < 86400:
            h = int(secs / 3600)
            return f"{h}h ago"
        d = int(secs / 86400)
        return f"{d}d ago"
    except (ValueError, OverflowError):
        return "unknown"


def _scope_color(scope: str) -> str:
    return _GREEN if scope == "project" else _GOLD_DIM


def _highlight(text: str, query: str) -> Text:
    """Return a Rich Text with all occurrences of query highlighted in gold."""
    if not query:
        return Text(text, style=_TEXT)
    t = Text()
    lower_text = text.lower()
    lower_q = query.lower()
    pos = 0
    while True:
        idx = lower_text.find(lower_q, pos)
        if idx == -1:
            t.append(text[pos:], style=_TEXT)
            break
        t.append(text[pos:idx], style=_TEXT)
        t.append(text[idx:idx + len(query)], style=f"bold {_GOLD}")
        pos = idx + len(query)
    return t


def _pill(label: str, active: bool = False, danger: bool = False) -> Text:
    """Render a small inline chip."""
    if danger:
        return Text(f" {label} ", style=f"bold {_ORANGE} on #2a0e06")
    if active:
        return Text(f" {label} ", style=f"bold {_GOLD} on #1a1200")
    return Text(f" {label} ", style=f"{_DIM} on #161614")


def _keycap(key: str) -> Text:
    return Text(f" {key} ", style=f"bold {_SILVER} on #2a2a2a")


class MemoriesScreen(ModalScreen):  # type: ignore[type-arg]
    """Full-screen split-pane memory browser opened by /memories."""

    CSS = f"""
MemoriesScreen {{
    align: center middle;
    background: #000000 40%;
}}
#mem-panel {{
    width: 90%;
    height: 90%;
    background: #0d0d0d;
    border: round {_RULE};
}}
#mem-header {{
    height: auto;
    padding: 0 2;
    border-bottom: solid {_RULE};
}}
#mem-body {{
    height: 1fr;
}}
#mem-list-pane {{
    width: 60%;
    border-right: solid {_RULE};
}}
#mem-list-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-color: #2a2a2a;
}}
#mem-list {{
    height: auto;
}}
#mem-preview-pane {{
    width: 40%;
    padding: 0 1;
}}
#mem-preview {{
    height: auto;
}}
#mem-edit-area {{
    height: 1fr;
    display: none;
    border: solid {_GOLD};
    background: #0d0d0d;
    color: {_TEXT};
    padding: 0 1;
}}
#mem-edit-area:focus {{
    border: solid {_GOLD};
}}
#mem-footer {{
    height: 2;
    padding: 0 2;
    background: #0d0d0d;
    border-top: solid {_RULE};
}}
#mem-list-dim {{
    display: none;
}}
"""

    BINDINGS = [
        Binding("escape", "esc_action",  show=False, priority=True),
        Binding("up",     "nav_up",      show=False, priority=True),
        Binding("down",   "nav_down",    show=False, priority=True),
        Binding("enter",  "confirm",     show=False, priority=True),
        Binding("tab",    "toggle_pane", show=False, priority=True),
    ]

    _DEL_ALL_OPTIONS: list[tuple[str, str]] = [
        ("All memories",  "all"),
        ("Global only",   "global"),
        ("Project only",  "project"),
    ]

    def __init__(
        self,
        memory_store: "Optional[MemoryStore]" = None,
        initial_query: str = "",
    ) -> None:
        super().__init__()
        self._store = memory_store
        self._initial_query = initial_query
        self._mode: str = "browse"
        self._scope: str = "all"
        self._query: str = ""
        self._records: list["MemoryRecord"] = []
        self._selected: int = 0
        self._edit_original: str = ""
        self._del_all_selected: int = 0
        self._del_all_confirmed: bool = False
        self._del_all_timer: object | None = None
        self._undo_record: Optional["MemoryRecord"] = None
        self._undo_expired: bool = True
        self._focus_list: bool = True  # True = list focused, False = preview

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="mem-panel"):
            yield Static("", id="mem-header")
            with Horizontal(id="mem-body"):
                with Vertical(id="mem-list-pane"):
                    yield ModalSearchBar(placeholder="search memories…", id="mem-search")
                    with VerticalScroll(id="mem-list-scroll"):
                        yield Static("", id="mem-list")
                    yield Static("", id="mem-list-dim")
                with Vertical(id="mem-preview-pane"):
                    yield Static("", id="mem-preview")
                    yield TextArea("", id="mem-edit-area")
            yield Static("", id="mem-footer")

    def on_mount(self) -> None:
        self._reload_records()
        if self._initial_query:
            search = self.query_one("#mem-search", ModalSearchBar)
            search.query_one(Input).value = self._initial_query
            self._query = self._initial_query
            self._mode = "search"
            self._reload_records()
        panel = self.query_one("#mem-panel", Vertical)
        panel.can_focus = True
        panel.focus()
        self.query_one("#mem-edit-area", TextArea).display = False
        self._refresh()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._query = event.value
        self._mode = "search" if event.value else "browse"
        self._reload_records()
        self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#mem-panel", Vertical).focus()
        self._mode = "browse" if not self._query else "search"
        self._refresh()

    # ── Data ──────────────────────────────────────────────────────────────────

    def _reload_records(self) -> None:
        if self._store is None:
            self._records = []
            return
        if self._query and len(self._query) < 3:
            # Store's keyword search requires ≥3-char tokens; do local substring match instead.
            all_active = self._store.list_all()
            q = self._query.lower()
            all_active = [r for r in all_active if q in r.content.lower()]
        else:
            all_active = self._store.list_all(query=self._query or None)
        if self._scope != "all":
            all_active = [r for r in all_active if r.scope == self._scope]
        pinned = [r for r in all_active if r.pinned]
        rest   = [r for r in all_active if not r.pinned]
        self._records = pinned + rest
        self._selected = min(self._selected, max(0, len(self._records) - 1))

    def _current_record(self) -> "Optional[MemoryRecord]":
        if not self._records:
            return None
        return self._records[self._selected]

    def _stats(self) -> tuple[int, int]:
        """Return (global_count, project_count) from unfiltered store."""
        if self._store is None:
            return 0, 0
        s = self._store.stats()
        return s.get("global_count", 0), s.get("project_count", 0)

    def _del_all_scope_count(self, scope: str) -> int:
        g, p = self._stats()
        return g + p if scope == "all" else (g if scope == "global" else p)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self.query_one("#mem-header", Static).update(self._build_header())

        if self._mode == "confirm_delete_all":
            scope = self._DEL_ALL_OPTIONS[self._del_all_selected][1]
            all_recs = self._store.list_all() if self._store else []
            pending = [r for r in all_recs if scope == "all" or r.scope == scope]
            self.query_one("#mem-list", Static).update(self._build_list_delete_pending(pending))
        elif not self._records and self._query:
            self.query_one("#mem-list", Static).update(self._build_search_empty())
        elif not self._records:
            self.query_one("#mem-list", Static).update(self._build_list_empty())
        else:
            self.query_one("#mem-list", Static).update(self._build_list())

        self.query_one("#mem-preview", Static).update(self._build_preview())
        self.query_one("#mem-footer", Static).update(self._build_footer())

        edit_area = self.query_one("#mem-edit-area", TextArea)
        edit_area.display = (self._mode == "edit")

        self.query_one("#mem-list-dim", Static).display = False

    # ── Builders ──────────────────────────────────────────────────────────────

    def _build_header(self) -> Text:
        t = Text()
        t.append("┌─ ", style=_FAINT)
        t.append("/memories", style="bold")
        t.append(" — browse & manage", style=_DIM)
        t.append("  ")

        g_count, p_count = self._stats()
        t.append(f" {g_count} global ", style=f"{_GOLD_DIM} on #161614")
        t.append("·", style=_FAINT)
        t.append(f" {p_count} project ", style=f"{_GREEN} on #0a1208")

        if self._mode == "edit":
            rec = self._current_record()
            rid = rec.id[:8] if rec else "?"
            t.append(f"  editing · {rid} ", style=f"bold {_GOLD} on #1a1200")
        elif self._mode == "confirm_delete":
            t.append("  delete pending ", style=f"bold {_ORANGE} on #1a0800")
        elif self._mode == "confirm_delete_all":
            t.append(" delete all ", style=f"bold {_ORANGE} on #2a0e06")
        elif self._query:
            count = len(self._records)
            t.append(f"  {count} match{'es' if count != 1 else ''} ", style=f"{_GOLD} on #1a1200")

        return t

    def _build_list(self) -> Table:
        outer = Table.grid(expand=True, padding=0)
        outer.add_column()

        # Multi-column table for memory rows — gives true column alignment.
        # Columns: ptr+pin | ID | scope | content (fills) | age
        mem = Table.grid(expand=True, padding=0)
        mem.add_column(width=5,  no_wrap=True)   # " › ★ "
        mem.add_column(width=10, no_wrap=True)   # 8-char ID + 2sp
        mem.add_column(width=10, no_wrap=True)   # "project" (7) + 3sp
        mem.add_column(ratio=1)                   # content — fills remaining
        mem.add_column(width=10, no_wrap=True)   # age, right-aligned
        mem.add_column(width=2,  no_wrap=True)   # right-edge padding

        for i, rec in enumerate(self._records):
            is_focused = (i == self._selected)
            is_danger  = (self._mode == "confirm_delete" and is_focused)
            row_style  = (f"on {_TINT_ORG}" if is_danger
                          else f"on {_TINT_YEL}" if is_focused else "")

            # pointer + pin
            ptr = Text(no_wrap=True)
            if is_focused:
                ptr.append(" › " if self._focus_list else "   ",
                           style=f"bold {_ORANGE if is_danger else _GOLD}")
            else:
                ptr.append("   ")
            ptr.append("★" if rec.pinned else " ",
                       style=_GOLD if rec.pinned else _FAINT)
            ptr.append(" ")

            # ID
            id_t = Text(rec.id[:8],
                        style=f"strike {_ORANGE}" if is_danger else _CYAN,
                        no_wrap=True)

            # scope
            scope_t = Text(rec.scope,
                           style=f"bold {_scope_color(rec.scope)}",
                           no_wrap=True)

            # content
            preview = rec.content.replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:117] + "…"
            if is_danger:
                content_t = Text(preview, style=f"strike {_DIM}")
            elif self._query:
                content_t = _highlight(preview, self._query)
            else:
                content_t = Text(preview, style=_TEXT if is_focused else _DIM)

            # age
            age_t = Text(_age(rec.created_at), style=_FAINT, justify="right",
                         no_wrap=True)

            mem.add_row(ptr, id_t, scope_t, content_t, age_t, Text(""), style=row_style)

        outer.add_row(mem)

        # Undo toast
        if self._undo_record and not self._undo_expired:
            outer.add_row(Text(""))
            undo_line = Text()
            undo_line.append("  deleted · ", style=_DIM)
            undo_line.append(" u ", style=f"bold {_SILVER} on #2a2a2a")
            undo_line.append(" undo (4s)", style=_DIM)
            outer.add_row(undo_line)

        return outer

    def _build_list_delete_pending(self, records: list) -> Table:
        outer = Table.grid(expand=True, padding=0)
        outer.add_column()

        n = len(records)
        hdr_line = Text()
        hdr_line.append(f"  ABOUT TO DELETE · {n} {'MEMORY' if n == 1 else 'MEMORIES'}",
                        style=f"bold {_ORANGE}")
        outer.add_row(hdr_line)

        if not records:
            outer.add_row(Text("  nothing in this scope", style=_FAINT))
            return outer

        mem = Table.grid(expand=True, padding=0)
        mem.add_column(width=5,  no_wrap=True)
        mem.add_column(width=10, no_wrap=True)
        mem.add_column(width=10, no_wrap=True)
        mem.add_column(ratio=1)
        mem.add_column(width=10, no_wrap=True)
        mem.add_column(width=2,  no_wrap=True)

        for rec in records:
            ptr = Text()
            ptr.append("  × ", style=f"bold {_ORANGE}")
            ptr.append(" ")

            id_t = Text(no_wrap=True)
            id_t.append(rec.id[:8], style=f"strike {_ORANGE}")
            id_t.append("  ")  # plain trailing spaces — no strikethrough bleed
            scope_t = Text(rec.scope, style=f"bold {_scope_color(rec.scope)}", no_wrap=True)

            preview = rec.content.replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:117] + "…"
            content_t = Text(preview, style=f"strike {_DIM}")

            age_t = Text(_age(rec.created_at), style=_FAINT, justify="right", no_wrap=True)
            mem.add_row(ptr, id_t, scope_t, content_t, age_t, Text(""), style=f"on {_TINT_ORG}")

        outer.add_row(mem)
        return outer

    def _build_search_empty(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        tbl.add_row(Text(""))
        art = Text()
        art.append(f'  no memories match "{self._query}"\n', style=_DIM)
        art.append("  try a different term or", style=_FAINT)
        art.append(" esc", style=f"bold {_SILVER}")
        art.append(" to clear", style=_FAINT)
        tbl.add_row(art)

        return tbl

    def _build_list_empty(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(""))
        art = Text()
        art.append("       ___\n", style=_FAINT)
        art.append("      /   \\\n", style=_FAINT)
        art.append("     |  ·  |   no memories yet\n", style=_FAINT)
        art.append("      \\___/    minion forgets between\n", style=_FAINT)
        art.append("               sessions unless you let\n", style=_FAINT)
        art.append("               it remember\n", style=_FAINT)
        tbl.add_row(art)
        return tbl

    def _build_preview(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        if self._mode == "confirm_delete_all":
            return self._build_del_all_top()

        rec = self._current_record()
        if rec is None:
            if not self._records:
                helper = Text()
                helper.append("\n  Add a memory with:\n\n", style=_DIM)
                helper.append("  /remember ", style=f"bold {_GOLD}")
                helper.append('"use ruff for linting"\n', style=_TEXT)
                helper.append("  /remember --global ", style=f"bold {_GOLD}")
                helper.append('"prefer TypeScript"\n', style=_TEXT)
                tbl.add_row(helper)
            return tbl

        # ── Header row: ID · scope · age ──────────────────────────────────────
        header = Text()
        header.append(" ")
        header.append(rec.id[:8], style=f"bold {_CYAN}")
        header.append("  ")
        header.append(rec.scope, style=f"bold {_scope_color(rec.scope)}")
        header.append("  ")
        header.append(_age(rec.created_at), style=_FAINT)
        tbl.add_row(header)
        tbl.add_row(Text(""))

        # ── Content block ─────────────────────────────────────────────────────
        # Nested two-column table: 1-char indent + content. This ensures
        # Rich wraps the content column so every continuation line starts at
        # the same column as the first line (not at col 0).
        if self._mode != "edit":
            content_style = _DIM if self._mode == "confirm_delete" else _TEXT
            content_tbl = Table.grid(expand=True, padding=0)
            content_tbl.add_column(width=1, no_wrap=True)
            content_tbl.add_column(ratio=1)
            content_tbl.add_row(Text(""), Text(rec.content, style=content_style))
            tbl.add_row(content_tbl)
            tbl.add_row(Text(""))

        # ── Metadata: two-column table (label fixed, value fills) ─────────────
        meta = Table.grid(expand=True, padding=0)
        meta.add_column(width=12, no_wrap=True)   # " type      " (label)
        meta.add_column(ratio=1)                   # value

        meta.add_row(
            Text(" type", style=_FAINT),
            Text(rec.type, style=_DIM),
        )
        meta.add_row(
            Text(" category", style=_FAINT),
            Text(rec.category, style=_DIM),
        )
        if rec.tags:
            meta.add_row(
                Text(" tags", style=_FAINT),
                Text(", ".join(rec.tags), style=_DIM),
            )
        if rec.pinned:
            meta.add_row(
                Text(" pinned", style=_FAINT),
                Text("★ yes", style=f"bold {_GOLD}"),
            )
        tbl.add_row(meta)

        # confirm_delete shows an inline strip since it replaces the preview body
        if self._mode == "confirm_delete":
            tbl.add_row(Text(""))
            tbl.add_row(Rule(style=_RULE))
            confirm = Text()
            confirm.append(" delete this memory?  ", style=f"bold {_ORANGE}")
            confirm.append(" ↵ ", style=f"bold {_SILVER} on {_ORANGE}")
            confirm.append(" delete  ", style=_DIM)
            confirm.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
            confirm.append(" keep", style=_DIM)
            tbl.add_row(confirm, style=f"on {_TINT_ORG}")
            tbl.add_row(Rule(style=_RULE))

        return tbl

    def _build_footer(self) -> Text:
        dot = f" [{_FAINT}]·[/] "

        def _hint(key: str, label: str) -> str:
            return f"[bold {_SILVER} on #2a2a2a] {key} [/] [{_DIM}]{label}[/]"

        if self._mode == "search":
            hints = [_hint("↑↓", "nav"), _hint("⌫", "erase"), _hint("esc", "back")]
        elif self._mode == "edit":
            hints = [_hint("↵", "save"), _hint("esc", "cancel"), _hint("ctrl+z", "revert")]
        elif self._mode == "confirm_delete":
            hints = [_hint("↵", "confirm delete"), _hint("esc", "keep")]
        elif self._mode == "confirm_delete_all":
            hints = [
                _hint("↑↓", "select scope"),
                _hint("↵", "delete (press twice)"),
                _hint("esc", "cancel"),
            ]
        else:
            hints = [_hint("↑↓", "nav"), _hint("/", "search"), _hint("e", "edit"),
                     _hint("p", "pin"), _hint("y", "copy text"), _hint("m", "move"),
                     _hint("d", "delete"), _hint("D", "delete all"), _hint("esc", "close")]
            if self._undo_record and not self._undo_expired:
                hints.append(_hint("u", "undo"))

        return Text.from_markup("  " + dot.join(hints))

    def _build_del_all_top(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        # ── Headline ──────────────────────────────────────────────────────────
        hdr = Table.grid(expand=True, padding=0)
        hdr.add_column(ratio=1)
        hdr.add_column(no_wrap=True)
        title = Text()
        title.append("⚠  ", style=_ORANGE)
        title.append("Clear memory store", style=f"bold {_ORANGE}")
        warning = Text(" irreversible · no backup ", style=f"{_DIM} on #1a1a1a")
        hdr.add_row(title, warning)
        tbl.add_row(hdr)
        tbl.add_row(Text(""))

        desc = Text()
        desc.append("Global", style=f"bold {_GOLD_DIM}")
        desc.append(" memories affect every project on this machine;\n", style=_DIM)
        desc.append("Project", style=f"bold {_GREEN}")
        desc.append(" memories only affect this project.", style=_DIM)
        tbl.add_row(desc)
        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))

        # ── SCOPE label ───────────────────────────────────────────────────────
        tbl.add_row(Text("  SCOPE", style=_FAINT))
        tbl.add_row(Text(""))

        # ── Selector rows ─────────────────────────────────────────────────────
        _SUBLINES = {
            "all":     "everything across both scopes",
            "global":  "~/.minion/memory/global.jsonl",
            "project": ".minion/memory/project.jsonl",
        }
        _SCOPE_COLORS = {"all": _ORANGE, "global": _GOLD_DIM, "project": _GREEN}

        for i, (label, scope) in enumerate(self._DEL_ALL_OPTIONS):
            active   = i == self._del_all_selected
            n        = self._del_all_scope_count(scope)
            disabled = (n == 0 and scope != "all")
            color    = _SCOPE_COLORS[scope]
            row_style = f"on {_TINT_ORG}" if active else ""

            ptr = Text(" › " if active else "   ",
                       style=f"bold {_ORANGE}" if active else "")

            subline = _SUBLINES[scope]
            if disabled:
                subline += " · nothing to delete"
            lbl_block = Text()
            lbl_block.append(
                label + "\n",
                style=(_FAINT if disabled else (f"bold {color}" if active else _TEXT)),
            )
            lbl_block.append(subline, style=_FAINT)

            if disabled:
                cnt = Text("0", style=_FAINT, no_wrap=True)
            elif active:
                cnt = Text(str(n), style=f"bold {color}", no_wrap=True)
            else:
                cnt = Text(str(n), style=color, no_wrap=True)

            row_tbl = Table.grid(expand=True, padding=0)
            row_tbl.add_column(width=3, no_wrap=True)
            row_tbl.add_column(ratio=1)
            row_tbl.add_column(width=6, no_wrap=True)
            row_tbl.add_row(ptr, lbl_block, cnt)
            tbl.add_row(row_tbl, style=row_style)

            if i < len(self._DEL_ALL_OPTIONS) - 1:
                tbl.add_row(Text(""))

        # ── Two-press confirm strip ───────────────────────────────────────────
        if self._del_all_confirmed:
            _, scope = self._DEL_ALL_OPTIONS[self._del_all_selected]
            n = self._del_all_scope_count(scope)
            noun = "memory" if n == 1 else "memories"
            tbl.add_row(Text(""))
            tbl.add_row(Rule(style=_RULE))
            confirm = Text()
            confirm.append(f" delete {n} {noun}?  ", style=f"bold {_ORANGE}")
            confirm.append(" ↵ ", style=f"bold {_SILVER} on {_ORANGE}")
            confirm.append(" delete  ", style=_DIM)
            confirm.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
            confirm.append(" cancel", style=_DIM)
            tbl.add_row(confirm, style=f"on {_TINT_ORG}")
            tbl.add_row(Rule(style=_RULE))

        return tbl

    # ── Key handling ──────────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple) -> bool | None:  # type: ignore[override]
        if action in ("nav_up", "nav_down") and self._mode == "edit":
            return False
        return True

    def _cancel_del_all_confirm_immediate(self) -> None:
        """Cancel a pending two-press confirm without waiting for the timer."""
        if self._del_all_confirmed:
            self._del_all_confirmed = False
            if self._del_all_timer:
                self._del_all_timer.cancel()  # type: ignore[union-attr]
                self._del_all_timer = None

    def action_nav_up(self) -> None:
        if self._mode == "confirm_delete_all":
            self._cancel_del_all_confirm_immediate()
            idx = self._del_all_selected - 1
            while idx >= 0:
                _, scope = self._DEL_ALL_OPTIONS[idx]
                if scope == "all" or self._del_all_scope_count(scope) > 0:
                    break
                idx -= 1
            if idx >= 0:
                self._del_all_selected = idx
            self._refresh()
        elif self._records:
            self._selected = max(0, self._selected - 1)
            self._refresh()

    def action_nav_down(self) -> None:
        if self._mode == "confirm_delete_all":
            self._cancel_del_all_confirm_immediate()
            idx = self._del_all_selected + 1
            while idx < len(self._DEL_ALL_OPTIONS):
                _, scope = self._DEL_ALL_OPTIONS[idx]
                if scope == "all" or self._del_all_scope_count(scope) > 0:
                    break
                idx += 1
            if idx < len(self._DEL_ALL_OPTIONS):
                self._del_all_selected = idx
            self._refresh()
        elif self._records:
            self._selected = min(len(self._records) - 1, self._selected + 1)
            self._refresh()

    def action_esc_action(self) -> None:
        if self._mode == "confirm_delete_all":
            self._cancel_del_all_confirm_immediate()
            self._mode = "browse"
            self._del_all_selected = 0
            self.query_one("#mem-panel", Vertical).focus()
            self._refresh()
        elif self._mode in ("edit", "confirm_delete"):
            self._mode = "browse"
            self.query_one("#mem-edit-area", TextArea).display = False
            self.query_one("#mem-panel", Vertical).focus()
            self._refresh()
        elif self._mode == "search" or self._query:
            self.query_one("#mem-search", ModalSearchBar).clear()
            self._query = ""
            self._mode = "browse"
            self._reload_records()
            self.query_one("#mem-panel", Vertical).focus()
            self._refresh()
        else:
            self.dismiss(None)

    def action_confirm(self) -> None:
        if self._mode == "edit":
            self._save_edit()
        elif self._mode == "confirm_delete":
            self._do_delete()
        elif self._mode == "confirm_delete_all":
            if not self._del_all_confirmed:
                self._del_all_confirmed = True
                self._refresh()
            else:
                _, scope = self._DEL_ALL_OPTIONS[self._del_all_selected]
                self._delete_scope(scope)

    def action_toggle_pane(self) -> None:
        self._focus_list = not self._focus_list
        if not self._focus_list and self._mode == "edit":
            self.query_one("#mem-edit-area", TextArea).focus()
        else:
            self.query_one("#mem-panel", Vertical).focus()
        self._refresh()

    def on_key(self, event: Key) -> None:
        key = event.key
        mode = self._mode

        if mode == "confirm_delete_all":
            return  # all keys handled by BINDINGS

        if mode == "edit":
            if key == "ctrl+z":
                rec = self._current_record()
                if rec:
                    self.query_one("#mem-edit-area", TextArea).load_text(rec.content)
                event.stop()
            return

        # Browse mode key dispatch
        if key == "slash":
            self.query_one("#mem-search", ModalSearchBar).focus_input()
            self._mode = "search"
            self._refresh()
            event.stop()
        elif key == "e":
            self._start_edit()
            event.stop()
        elif key == "p":
            self._toggle_pin()
            event.stop()
        elif key == "y":
            self._copy_id()
            event.stop()
        elif key == "m":
            self._move_scope()
            event.stop()
        elif key == "d":
            self._start_delete_confirm()
            event.stop()
        elif key == "D":
            self._mode = "confirm_delete_all"
            self._refresh()
            event.stop()
        elif key == "u" and not self._undo_expired and self._undo_record:
            self._do_undo()
            event.stop()

    # ── Disk operations ───────────────────────────────────────────────────────

    def _start_edit(self) -> None:
        rec = self._current_record()
        if rec is None or self._store is None:
            return
        self._edit_original = rec.content
        self._mode = "edit"
        edit_area = self.query_one("#mem-edit-area", TextArea)
        edit_area.load_text(rec.content)
        edit_area.display = True
        self._focus_list = False
        edit_area.focus()
        self._refresh()

    def _save_edit(self) -> None:
        rec = self._current_record()
        if rec is None or self._store is None:
            return
        new_text = self.query_one("#mem-edit-area", TextArea).text.strip()
        if new_text and new_text != rec.content:
            rec.content = new_text
            self._store.store(rec)
        self._mode = "browse"
        self.query_one("#mem-edit-area", TextArea).display = False
        self._reload_records()
        self._refresh()

    def _start_delete_confirm(self) -> None:
        if self._current_record() is None:
            return
        self._mode = "confirm_delete"
        self._refresh()

    def _do_delete(self) -> None:
        rec = self._current_record()
        if rec is None or self._store is None:
            return
        self._undo_record = rec
        self._undo_expired = False
        self._store.delete(rec.id)
        self.set_timer(4.0, self._expire_undo)
        self._mode = "browse"
        self._reload_records()
        self._refresh()

    def _do_undo(self) -> None:
        if self._undo_record and not self._undo_expired and self._store:
            self._store.store(self._undo_record)
            self._undo_record = None
            self._undo_expired = True
            self._reload_records()
            self._refresh()

    def _expire_undo(self) -> None:
        self._undo_expired = True
        self._undo_record = None
        self._refresh()

    def _cancel_del_all_confirm(self) -> None:
        self._del_all_confirmed = False
        self._del_all_timer = None
        self._refresh()

    def _delete_scope(self, which: str) -> None:
        if self._store is None:
            return
        records = self._store.list_all()
        if which != "all":
            records = [r for r in records if r.scope == which]
        for r in records:
            self._store.delete(r.id)
        self._del_all_confirmed = False
        if self._del_all_timer:
            self._del_all_timer.cancel()  # type: ignore[union-attr]
            self._del_all_timer = None
        self._mode = "browse"
        self._del_all_selected = 0
        self._reload_records()
        self._refresh()

    def _toggle_pin(self) -> None:
        rec = self._current_record()
        if rec is None or self._store is None:
            return
        rec.pinned = not rec.pinned
        self._store.store(rec)
        self._reload_records()
        self._refresh()

    def _move_scope(self) -> None:
        rec = self._current_record()
        if rec is None or self._store is None:
            return
        new_scope = "project" if rec.scope == "global" else "global"
        self._store.delete(rec.id)
        rec.scope = new_scope
        rec.project_path = (
            str(self._store._project_dir.parent.parent) if new_scope == "project" else None
        )
        self._store.store(rec)
        self._reload_records()
        self._refresh()

    def _copy_id(self) -> None:
        rec = self._current_record()
        if rec is None:
            return
        try:
            import subprocess
            subprocess.run(["pbcopy"], input=rec.content.encode(), check=False)
        except Exception:
            pass
