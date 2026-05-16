"""AgentsScreen — /agents modal for the Textual TUI.

States:
  browse          — full list, detail of focused agent, action chips.
  search          — live-filter list as user types; highlights keyword matches.
  detail          — right pane focused (↵ from browse moves focus there).
  confirm_delete  — inline confirm strip; second d executes deletion.
  duplicate       — duplicate form in right pane; name input + tier selector.

Layered esc:
  confirm_delete  → back to browse (no deletion)
  duplicate       → back to browse (no file created)
  search active   → clear query → full list
  otherwise       → dismiss modal
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml

from rich.panel import Panel
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

if TYPE_CHECKING:
    from ...agents.manifest import AgentRoleManifest

# ── Color tokens ──────────────────────────────────────────────────────────────

_ORANGE   = "#d97757"    # selected row, delete, warnings
_GOLD     = "#c8a84b"    # user-tier color
_GOLD_DIM = "#b8a030"    # dim gold for labels
_GREEN    = "#7ec8a0"    # project-tier color
_BLUE     = "#6aa3d4"    # model names, accents
_SILVER   = "#bbbbbb"    # body text, keycaps
_DIM      = "#888888"    # secondary text
_FAINT    = "#555555"    # tertiary, section labels
_RULE     = "#2a2820"    # border lines
_TEXT     = "#d8cfb8"    # primary warm-white text
_TINT_ORG = "#1a0800"    # faint orange background (selected/danger rows)
_BG       = "#0d0d0d"    # panel background

# ── Tool data ─────────────────────────────────────────────────────────────────

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

_NATIVE_TOOLS = list(_TOOL_DESCRIPTIONS.keys())

_TIER_ORDER: dict[str, int] = {"builtin": 0, "user": 1, "project": 2}

# ── Helpers ───────────────────────────────────────────────────────────────────


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


def _format_source_path(manifest: "AgentRoleManifest") -> str:
    """Format the source path for display, collapsing home dir."""
    if manifest.source_path is None:
        return f"minion://builtin/agents/{manifest.name}.yaml"
    if manifest.source == "builtin":
        return f"minion://builtin/agents/{manifest.source_path.name}"
    try:
        return "~/" + str(manifest.source_path.relative_to(Path.home()))
    except ValueError:
        return str(manifest.source_path)


def _hint(key: str, label: str) -> str:
    return f"[bold {_SILVER} on #2a2a2a] {key} [/] [{_DIM}]{label}[/]"


# ── Screen ────────────────────────────────────────────────────────────────────


class AgentsScreen(ModalScreen):  # type: ignore[type-arg]
    """Full-screen split-pane agent browser opened by /agents."""

    CSS = f"""
AgentsScreen {{
    align: center middle;
    background: #000000 40%;
}}
#ag-panel {{
    width: 90%;
    height: 90%;
    background: {_BG};
    border: round {_RULE};
}}
#ag-header {{
    height: auto;
    padding: 0 2;
    border-bottom: solid {_RULE};
}}
#ag-body {{
    height: 1fr;
}}
#ag-list-pane {{
    width: 60%;
    border-right: solid {_RULE};
}}
#ag-list-pane.lhs-focused {{
    border-right: solid {_ORANGE};
}}
#ag-list-pane.rhs-focused {{
    border-right: solid {_BLUE};
}}
#ag-list-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {_DIM};
}}
#ag-list {{
    height: auto;
}}
#ag-preview-pane {{
    width: 40%;
    padding: 0 1;
}}
#ag-preview-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {_DIM};
}}
#ag-preview {{
    height: auto;
}}
#ag-dup-name {{
    height: auto;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    padding: 0 1;
}}
#ag-dup-name:focus {{
    border: solid {_ORANGE};
}}
#ag-footer {{
    height: 2;
    padding: 0 2;
    background: {_BG};
    border-top: solid {_RULE};
}}
"""

    BINDINGS = [
        Binding("escape", "esc_action",    show=False, priority=True),
        Binding("up",     "nav_up",        show=False, priority=True),
        Binding("down",   "nav_down",      show=False, priority=True),
        Binding("enter",  "confirm",       show=False, priority=True),
        Binding("tab",    "cycle_scope",   show=False, priority=True),
    ]

    def __init__(
        self,
        agent_registry: "dict[str, AgentRoleManifest]",
        cwd: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._registry: dict[str, AgentRoleManifest] = dict(agent_registry)
        self._cwd: Path = cwd or Path.cwd()
        self._mode: str = "browse"
        self._scope: str = "all"
        self._query: str = ""
        self._selected: int = 0
        self._focus_pane: str = "list"
        self._visible: list[AgentRoleManifest] = []
        self._show_full_prompt: bool = False
        # Phase 2 — delete
        self._del_confirmed: bool = False
        # Phase 2 — duplicate
        self._dup_name: str = ""
        self._dup_tier: str = "user"
        # True after any successful create/delete — passed to dismiss() so the
        # session callback can reload the live agent_registry from disk.
        self._registry_changed: bool = False
        self._dup_focus: str = "name"   # "name" | "tier"

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="ag-panel"):
            yield Static("", id="ag-header")
            with Horizontal(id="ag-body"):
                with Vertical(id="ag-list-pane"):
                    yield ModalSearchBar(placeholder="search agents…", id="ag-search")
                    with VerticalScroll(id="ag-list-scroll"):
                        yield Static("", id="ag-list")
                with Vertical(id="ag-preview-pane"):
                    with VerticalScroll(id="ag-preview-scroll"):
                        yield Static("", id="ag-preview")
                    yield Input(placeholder="new agent name…", id="ag-dup-name")
            yield Static("", id="ag-footer")

    def on_mount(self) -> None:
        self._rebuild_visible()
        panel = self.query_one("#ag-panel", Vertical)
        panel.can_focus = True
        panel.focus()
        self.query_one("#ag-dup-name", Input).display = False
        self._refresh()

    # ── Data ──────────────────────────────────────────────────────────────────

    def _rebuild_visible(self) -> None:
        agents = list(self._registry.values())
        agents.sort(key=lambda m: (_TIER_ORDER.get(m.source, 3), m.name))
        if self._scope != "all":
            agents = [m for m in agents if m.source == self._scope]
        if self._query:
            q = self._query.lower()
            agents = [
                m for m in agents
                if q in m.name.lower() or q in m.description.lower()
            ]
        self._visible = agents
        if self._selected >= len(self._visible):
            self._selected = max(0, len(self._visible) - 1)

    def _reload_registry(self) -> None:
        from ...agents.registry import load_agent_registry
        self._registry = dict(load_agent_registry(self._cwd))
        self._rebuild_visible()

    def _current_agent(self) -> "Optional[AgentRoleManifest]":
        if not self._visible:
            return None
        return self._visible[self._selected]

    def _shadow_set(self) -> set[str]:
        """Names that appear in user or project tier (shadows a builtin)."""
        return {
            m.name for m in self._registry.values()
            if m.source in ("user", "project")
        }

    def _builtin_names(self) -> set[str]:
        return {m.name for m in self._registry.values() if m.source == "builtin"}

    def _tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"builtin": 0, "user": 0, "project": 0}
        for m in self._registry.values():
            counts[m.source] = counts.get(m.source, 0) + 1
        return counts

    def _dup_name_available(self) -> bool:
        if not self._dup_name:
            return False
        return not any(m.name == self._dup_name for m in self._registry.values())

    def _dup_target_path(self, tier: str) -> Path:
        name = self._dup_name or "unnamed"
        if tier == "user":
            return Path.home() / ".minion" / "agents" / f"{name}.yaml"
        return self._cwd / ".minion" / "agents" / f"{name}.yaml"

    def _dup_target_path_preview(self, tier: str) -> str:
        name = self._dup_name or "<name>"
        if tier == "user":
            return f"~/.minion/agents/{name}.yaml"
        return f".minion/agents/{name}.yaml"

    def _find_fallback(
        self, manifest: "AgentRoleManifest"
    ) -> "Optional[AgentRoleManifest]":
        """Find the next-lower-tier agent with the same name, if any."""
        current_num = _TIER_ORDER.get(manifest.source, 99)
        candidates = [
            m for m in self._registry.values()
            if m.name == manifest.name and _TIER_ORDER.get(m.source, 99) < current_num
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda m: _TIER_ORDER.get(m.source, 0))

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self.query_one("#ag-header", Static).update(self._build_header())
        self.query_one("#ag-list",   Static).update(self._build_list())
        self.query_one("#ag-preview", Static).update(self._build_preview())
        self.query_one("#ag-footer", Static).update(self._build_footer())

        # Single divider border changes color: orange = left pane active,
        # blue = right pane active. Both classes live on the list pane so
        # there is only ever one border character between the two panes.
        list_pane = self.query_one("#ag-list-pane", Vertical)
        if self._focus_pane == "detail":
            list_pane.remove_class("lhs-focused")
            list_pane.add_class("rhs-focused")
        else:
            list_pane.remove_class("rhs-focused")
            list_pane.add_class("lhs-focused")

        dup_input = self.query_one("#ag-dup-name", Input)
        dup_input.display = (self._mode == "duplicate")
        if self._mode == "duplicate" and self._dup_focus == "name":
            dup_input.focus()

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> Text:
        t = Text()
        t.append("┌─ ", style=_FAINT)
        t.append("/agents", style="bold")
        t.append(" — ", style=_DIM)

        if self._mode == "confirm_delete":
            t.append("delete agent", style=_DIM)
            t.append("  ")
            t.append(" press d again to confirm ", style=f"bold {_ORANGE} on #2a0e06")
        elif self._mode == "duplicate":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} › duplicate", style=_DIM)
        elif self._mode == "detail":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} · detail", style=_DIM)
            t.append("  ")
            t.append(" focused ", style=f"{_DIM} on #161614")
        elif self._query:
            count = len(self._visible)
            t.append("browse agents", style=_DIM)
            t.append("  ")
            noun = "match" if count == 1 else "matches"
            t.append(f" {count} {noun} ", style=f"{_ORANGE} on #1a0800")
        else:
            t.append("browse agents", style=_DIM)

        # Right-aligned counts
        counts = self._tier_counts()
        right = Text(justify="right")
        right.append(f" {counts.get('builtin', 0)} builtin ", style=f"{_FAINT} on #161614")
        right.append(" · ", style=_FAINT)
        right.append(f" {counts.get('user', 0)} user ", style=f"{_GOLD_DIM} on #161614")
        right.append(" · ", style=_FAINT)
        right.append(f" {counts.get('project', 0)} project ", style=f"{_GREEN} on #0a1208")

        # Combine left + right into a two-column table row
        row = Table.grid(expand=True, padding=0)
        row.add_column(ratio=1)
        row.add_column(no_wrap=True, justify="right")
        row.add_row(t, right)
        return row  # type: ignore[return-value]

    # ── List ──────────────────────────────────────────────────────────────────

    def _build_scope_chips(self) -> Text:
        scopes = [
            ("all",     len(self._registry)),
            ("builtin", self._tier_counts().get("builtin", 0)),
            ("user",    self._tier_counts().get("user", 0)),
            ("project", self._tier_counts().get("project", 0)),
        ]
        t = Text()
        t.append("  ")
        for i, (scope, count) in enumerate(scopes):
            if i > 0:
                t.append("   ")
            is_active = self._scope == scope
            if is_active:
                t.append(f" {scope} {count} ", style=f"bold {_ORANGE} on #1a0800")
            else:
                t.append(f" {scope} {count} ", style=f"{_FAINT} on #161614")
        return t

    def _build_tier_header(self, tier: str) -> Text:
        if tier == "builtin":
            label = "─── BUILTIN  read-only  ─────────────────────────────────"
        elif tier == "user":
            label = "─── USER  ───────────────────────────────────────────────"
        else:
            label = "─── PROJECT  ────────────────────────────────────────────"
        return Text(f"  {label}", style=_FAINT)

    def _make_agent_row_table(self) -> Table:
        t = Table.grid(expand=True, padding=0)
        t.add_column(no_wrap=True, width=3)   # pointer
        t.add_column(no_wrap=True, ratio=1)   # name
        t.add_column(no_wrap=True, width=9)   # tier badge
        t.add_column(ratio=2)                  # description
        t.add_column(no_wrap=True, width=4)   # tool count
        return t

    def _add_agent_inner_row(
        self,
        inner: Table,
        manifest: "AgentRoleManifest",
        idx: int,
        shadowed: bool,
        shadows_builtin: bool,
    ) -> None:
        is_selected = idx == self._selected
        is_danger   = self._mode == "confirm_delete" and is_selected
        row_style   = f"on {_TINT_ORG}" if is_selected else ""

        # Pointer
        ptr = Text(no_wrap=True)
        if is_selected and self._focus_pane == "list":
            ptr.append("▸ ", style=f"bold {_ORANGE}")
            ptr.append(" ")
        else:
            ptr.append("   ")

        # Name
        if is_danger:
            name_t = Text(manifest.name, style=f"strike {_ORANGE}", no_wrap=True)
        elif is_selected:
            name_t = Text(manifest.name, style=f"bold {_SILVER}", no_wrap=True)
        else:
            name_t = Text(manifest.name, style=_DIM, no_wrap=True)

        # Tier badge
        tier_t = Text(manifest.source, style=_tier_color(manifest.source), no_wrap=True)

        # Description (with shadowing annotation)
        desc = manifest.description
        if len(desc) > 55:
            desc = desc[:52] + "…"
        if is_danger:
            desc_t = Text(desc, style=f"strike {_FAINT}")
        elif self._query:
            desc_t = _highlight(desc, self._query, _TEXT if is_selected else _DIM)
        else:
            desc_t = Text(desc, style=_TEXT if is_selected else _DIM)

        if shadowed:
            desc_t.append("  ↳ shadowed", style=_FAINT)
        if shadows_builtin:
            tier_t.append("  ↳", style=_FAINT)

        # Tool count
        tools = manifest.tools
        if tools is None:
            count_str = "all"
            count_style = _FAINT
        else:
            count_str = str(len(tools))
            count_style = _ORANGE if is_selected else _DIM
        count_t = Text(count_str, style=count_style, no_wrap=True)

        inner.add_row(ptr, name_t, tier_t, desc_t, count_t, style=row_style)

    def _add_confirm_strip_row(self, inner: Table) -> None:
        ptr = Text("▌  ", style=f"bold {_ORANGE}", no_wrap=True)
        msg = Text()
        msg.append("delete this agent?  ·  ", style=_ORANGE)
        msg.append(" d ", style=f"bold {_SILVER} on #2a2a2a")
        msg.append(" confirm  ·  ", style=_DIM)
        msg.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
        msg.append(" cancel", style=_DIM)
        inner.add_row(ptr, msg, Text(""), Text(""), Text(""), style=f"on {_TINT_ORG}")

    def _build_list(self) -> Table:
        outer = Table.grid(expand=True, padding=0)
        outer.add_column()

        outer.add_row(self._build_scope_chips())
        outer.add_row(Text(""))

        if not self._visible:
            if self._query:
                no_match = Text()
                no_match.append(f'  no agents match "{self._query}"  ·  ', style=_DIM)
                no_match.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
                no_match.append(" to clear", style=_DIM)
                outer.add_row(no_match)
            else:
                outer.add_row(Text("  no agents loaded", style=_FAINT))
            return outer

        shadow_builtins = self._shadow_set()
        builtin_names   = self._builtin_names()

        # Group by tier in order (builtin → user → project)
        tiers_seen: list[str] = []
        by_tier: dict[str, list[int]] = {}
        for idx, m in enumerate(self._visible):
            if m.source not in by_tier:
                tiers_seen.append(m.source)
                by_tier[m.source] = []
            by_tier[m.source].append(idx)

        for tier in tiers_seen:
            outer.add_row(self._build_tier_header(tier))
            inner = self._make_agent_row_table()
            for idx in by_tier[tier]:
                manifest = self._visible[idx]
                shadowed       = tier == "builtin" and manifest.name in shadow_builtins
                shadows_builtin = tier == "project" and manifest.name in builtin_names
                self._add_agent_inner_row(inner, manifest, idx, shadowed, shadows_builtin)
                if self._mode == "confirm_delete" and idx == self._selected:
                    self._add_confirm_strip_row(inner)
            outer.add_row(inner)

        if self._query:
            hidden = len(self._registry) - len(self._visible)
            if hidden > 0:
                outer.add_row(Text(""))
                outer.add_row(Text(
                    "  ─── HIDDEN BY FILTER ─────────────────────────────────",
                    style=_FAINT,
                ))
                noun = "agent" if hidden == 1 else "agents"
                outer.add_row(Text(
                    f"  {hidden} {noun} hidden  ·  esc clears search",
                    style=_DIM,
                ))

        return outer

    # ── Preview / right pane ──────────────────────────────────────────────────

    def _build_preview(self) -> Table:
        agent = self._current_agent()
        if self._mode == "confirm_delete" and agent:
            return self._build_preview_delete(agent)
        if self._mode == "duplicate" and agent:
            return self._build_preview_duplicate(agent)
        if agent is None:
            tbl = Table.grid(expand=True, padding=0)
            tbl.add_column()
            tbl.add_row(Text(""))
            tbl.add_row(Text("  select an agent to see details", style=_FAINT))
            return tbl
        return self._build_preview_browse(agent)

    def _build_preview_browse(self, manifest: "AgentRoleManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        # Header: name + tier chip + mtime
        header = Text()
        header.append(f" {manifest.name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        if manifest.source == "builtin":
            header.append("  ")
            header.append(" read-only ", style=f"{_FAINT} on #161614")
        else:
            mtime = _age(manifest.source_path)
            if mtime != "unknown":
                header.append(f"  ·  edited {mtime}", style=_DIM)
        tbl.add_row(header)
        tbl.add_row(Text(""))

        # Description (wrapped)
        desc_tbl = Table.grid(expand=True, padding=0)
        desc_tbl.add_column(width=1, no_wrap=True)
        desc_tbl.add_column(ratio=1)
        desc_tbl.add_row(Text(""), Text(manifest.description, style=_TEXT))
        tbl.add_row(desc_tbl)
        tbl.add_row(Text(""))

        # Tools section
        tbl.add_row(self._build_tools_section(manifest))
        tbl.add_row(Text(""))

        # SOURCE
        source_header = Text()
        source_header.append(" SOURCE", style=f"bold {_DIM}")
        tbl.add_row(source_header)
        tbl.add_row(Text(f"   {_format_source_path(manifest)}", style=_FAINT))
        tbl.add_row(Text(""))

        # MODEL — placeholder until Phase 4 adds model override to YAML
        model_header = Text()
        model_header.append(" MODEL", style=f"bold {_DIM}")
        model_header.append("  ·  inherit", style=_DIM)
        tbl.add_row(model_header)
        tbl.add_row(Text(f"   uses session model (override available in Phase 4)", style=_FAINT))
        tbl.add_row(Text(""))

        # COLOR — placeholder until Phase 4
        color_header = Text()
        color_header.append(" COLOR", style=f"bold {_DIM}")
        tier_default_color = _tier_color(manifest.source)
        tier_default_name = {"builtin": "muted", "user": "gold", "project": "green"}.get(manifest.source, "inherit")
        color_header.append(f"  ·  ● {tier_default_name} (tier default)", style=f"{tier_default_color}")
        tbl.add_row(color_header)
        tbl.add_row(Text("   chat badge color — override per-agent in Phase 4", style=_FAINT))
        tbl.add_row(Text(""))

        # System prompt preview
        tbl.add_row(self._build_prompt_preview(manifest))

        # Shadowing precedence block for project agents that shadow a builtin
        if manifest.source == "project" and manifest.name in self._builtin_names():
            tbl.add_row(Text(""))
            tbl.add_row(self._build_precedence_block(manifest))

        return tbl

    def _build_tools_section(self, manifest: "AgentRoleManifest") -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        tools = manifest.tools  # None = all, [] = none, [...] = subset
        total = len(_NATIVE_TOOLS)

        header = Text()
        header.append(" TOOLS", style=f"bold {_DIM}")
        if tools is None:
            header.append(" · all allowed", style=_DIM)
        else:
            header.append(f" · {len(tools)} of {total} allowed", style=_DIM)
        tbl.add_row(header)
        tbl.add_row(Text(""))

        if tools is None:
            tbl.add_row(Text("  all native tools allowed", style=_FAINT))
        elif not tools:
            tbl.add_row(Text("  no tools allowed", style=_FAINT))
        else:
            # Group by category
            by_cat: dict[str, list[str]] = {}
            for tool in tools:
                cat = _TOOL_CATEGORIES.get(tool, "other")
                by_cat.setdefault(cat, []).append(tool)

            for cat in sorted(by_cat):
                tbl.add_row(Text(f"  {cat}", style=_FAINT))
                for tool in by_cat[cat]:
                    row = Text()
                    row.append(f"    {tool:<22}", style=_TEXT)
                    desc = _TOOL_DESCRIPTIONS.get(tool, "")
                    if desc:
                        row.append(f"{desc}", style=_FAINT)
                    warn = _TOOL_WARN.get(tool, "")
                    if warn:
                        row.append(f"  {warn}", style=f"bold {_ORANGE}")
                    tbl.add_row(row)

            # Denied list — vertical, one per line
            denied = [t for t in _NATIVE_TOOLS if t not in tools]
            if denied:
                tbl.add_row(Text(""))
                tbl.add_row(Text("  denied", style=_DIM))
                for tool in denied:
                    row = Text()
                    row.append(f"    {tool:<22}", style=_FAINT)
                    desc = _TOOL_DESCRIPTIONS.get(tool, "")
                    if desc:
                        row.append(desc, style=_FAINT)
                    tbl.add_row(row)

        return tbl

    def _build_prompt_preview(self, manifest: "AgentRoleManifest") -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        lines = manifest.system_prompt.splitlines()
        total = len(lines)
        max_preview = 5

        header = Text()
        header.append(" SYSTEM PROMPT", style=f"bold {_DIM}")
        header.append(f"  ·  {total} lines", style=_DIM)
        if not self._show_full_prompt:
            header.append("  ", style=_DIM)
            header.append(" v ", style=f"bold {_SILVER} on #2a2a2a")
            header.append(" view full", style=_DIM)
        else:
            header.append("  ", style=_DIM)
            header.append(" v ", style=f"bold {_SILVER} on #2a2a2a")
            header.append(" collapse", style=_DIM)
        tbl.add_row(header)
        tbl.add_row(Text(""))

        preview_lines = lines if self._show_full_prompt else lines[:max_preview]
        content_parts: list[str] = list(preview_lines)
        if not self._show_full_prompt and total > max_preview:
            content_parts.append(f"… +{total - max_preview} more lines")

        content_text = Text()
        for part in content_parts:
            content_text.append((part or " ") + "\n", style=_FAINT)

        panel = Panel(
            content_text,
            border_style=_RULE,
            style="on #0f0f0d",
            padding=(0, 1),
            expand=True,
        )
        tbl.add_row(panel)

        return tbl

    def _build_precedence_block(self, manifest: "AgentRoleManifest") -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(" PRECEDENCE", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))

        # This (project) agent is used
        active = Text()
        active.append("  ●  this   ", style=f"bold {_GREEN}")
        active.append(_format_source_path(manifest), style=_FAINT)
        active.append("  ← used", style=f"bold {_GREEN}")
        tbl.add_row(active)

        # Shadowed builtin
        builtin_m = next(
            (m for m in self._registry.values()
             if m.name == manifest.name and m.source == "builtin"),
            None,
        )
        if builtin_m:
            below = Text()
            below.append("  ○  below  ", style=_FAINT)
            below.append(_format_source_path(builtin_m), style=_FAINT)
            tbl.add_row(below)

        return tbl

    def _build_preview_delete(self, manifest: "AgentRoleManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        tbl.add_row(Text(""))

        warn = Text()
        warn.append(" ⚠  About to delete ", style=f"bold {_ORANGE}")
        warn.append(manifest.name, style=f"bold {_SILVER}")
        warn.append(f"     {manifest.source} tier", style=_DIM)
        tbl.add_row(warn)
        tbl.add_row(Text(""))

        tbl.add_row(Text(" The agent file will be permanently removed.", style=_DIM))
        tbl.add_row(Text(" No backup is created. No undo.", style=_DIM))
        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))

        # File info
        tbl.add_row(Text(" FILE", style=f"bold {_DIM}"))
        tbl.add_row(Text(f"   {_format_source_path(manifest)}", style=_FAINT))
        mtime = _age(manifest.source_path)
        line_count = len(manifest.system_prompt.splitlines())
        tbl.add_row(Text(f"   last edited {mtime}  ·  {line_count} lines of prompt", style=_FAINT))
        tbl.add_row(Text(""))

        # Fallback after delete
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(" FALLBACK AFTER DELETE", style=f"bold {_DIM}"))
        tbl.add_row(Text("  Subagent calls to this name resolve to:", style=_DIM))
        fallback = self._find_fallback(manifest)
        if fallback:
            fb_t = Text()
            fb_t.append(f"  → {fallback.source}  ", style=_FAINT)
            fb_t.append(_format_source_path(fallback), style=_FAINT)
            tbl.add_row(fb_t)
        else:
            tbl.add_row(Text("  → — none —", style=_FAINT))
        tbl.add_row(Text(""))

        # Confirm strip
        tbl.add_row(Rule(style=_RULE))
        confirm = Text()
        confirm.append("  ")
        confirm.append(" d ", style=f"bold {_SILVER} on #2a2a2a")
        confirm.append("  confirm delete      ", style=_DIM)
        confirm.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
        confirm.append("  cancel", style=_DIM)
        tbl.add_row(confirm)

        return tbl

    def _build_preview_duplicate(self, manifest: "AgentRoleManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        # FROM section
        from_t = Text()
        from_t.append(" FROM  ", style=_DIM)
        from_t.append(f"  {manifest.name}  ", style=f"bold {_SILVER}")
        from_t.append(manifest.source, style=_tier_color(manifest.source))
        from_t.append(f"  ·  {_format_source_path(manifest)}", style=_FAINT)
        tbl.add_row(from_t)
        tbl.add_row(Text(""))
        tbl.add_row(Text(
            " Tools and system prompt are copied verbatim.",
            style=_FAINT,
        ))
        tbl.add_row(Text(""))

        # Name section (Input widget appears below this Static)
        tbl.add_row(Text(" ─── new name ────────────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))

        # Availability indicator
        avail = self._dup_name_available()
        if self._dup_name:
            if avail:
                tbl.add_row(Text("  ✓ available", style=f"bold {_GREEN}"))
            else:
                tbl.add_row(Text("  ✗ name already exists", style=f"bold {_ORANGE}"))
        else:
            tbl.add_row(Text("  enter a unique name", style=_FAINT))
        tbl.add_row(Text(""))

        # Tier selector
        tbl.add_row(Text(" ─── target tier ─────────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        for tier in ["user", "project"]:
            is_sel = self._dup_tier == tier
            tier_focused = self._dup_focus == "tier"
            bullet = "●" if is_sel else "○"
            ptr_style = f"bold {_ORANGE}" if (is_sel and tier_focused) else (_DIM if not is_sel else _SILVER)
            row = Text()
            row.append(f"  {'▸' if (is_sel and tier_focused) else ' '} {bullet} ", style=ptr_style)
            row.append(f"{tier:<8}", style=f"bold {_tier_color(tier)}" if is_sel else _DIM)
            row.append(f"  {self._dup_target_path_preview(tier)}", style=_FAINT)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if (is_sel and tier_focused) else "")
        tbl.add_row(Text(""))

        # What gets copied
        tbl.add_row(Text(" ─── what gets copied ────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        tools = manifest.tools
        tools_str = f"{len(tools)} tools" if tools is not None else "all tools"
        prompt_lines = len(manifest.system_prompt.splitlines())
        for item, value in [("tools", tools_str), ("system prompt", f"{prompt_lines} lines")]:
            row = Text()
            row.append("  ✓  ", style=f"bold {_GREEN}")
            row.append(f"{item:<16}", style=_DIM)
            row.append(value, style=_FAINT)
            tbl.add_row(row)

        # Result preview
        if self._dup_name and avail:
            tbl.add_row(Text(""))
            tbl.add_row(Rule(style=_RULE))
            result = Text()
            result.append("  RESULT  ", style=_DIM)
            result.append(f"new {self._dup_tier} agent  ", style=_DIM)
            result.append(self._dup_name, style=f"bold {_tier_color(self._dup_tier)}")
            result.append(f"  at  {self._dup_target_path_preview(self._dup_tier)}", style=_FAINT)
            tbl.add_row(result)

        return tbl

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self) -> Text:
        dot = f" [{_FAINT}]·[/] "
        agent = self._current_agent()
        is_builtin = agent is not None and agent.source == "builtin"

        if self._mode == "confirm_delete":
            hints = [
                _hint("d", "confirm delete"),
                _hint("esc", "cancel"),
            ]
            suffix = f"  [{_FAINT}]irreversible — no backup[/]"
        elif self._mode == "duplicate":
            hints = [
                _hint("tab", "next field"),
                _hint("↑↓", "switch tier"),
                _hint("↵", "duplicate & open"),
                _hint("esc", "cancel"),
            ]
            suffix = ""
        elif self._mode == "detail":
            hints = [
                _hint("↑↓", "scroll"),
                _hint("v", "view full prompt"),
            ]
            if not is_builtin:
                hints += [
                    _hint("r", "run"),
                    _hint("y", "duplicate"),
                    _hint("d", "delete"),
                ]
            else:
                hints += [_hint("r", "run"), _hint("y", "duplicate")]
            hints.append(_hint("esc", "back"))
            suffix = ""
        elif self._query:
            hints = [
                _hint("↑↓", "nav matches"),
                _hint("↵", "focus"),
                _hint("esc", "clear search"),
            ]
            suffix = ""
        else:
            hints = [
                _hint("↑↓", "nav"),
                _hint("tab", "scope"),
                _hint("↵", "focus"),
                _hint("/", "search"),
                _hint("r", "run"),
                _hint("y", "dup"),
            ]
            if not is_builtin:
                hints.append(_hint("d", "delete"))
            hints.append(_hint("esc", "close"))
            suffix = "" if not is_builtin else f"  [{_FAINT}]read-only — edit/delete hidden[/]"

        t = Text.from_markup("  " + dot.join(hints) + (suffix or ""))
        return t

    # ── Actions (BINDINGS) ────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple) -> bool | None:  # type: ignore[override]
        return True

    def action_nav_up(self) -> None:
        if self._focus_pane == "detail":
            self.query_one("#ag-preview-scroll", VerticalScroll).scroll_relative(y=-3)
            return
        if self._mode == "duplicate" and self._dup_focus == "tier":
            self._dup_tier = "user"
            self._refresh()
            return
        if self._mode in ("confirm_delete", "duplicate"):
            return
        if self._visible:
            self._selected = max(0, self._selected - 1)
            self._refresh()

    def action_nav_down(self) -> None:
        if self._focus_pane == "detail":
            self.query_one("#ag-preview-scroll", VerticalScroll).scroll_relative(y=3)
            return
        if self._mode == "duplicate" and self._dup_focus == "tier":
            self._dup_tier = "project"
            self._refresh()
            return
        if self._mode in ("confirm_delete", "duplicate"):
            return
        if self._visible:
            self._selected = min(len(self._visible) - 1, self._selected + 1)
            self._refresh()

    def action_esc_action(self) -> None:
        if self._mode in ("confirm_delete", "duplicate", "detail"):
            self._mode = "browse"
            self._del_confirmed = False
            self._dup_name = ""
            self._dup_focus = "name"
            self._show_full_prompt = False
            self._focus_pane = "list"
            self.query_one("#ag-panel", Vertical).focus()
            self._refresh()
        elif self._query:
            self.query_one("#ag-search", ModalSearchBar).clear()
            self._query = ""
            self._mode = "browse"
            self._rebuild_visible()
            self.query_one("#ag-panel", Vertical).focus()
            self._refresh()
        else:
            self.dismiss(self._registry_changed)

    def action_confirm(self) -> None:
        if self._mode == "duplicate":
            if self._dup_focus == "name":
                self._dup_focus = "tier"
                self.query_one("#ag-panel", Vertical).focus()
                self._refresh()
            elif self._dup_focus == "tier" and self._dup_name_available():
                self._do_duplicate()
        elif self._mode in ("browse", "search") and self._visible:
            self._mode = "detail"
            self._focus_pane = "detail"
            self._refresh()
        elif self._mode == "detail":
            pass  # Enter in detail mode: no-op (run is 'r')

    def action_cycle_scope(self) -> None:
        if self._mode == "duplicate":
            if self._dup_focus == "name":
                self._dup_focus = "tier"
                self.query_one("#ag-panel", Vertical).focus()
            else:
                self._dup_focus = "name"
                self.query_one("#ag-dup-name", Input).focus()
            self._refresh()
            return
        if self._mode not in ("browse", "search"):
            return
        scopes = ["all", "builtin", "user", "project"]
        idx = scopes.index(self._scope) if self._scope in scopes else 0
        self._scope = scopes[(idx + 1) % len(scopes)]
        self._rebuild_visible()
        self._refresh()

    # ── Key dispatch ──────────────────────────────────────────────────────────

    def on_key(self, event: Key) -> None:
        key = event.key

        # When the dup-name input has focus, only handle esc
        try:
            focused = self.focused
        except Exception:
            focused = None
        if isinstance(focused, Input):
            if key == "escape" and focused.id == "ag-dup-name":
                self._mode = "browse"
                self._dup_name = ""
                self._dup_focus = "name"
                self.query_one("#ag-panel", Vertical).focus()
                self._refresh()
                event.stop()
            return

        mode = self._mode

        if mode == "confirm_delete":
            if key == "d":
                self._do_delete()
                event.stop()
            return

        if mode == "duplicate":
            return

        # Browse / search / detail modes
        if key == "d":
            self._start_delete()
            event.stop()
        elif key == "y":
            self._start_duplicate()
            event.stop()
        elif key == "r":
            self._action_run_stub()
            event.stop()
        elif key == "slash":
            self.query_one("#ag-search", ModalSearchBar).focus_input()
            self._mode = "search"
            self._refresh()
            event.stop()
        elif key == "v":
            self._show_full_prompt = not self._show_full_prompt
            self._refresh()
            event.stop()
        elif key == "c":
            self._copy_path()
            event.stop()
        elif key in ("left", "right") and self._mode in ("browse", "search"):
            scopes = ["all", "builtin", "user", "project"]
            idx = scopes.index(self._scope) if self._scope in scopes else 0
            delta = -1 if key == "left" else 1
            self._scope = scopes[(idx + delta) % len(scopes)]
            self._rebuild_visible()
            self._refresh()
            event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "ag-dup-name":
            self._dup_name = event.value.strip()
            self._refresh()
        else:
            # ModalSearchBar wraps an Input without exposing its id — treat any
            # other Input.Changed as the search bar firing.
            self._query = event.value.strip().lower()
            self._mode = "search" if self._query else "browse"
            self._rebuild_visible()
            self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ag-dup-name":
            if self._dup_name_available():
                self._dup_focus = "tier"
                self.query_one("#ag-panel", Vertical).focus()
                self._refresh()
        else:
            self.query_one("#ag-panel", Vertical).focus()
            self._mode = "browse" if not self._query else "search"
            self._refresh()

    # ── Disk operations ───────────────────────────────────────────────────────

    def _start_delete(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin":
            return
        self._mode = "confirm_delete"
        self._del_confirmed = False
        self._refresh()

    def _do_delete(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source_path is None:
            return
        try:
            agent.source_path.unlink()
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._del_confirmed = False
        self._focus_pane = "list"
        self._refresh()

    def _start_duplicate(self) -> None:
        agent = self._current_agent()
        if agent is None:
            return
        self._dup_name = f"{agent.name}-copy"
        self._dup_tier = "user"
        self._dup_focus = "name"
        self._mode = "duplicate"
        self._refresh()
        # Focus the name input after refresh
        dup_input = self.query_one("#ag-dup-name", Input)
        dup_input.value = self._dup_name
        dup_input.focus()

    def _do_duplicate(self) -> None:
        source_agent = self._current_agent()
        if source_agent is None or source_agent.source_path is None:
            return
        if not self._dup_name_available():
            return
        target_path = self._dup_target_path(self._dup_tier)
        dup_name = self._dup_name
        try:
            raw = yaml.safe_load(source_agent.source_path.read_text(encoding="utf-8")) or {}
            raw["name"] = dup_name  # must match filename so registry lookup works
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(
                yaml.dump(raw, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        # Jump focus to the newly created agent
        new_agent_idx = next(
            (i for i, m in enumerate(self._visible) if m.name == dup_name and m.source == self._dup_tier),
            0,
        )
        self._selected = new_agent_idx
        self._mode = "detail"
        self._focus_pane = "detail"
        self._dup_name = ""
        self._dup_focus = "name"
        self.query_one("#ag-panel", Vertical).focus()
        self._refresh()

    def _action_run_stub(self) -> None:
        """Phase 3 placeholder — run flow not yet implemented."""
        pass

    def _copy_path(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source_path is None:
            return
        try:
            import subprocess
            subprocess.run(
                ["pbcopy"],
                input=str(agent.source_path).encode(),
                check=False,
            )
        except (OSError, FileNotFoundError):
            pass
