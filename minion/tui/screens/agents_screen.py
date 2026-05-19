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
from textual.widgets import Input, Static, TextArea

from .base import (
    ModalSearchBar,
    _ORANGE, _GOLD, _GOLD_DIM, _GREEN, _GREEN_DIM, _BLUE, _SILVER,
    _DIM, _FAINT, _RULE, _TEXT, _TINT_ORG, _BG,
    _TOOL_CATEGORIES, _TOOL_DESCRIPTIONS, _TOOL_WARN, _NATIVE_TOOLS, _TIER_ORDER,
    _age, _highlight, _tier_color, _hint,
)

if TYPE_CHECKING:
    from ...agents.manifest import AgentRoleManifest

# Default tool set for new agents created with the blank starting point
_BLANK_TOOLS: list[str] = ["read_file", "list_directory", "search_file", "get_file_outline"]

# Named color options for the color picker (Phase 4)
_COLOR_OPTIONS: list[tuple[str, str, str]] = [
    ("gold",    _GOLD,    "user-tier default"),
    ("green",   _GREEN,   "project-tier default"),
    ("blue",    _BLUE,    "model / accent"),
    ("orange",  _ORANGE,  "high-attention (use sparingly)"),
    ("silver",  _SILVER,  "neutral helper"),
    ("muted",   _DIM,     "background / quiet"),
    ("inherit", _FAINT,   "no override — falls back to tier default"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────


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
    width: 50%;
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
    width: 50%;
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
#ag-dup-top {{
    height: auto;
    display: none;
    padding: 1 1 0 1;
}}
#ag-dup-validation {{
    height: auto;
    display: none;
    padding: 0 1;
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
#ag-preview-scroll.run-compact {{
    height: 12;
}}
#ag-run-prompt-label {{
    height: auto;
    display: none;
    margin-top: 1;
}}
#ag-run-prompt-label.no-top-margin {{
    margin-top: 0;
}}
#ag-run-input {{
    height: 8;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {_DIM};
}}
#ag-run-input.single-line {{
    height: 12;
}}
#ag-run-input.prompt-edit {{
    height: 1fr;
}}
#ag-run-input:focus {{
    border: solid {_ORANGE};
}}
#ag-run-input .text-area--cursor-line {{
    background: #1a1a1a;
}}
#ag-preview-scroll.text-edit-compact {{
    height: auto;
}}
#ag-run-hints {{
    height: 2;
    display: none;
    background: {_BG};
    border-top: solid {_RULE};
    padding: 0 2;
    margin: 0 -1;
}}
#ag-footer {{
    height: 2;
    padding: 0 2;
    background: {_BG};
    border-top: solid {_RULE};
}}
#ag-create-banner {{
    height: auto;
    display: none;
    padding: 0 1;
    color: {_ORANGE};
}}
#ag-run-input.inherited {{
    color: {_DIM};
}}
"""

    BINDINGS = [
        Binding("escape", "esc_action",    show=False, priority=True),
        Binding("up",     "nav_up",        show=False, priority=True),
        Binding("down",   "nav_down",      show=False, priority=True),
        Binding("enter",  "confirm",       show=False, priority=True),
        Binding("tab",        "cycle_scope",      show=False, priority=True),
        Binding("shift+tab",  "cycle_scope_back", show=False, priority=True),
        Binding("ctrl+enter", "confirm_primary",  show=False, priority=True),
        Binding("ctrl+j",     "confirm_primary",  show=False, priority=True),
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
        # Phase 2 — delete
        self._del_confirmed: bool = False
        # Phase 2 — duplicate
        self._dup_name: str = ""
        self._dup_tier: str = "user"
        # True after any successful create/delete — passed to dismiss() so the
        # session callback can reload the live agent_registry from disk.
        self._registry_changed: bool = False
        self._dup_focus: str = "name"   # "name" | "tier"
        # Phase 3 — run
        self._run_agent_name: str = ""
        # Phase 4 — edit color
        self._edit_color_cursor: int = 0
        # Phase 4 — edit tools
        self._edit_tools: list[str] = []
        self._edit_tools_saved: list[str] = []
        self._edit_tools_cursor: int = 0
        # Phase 4 — edit model
        self._edit_model_cursor: int = 0
        self._edit_model_flat: list[Optional[str]] = []
        # Field edits
        self._edit_iterations_val: int = 20
        # Create form state
        self._create_name: str = ""
        self._create_tier: str = "user"           # "user" | "project"
        self._create_desc: str = ""
        self._create_desc_inherited: bool = False  # True until user edits desc after template pick
        self._create_starting_point: str = "blank" # "blank" | "template"
        self._create_template_cursor: int = 0      # index into _get_template_list()
        self._create_focus: str = "name"           # "name"|"tier"|"description"|"starting_point"|"template_picker"
        # Post-create undo
        self._create_undo_path: Optional[Path] = None
        self._create_undo_active: bool = False

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="ag-panel"):
            yield Static("", id="ag-header")
            with Horizontal(id="ag-body"):
                with Vertical(id="ag-list-pane"):
                    yield Static("", id="ag-create-banner")
                    yield ModalSearchBar(placeholder="search agents…", id="ag-search")
                    with VerticalScroll(id="ag-list-scroll"):
                        yield Static("", id="ag-list")
                with Vertical(id="ag-preview-pane"):
                    yield Static("", id="ag-dup-top")
                    yield Input(placeholder="new agent name…", id="ag-dup-name")
                    yield Static("", id="ag-dup-validation")
                    yield Static("", id="ag-run-prompt-label")
                    yield TextArea("", id="ag-run-input")
                    with VerticalScroll(id="ag-preview-scroll"):
                        yield Static("", id="ag-preview")
                    yield Static("", id="ag-run-hints")
            yield Static("", id="ag-footer")

    def on_mount(self) -> None:
        self._rebuild_visible()
        panel = self.query_one("#ag-panel", Vertical)
        panel.can_focus = True
        panel.focus()
        self.query_one("#ag-dup-top", Static).display = False
        self.query_one("#ag-dup-name", Input).display = False
        self.query_one("#ag-dup-validation", Static).display = False
        self.query_one("#ag-run-prompt-label", Static).display = False
        self.query_one("#ag-run-input", TextArea).display = False
        self.query_one("#ag-run-hints", Static).display = False
        self.query_one("#ag-create-banner", Static).display = False
        self._refresh()

    # ── Data ──────────────────────────────────────────────────────────────────

    def _rebuild_visible(self) -> None:
        agents = list(self._registry.values())
        agents.sort(key=lambda m: (_TIER_ORDER.get(m.source, 3), m.name))
        # Scope filter is bypassed while searching — search always covers all tiers.
        if self._scope != "all" and not self._query:
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

    def _dup_suggest(self) -> str:
        """Return a non-colliding name based on _dup_name, trying -copy, -copy-2, etc."""
        existing = {m.name for m in self._registry.values()}
        base = self._dup_name
        candidate = f"{base}-copy"
        n = 2
        while candidate in existing:
            candidate = f"{base}-copy-{n}"
            n += 1
        return candidate

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

    # ── Create form helpers ───────────────────────────────────────────────────

    def _create_name_valid(self) -> bool:
        import re
        return bool(re.match(r'^[a-z0-9][a-z0-9-]{1,39}$', self._create_name))

    def _create_name_available(self) -> bool:
        return self._create_name_valid() and not any(
            m.name == self._create_name for m in self._registry.values()
        )

    def _create_suggest(self) -> str:
        """Return first available name by integer suffix: {name}-2, {name}-3, …"""
        existing = {m.name for m in self._registry.values()}
        n = 2
        while True:
            candidate = f"{self._create_name}-{n}"
            if candidate not in existing:
                return candidate
            n += 1

    def _create_target_path(self, tier: str) -> Path:
        name = self._create_name or "unnamed"
        if tier == "user":
            return Path.home() / ".minion" / "agents" / f"{name}.yaml"
        return self._cwd / ".minion" / "agents" / f"{name}.yaml"

    def _create_target_path_preview(self, tier: str) -> str:
        name = self._create_name or "<name>"
        if tier == "user":
            return f"~/.minion/agents/{name}.yaml"
        return f".minion/agents/{name}.yaml"

    def _create_scaffold_prompt(self, name: str, desc: str) -> str:
        body = desc.strip()
        return f"# {name}\n\n{body}" if body else f"# {name}"

    def _get_template_list(self) -> "list[AgentRoleManifest]":
        return sorted(self._registry.values(),
                      key=lambda m: (_TIER_ORDER.get(m.source, 3), m.name))

    def _template_capability_summary(self, m: "AgentRoleManifest") -> str:
        tools = m.tools if m.tools is not None else _NATIVE_TOOLS
        count = len(tools)
        has_shell = "run_shell" in tools
        has_write = any(t in tools for t in ("write_file", "edit_file", "delete_file"))
        if has_shell:
            kind = "edits + shell"
        elif has_write:
            kind = "edits files"
        else:
            kind = "read-only"
        lines = len(m.system_prompt.splitlines())
        return f"{kind} · {count} tools · {lines} lines"

    def _update_create_widget_focus(self) -> None:
        if self._create_focus == "name":
            self.query_one("#ag-dup-name", Input).focus()
        elif self._create_focus == "description":
            self.query_one("#ag-run-input", TextArea).focus()
        else:
            self.query_one("#ag-panel", Vertical).focus()

    def _sync_template_selection(self) -> None:
        """Populate description from selected template; mark as inherited."""
        templates = self._get_template_list()
        if 0 <= self._create_template_cursor < len(templates):
            tmpl = templates[self._create_template_cursor]
            ta = self.query_one("#ag-run-input", TextArea)
            ta.load_text(tmpl.description)
            self._create_desc = tmpl.description
            self._create_desc_inherited = True

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self.query_one("#ag-header", Static).update(self._build_header())
        self.query_one("#ag-footer", Static).update(self._build_footer())

        self.query_one("#ag-list", Static).update(self._build_list())
        self.query_one("#ag-preview", Static).update(self._build_preview())

        # Single divider border changes color: orange = left pane active,
        # blue = right pane active.
        _NON_BROWSE = {"run", "edit_tools", "edit_model", "edit_color",
                       "edit_description", "edit_iterations", "edit_prompt",
                       "duplicate", "create"}
        list_pane = self.query_one("#ag-list-pane", Vertical)
        if self._focus_pane == "detail" or self._mode in _NON_BROWSE:
            list_pane.remove_class("lhs-focused")
            list_pane.add_class("rhs-focused")
        else:
            list_pane.remove_class("rhs-focused")
            list_pane.add_class("lhs-focused")

        in_dup    = (self._mode == "duplicate")
        in_create = (self._mode == "create")
        in_either = in_dup or in_create
        agent = self._current_agent()

        # ── dup-top / dup-name / dup-validation ──────────────────────────────
        self.query_one("#ag-dup-top", Static).display = in_either
        if in_create:
            self.query_one("#ag-dup-top", Static).update(self._build_create_top())
        elif in_dup and agent:
            self.query_one("#ag-dup-top", Static).update(self._build_dup_top(agent))

        self.query_one("#ag-dup-validation", Static).display = in_either
        if in_create:
            self.query_one("#ag-dup-validation", Static).update(self._build_create_validation())
        elif in_dup:
            self.query_one("#ag-dup-validation", Static).update(self._build_dup_validation())

        dup_input = self.query_one("#ag-dup-name", Input)
        dup_input.display = in_either
        if in_create and self._create_focus == "name":
            dup_input.focus()
        elif in_dup and self._dup_focus == "name":
            dup_input.focus()

        # ── banner ────────────────────────────────────────────────────────────
        banner = self.query_one("#ag-create-banner", Static)
        banner.display = self._create_undo_active
        if self._create_undo_active:
            banner.update(self._build_create_banner())

        # ── TextArea / label / hints ──────────────────────────────────────────
        in_run = (self._mode == "run")
        in_prompt = (self._mode == "edit_prompt")
        in_field = (self._mode == "edit_description")   # iterations uses stepper, not TextArea
        in_any_edit = in_run or in_prompt or in_field

        preview_scroll = self.query_one("#ag-preview-scroll", VerticalScroll)
        ta = self.query_one("#ag-run-input", TextArea)

        # Compact scroll height: auto for field/prompt edits, 12 for run
        # Create mode keeps height: 1fr so the scroll fills remaining space
        # and the hints strip stays pinned to the bottom of the pane.
        if in_run:
            preview_scroll.remove_class("text-edit-compact")
            preview_scroll.add_class("run-compact")
        elif in_prompt or in_field:
            preview_scroll.remove_class("run-compact")
            preview_scroll.add_class("text-edit-compact")
        else:
            preview_scroll.remove_class("run-compact")
            preview_scroll.remove_class("text-edit-compact")

        # TextArea size modifiers
        if in_field or in_create:
            ta.add_class("single-line")
            ta.remove_class("prompt-edit")
        elif in_prompt or in_run:
            ta.remove_class("single-line")
            ta.add_class("prompt-edit")
        else:
            ta.remove_class("single-line")
            ta.remove_class("prompt-edit")

        # Inherited-description styling
        if in_create and self._create_desc_inherited:
            ta.add_class("inherited")
        else:
            ta.remove_class("inherited")

        # Label above TextArea (run + edit_prompt + field edits + create)
        show_label = in_any_edit or in_create
        lbl = self.query_one("#ag-run-prompt-label", Static)
        lbl.display = show_label
        if show_label:
            if in_create:
                label_text = self._build_create_desc_label()
                lbl.remove_class("no-top-margin")
            elif in_run:
                label_text = self._build_run_prompt_label()
                lbl.remove_class("no-top-margin")
            elif in_prompt:
                agent = self._current_agent()
                label_text = self._build_prompt_edit_label(agent)
                lbl.add_class("no-top-margin")
            else:  # edit_description — identity card sits flush with the top of the pane
                agent = self._current_agent()
                label_text = self._build_field_edit_label("DESCRIPTION", agent)
                lbl.add_class("no-top-margin")
            lbl.update(label_text)
        else:
            lbl.remove_class("no-top-margin")

        # TextArea: all edit modes + create
        ta.display = in_any_edit or in_create
        if in_create and self._create_focus == "description":
            ta.focus()

        # Hints strip
        show_hints = in_any_edit or in_create
        self.query_one("#ag-run-hints", Static).display = show_hints
        if show_hints:
            if in_create:
                hints_text: Text = self._build_create_hints()
            elif in_run:
                hints_text = self._build_run_hints()
            else:
                hints_text = self._build_edit_hints()
            self.query_one("#ag-run-hints", Static).update(hints_text)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> Text:
        t = Text()
        t.append("┌─ ", style=_FAINT)
        t.append("/agents", style="bold")
        sep = " › " if self._mode == "create" else " — "
        t.append(sep, style=_DIM)

        if self._mode == "create":
            t.append("create new agent", style=f"bold {_ORANGE}")
        elif self._mode == "confirm_delete":
            t.append("delete agent", style=_DIM)
            t.append("  ")
            t.append(" press d again to confirm ", style=f"bold {_ORANGE} on #2a0e06")
        elif self._mode == "duplicate":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} › duplicate", style=_DIM)
        elif self._mode == "run":
            t.append(f"{self._run_agent_name} › ", style=_DIM)
            t.append("run", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(" ctrl+↵ to dispatch ", style=f"{_SILVER} on #161614")
        elif self._mode == "edit_tools":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            allowed = len(self._edit_tools)
            total = len(_NATIVE_TOOLS)
            t.append(f"{name} › ", style=_DIM)
            t.append("edit tools", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(f" {allowed} of {total} allowed ", style=f"{_ORANGE} on #1a0800")
        elif self._mode == "edit_model":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit model", style=f"bold {_ORANGE}")
        elif self._mode == "edit_color":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit color", style=f"bold {_ORANGE}")
        elif self._mode == "edit_description":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit description", style=f"bold {_ORANGE}")
        elif self._mode == "edit_iterations":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit max iterations", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(f" {self._edit_iterations_val} ", style=f"bold {_ORANGE} on #1a0800")
        elif self._mode == "edit_prompt":
            agent = self._current_agent()
            name = agent.name if agent else "agent"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit system prompt", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(" ctrl+↵ to save ", style=f"{_SILVER} on #161614")
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

        # Tier counter only in browse context; edit/run modes already have the breadcrumb
        _NON_BROWSE = {"run", "edit_tools", "edit_model", "edit_color",
                       "edit_description", "edit_iterations", "edit_prompt", "duplicate", "create"}
        row = Table.grid(expand=True, padding=0)
        row.add_column(ratio=1)
        row.add_column(no_wrap=True, justify="right")
        if self._mode not in _NON_BROWSE:
            counts = self._tier_counts()
            right = Text(justify="right")
            right.append(f" {counts.get('builtin', 0)} builtin ", style=f"{_FAINT} on #161614")
            right.append(" · ", style=_FAINT)
            right.append(f" {counts.get('user', 0)} user ", style=f"{_GOLD_DIM} on #161614")
            right.append(" · ", style=_FAINT)
            right.append(f" {counts.get('project', 0)} project ", style=f"{_GREEN} on #0a1208")
            row.add_row(t, right)
        else:
            row.add_row(t, Text(""))
        return row  # type: ignore[return-value]

    # ── List ──────────────────────────────────────────────────────────────────

    def _build_scope_chips(self) -> Text:
        search_overrides = bool(self._query) and self._scope != "all"
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
                if search_overrides:
                    # Dim the chip to signal scope is bypassed by the search
                    t.append(f" {scope} {count} ", style=f"{_DIM} on #161614")
                else:
                    t.append(f" {scope} {count} ", style=f"bold {_ORANGE} on #1a0800")
            elif count == 0:
                t.append(f" {scope} {count} ", style="#383838 on #0d0d0d")
            else:
                t.append(f" {scope} {count} ", style=f"{_FAINT} on #161614")
        if search_overrides:
            t.append("   · searching all tiers", style=_FAINT)
        return t

    def _build_tier_header(self, tier: str) -> Text:
        _FILL = "─" * 120
        t = Text(no_wrap=True)
        if tier == "builtin":
            t.append("  ─── BUILTIN ", style=_FAINT)
            t.append("agents/builtin/", style=f"italic {_FAINT}")
            t.append("  read-only  " + _FILL, style=_FAINT)
        elif tier == "user":
            t.append("  ─── USER ", style=f"bold {_GOLD_DIM}")
            t.append("~/.minion/agents/", style=f"italic {_FAINT}")
            t.append("  " + _FILL, style=_FAINT)
        else:
            t.append("  ─── PROJECT ", style=f"bold {_GREEN_DIM}")
            t.append(".minion/agents/", style=f"italic {_FAINT}")
            t.append("  " + _FILL, style=_FAINT)
        return t

    def _make_agent_row_table(self, name_w: int = 18) -> Table:
        t = Table.grid(expand=True, padding=0)
        t.add_column(no_wrap=True, width=3)                        # pointer
        t.add_column(no_wrap=True, width=name_w)                   # name (content-sized)
        t.add_column(no_wrap=True, ratio=1, overflow="ellipsis")   # description
        t.add_column(no_wrap=True, width=2)                        # spacer
        t.add_column(no_wrap=True, width=6)                        # tool count
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
        row_style   = f"on {_TINT_ORG}" if is_danger else ""

        # Pointer
        ptr = Text(no_wrap=True)
        if is_selected and self._focus_pane == "list":
            ptr.append("▸ ", style=f"bold {_ORANGE}")
            ptr.append(" ")
        else:
            ptr.append("   ")

        # Name — tier color at full brightness when selected, dimmed when not
        _tier_bright = {"builtin": _ORANGE, "user": _GOLD, "project": _GREEN}
        _tier_dim    = {"builtin": _DIM,    "user": _GOLD_DIM, "project": _GREEN_DIM}
        if is_danger:
            name_t = Text(manifest.name, style=f"strike {_ORANGE}", no_wrap=True)
        elif is_selected:
            name_t = Text(manifest.name, style=f"bold {_tier_bright.get(manifest.source, _ORANGE)}", no_wrap=True)
        else:
            name_t = Text(manifest.name, style=_tier_dim.get(manifest.source, _DIM), no_wrap=True)

        # Description (with shadowing annotation — no separate tier badge column)
        desc = manifest.description
        if is_danger:
            desc_t = Text(desc, style=f"strike {_FAINT}")
        elif self._query:
            desc_t = _highlight(desc, self._query, _TEXT if is_selected else _DIM)
        else:
            desc_t = Text(desc, style=_TEXT if is_selected else _DIM)

        if shadowed:
            desc_t.append("  ↳ shadowed", style=_FAINT)
        if shadows_builtin:
            desc_t.append("  ↳ overrides builtin", style=_FAINT)

        # In create mode: mark the agent whose name would be shadowed
        if (self._mode == "create" and self._create_name
                and manifest.name == self._create_name):
            name_t.append("  ← shadows", style=f"bold #7a3a26")

        # Tool count — always dim (selection signalled by caret + name color)
        tools = manifest.tools
        if tools is None:
            count_str = "all"
        else:
            count_str = str(len(tools))
        count_t = Text(count_str, style=_DIM, no_wrap=True)

        inner.add_row(ptr, name_t, desc_t, Text(""), count_t, style=row_style)

    def _add_confirm_strip_row(self, inner: Table) -> None:
        ptr = Text("▌  ", style=f"bold {_ORANGE}", no_wrap=True)
        msg = Text()
        msg.append("delete this agent?  ·  ", style=_ORANGE)
        msg.append(" d ", style=f"bold {_SILVER} on #2a2a2a")
        msg.append(" confirm  ·  ", style=_DIM)
        msg.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
        msg.append(" cancel", style=_DIM)
        inner.add_row(ptr, msg, Text(""), Text(""), style=f"on {_TINT_ORG}")

    def _build_list(self) -> Table:
        outer = Table.grid(expand=True, padding=0)
        outer.add_column(overflow="crop", no_wrap=True)

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

        name_w = min(max((len(m.name) for m in self._visible), default=8) + 2, 24)

        # When viewing all agents with no filter, always show all 3 tier sections
        # so users see the tier model even when some tiers are empty.
        _ALL_TIERS = ["builtin", "user", "project"]
        tiers_to_render = _ALL_TIERS if (self._scope == "all" and not self._query) else tiers_seen

        for i, tier in enumerate(tiers_to_render):
            if i > 0:
                outer.add_row(Text(""))
            outer.add_row(self._build_tier_header(tier))
            if tier not in by_tier:
                if self._scope == "all" and not self._query and tier in ("user", "project"):
                    hint = Text()
                    hint.append(f"   no {tier} agents", style=_DIM)
                    hint.append("  ·  press ", style=_FAINT)
                    hint.append(" n ", style=f"bold {_SILVER} on #2a2a2a")
                    hint.append(" to create one", style=_DIM)
                    outer.add_row(hint)
                else:
                    outer.add_row(Text(f"   no {tier} agents", style=_FAINT))
            else:
                inner = self._make_agent_row_table(name_w)
                # Column header row (ptr, name, desc, spacer, count)
                inner.add_row(
                    Text(""),
                    Text("name", style=_FAINT),
                    Text("description", style=_FAINT),
                    Text(""),
                    Text("tools", style=_FAINT),
                )
                for idx in by_tier[tier]:
                    manifest = self._visible[idx]
                    shadowed        = tier == "builtin" and manifest.name in shadow_builtins
                    shadows_builtin = tier == "project" and manifest.name in builtin_names
                    self._add_agent_inner_row(inner, manifest, idx, shadowed, shadows_builtin)
                    if self._mode == "confirm_delete" and idx == self._selected:
                        self._add_confirm_strip_row(inner)
                outer.add_row(inner)

        return outer

    # ── Preview / right pane ──────────────────────────────────────────────────

    def _build_preview(self) -> Table:
        agent = self._current_agent()
        if self._mode == "create":
            return self._build_preview_create()
        if self._mode == "confirm_delete" and agent:
            return self._build_preview_delete(agent)
        if self._mode == "duplicate" and agent:
            return self._build_preview_duplicate(agent)
        if self._mode == "run":
            return self._build_preview_run()
        if self._mode == "edit_color" and agent:
            return self._build_preview_color(agent)
        if self._mode == "edit_tools":
            return self._build_preview_tools()
        if self._mode == "edit_model":
            return self._build_preview_model()
        if self._mode == "edit_description":
            tbl = Table.grid(expand=True, padding=0)
            tbl.add_column()
            return tbl  # identity card shown in label above the TextArea
        if self._mode == "edit_iterations" and agent:
            return self._build_preview_iterations(agent)
        if self._mode == "edit_prompt":
            tbl = Table.grid(expand=True, padding=0)
            tbl.add_column()
            return tbl  # identity card shown in label above the TextArea
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

        # ── Identity band ─────────────────────────────────────────────────────
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # DESCRIPTION
        desc_header = Text()
        desc_header.append(" DESCRIPTION", style=f"bold {_DIM}")
        if manifest.source != "builtin":
            desc_header.append("  ")
            desc_header.append(" b ", style=f"bold {_SILVER} on #2a2a2a")
            desc_header.append(" edit", style=_DIM)
        tbl.add_row(desc_header)
        tbl.add_row(Text(f"   {manifest.description}", style=_TEXT))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # SOURCE
        tbl.add_row(Text(" SOURCE", style=f"bold {_DIM}"))
        tbl.add_row(Text(f"   {_format_source_path(manifest)}", style=_FAINT))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # ── Capabilities band ─────────────────────────────────────────────────
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # TOOLS
        tools_header = Text()
        tools_header.append(" TOOLS", style=f"bold {_DIM}")
        tools_count = manifest.tools
        if tools_count is None:
            tools_header.append(" · all allowed", style=_DIM)
        else:
            tools_header.append(f" · {len(tools_count)} of {len(_NATIVE_TOOLS)} allowed", style=_DIM)
        if manifest.source != "builtin":
            tools_header.append("  ")
            tools_header.append(" t ", style=f"bold {_SILVER} on #2a2a2a")
            tools_header.append(" edit", style=_DIM)
        tbl.add_row(tools_header)
        tbl.add_row(self._build_tools_section(manifest))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # MAX ITERATIONS
        iter_header = Text()
        iter_header.append(" MAX ITERATIONS", style=f"bold {_DIM}")
        if manifest.source != "builtin":
            iter_header.append("  ")
            iter_header.append(" i ", style=f"bold {_SILVER} on #2a2a2a")
            iter_header.append(" edit", style=_DIM)
        tbl.add_row(iter_header)
        tbl.add_row(Text(f"   {manifest.max_iterations}", style=_DIM))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # ── Behavior band ─────────────────────────────────────────────────────
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # MODEL
        model_header = Text()
        model_header.append(" MODEL", style=f"bold {_DIM}")
        if manifest.source != "builtin":
            model_header.append("  ")
            model_header.append(" m ", style=f"bold {_SILVER} on #2a2a2a")
            model_header.append(" edit", style=_DIM)
        tbl.add_row(model_header)
        if manifest.model:
            tbl.add_row(Text(f"   {manifest.model}", style=f"bold {_BLUE}"))
        else:
            tbl.add_row(Text("   inherit", style=_DIM))
            tbl.add_row(Text("   uses session model", style=_FAINT))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # COLOR
        color_header = Text()
        color_header.append(" COLOR", style=f"bold {_DIM}")
        if manifest.source != "builtin":
            color_header.append("  ")
            color_header.append(" k ", style=f"bold {_SILVER} on #2a2a2a")
            color_header.append(" edit", style=_DIM)
        tbl.add_row(color_header)
        if manifest.color:
            color_val = next((c for c, col, _ in _COLOR_OPTIONS if c == manifest.color), _FAINT)
            tbl.add_row(Text(f"   ● {manifest.color}", style=color_val))
        else:
            tier_default_name = {"builtin": "muted", "user": "gold", "project": "green"}.get(manifest.source, "inherit")
            tier_default_color = _tier_color(manifest.source)
            color_line = Text()
            color_line.append(f"   ● {tier_default_name}", style=tier_default_color)
            color_line.append(" (tier default)", style=_FAINT)
            tbl.add_row(color_line)
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # SYSTEM PROMPT
        prompt_header = Text()
        prompt_header.append(" SYSTEM PROMPT", style=f"bold {_DIM}")
        total_lines = len(manifest.system_prompt.splitlines())
        prompt_header.append(f"  ·  {total_lines} lines", style=_DIM)
        if manifest.source != "builtin":
            prompt_header.append("  ")
            prompt_header.append(" s ", style=f"bold {_SILVER} on #2a2a2a")
            prompt_header.append(" edit", style=_DIM)
        tbl.add_row(prompt_header)
        tbl.add_row(self._build_prompt_preview(manifest))

        # Shadowing precedence block for project agents that shadow a builtin
        if manifest.source == "project" and manifest.name in self._builtin_names():
            tbl.add_row(Text(""))
            tbl.add_row(self._build_precedence_block(manifest))

        return tbl

    def _build_tools_section(self, manifest: "AgentRoleManifest") -> Table:
        """Renders allowed/denied tool rows (no header — caller adds it)."""
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        tools = manifest.tools  # None = all, [] = none, [...] = subset

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
                    warn = _TOOL_WARN.get(tool, "")
                    if warn:
                        row.append(f"  {warn:<15}", style=f"bold {_ORANGE}")
                    else:
                        row.append("  " + " " * 15)
                    desc = _TOOL_DESCRIPTIONS.get(tool, "")
                    if desc:
                        row.append(desc, style=_FAINT)
                    tbl.add_row(row)

            # Denied list — vertical, one per line
            denied = [t for t in _NATIVE_TOOLS if t not in tools]
            if denied:
                tbl.add_row(Text(""))
                tbl.add_row(Text("  denied", style=_DIM))
                for tool in denied:
                    row = Text()
                    row.append(f"    {tool:<22}", style=_FAINT)
                    row.append("  " + " " * 15)
                    desc = _TOOL_DESCRIPTIONS.get(tool, "")
                    if desc:
                        row.append(desc, style=_FAINT)
                    tbl.add_row(row)

        return tbl

    def _build_prompt_preview(self, manifest: "AgentRoleManifest") -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        content_text = Text()
        for part in manifest.system_prompt.splitlines():
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

    def _build_preview_run(self) -> Table:
        """Agent context shown in the scrollable upper portion of run mode."""
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        agent = next(
            (m for m in self._registry.values() if m.name == self._run_agent_name),
            None,
        )

        # Identity bar — same style as edit screens (name + tier chip + timestamp)
        identity = Text()
        identity.append(f" {self._run_agent_name}", style=f"bold {_SILVER}")
        if agent:
            identity.append("  ")
            identity.append(f" {agent.source} ", style=f"bold {_tier_color(agent.source)} on #161614")
            if agent.source != "builtin" and agent.source_path:
                mtime = _age(agent.source_path)
                if mtime != "unknown":
                    identity.append(f"  ·  edited {mtime}", style=_DIM)
        tbl.add_row(identity)
        tbl.add_row(Text(""))

        # Description
        if agent and agent.description:
            tbl.add_row(Text(f"  {agent.description}", style=_TEXT))
            tbl.add_row(Text(""))

        # Compact context — R.3: keys=_FAINT (secondary), values=_TEXT (primary)
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # Tools (count only, not expanded list)
        if agent:
            if agent.tools is None:
                tools_str = f"all {len(_NATIVE_TOOLS)} native tools"
            else:
                tools_str = f"{len(agent.tools)} of {len(_NATIVE_TOOLS)} allowed"
        else:
            tools_str = "unknown"
        tools_row = Text()
        tools_row.append("  tools   ", style=_FAINT)
        tools_row.append(tools_str, style=_TEXT)
        tbl.add_row(tools_row)

        # Model
        model_row = Text()
        model_row.append("  model   ", style=_FAINT)
        if agent and agent.model:
            model_row.append(agent.model, style=_BLUE)
        else:
            model_row.append("inherit session model", style=_TEXT)
        tbl.add_row(model_row)

        # Iteration limit
        limit_row = Text()
        limit_row.append("  limit   ", style=_FAINT)
        limit_row.append(f"{agent.max_iterations if agent else 20} iterations", style=_TEXT)
        tbl.add_row(limit_row)

        # R.5: dispatch destination hint
        tbl.add_row(Text(""))
        dispatch_row = Text()
        dispatch_row.append("  dispatches as  ", style=_FAINT)
        dispatch_row.append("spawn_agent", style=_DIM)
        dispatch_row.append("  →  subagent worker", style=_FAINT)
        tbl.add_row(dispatch_row)

        return tbl

    def _build_prompt_edit_label(self, manifest: "AgentRoleManifest | None" = None) -> Text:
        """Section heading rendered above the system prompt TextArea."""
        t = Text()
        if manifest is not None:
            t.append(f" {manifest.name}", style=f"bold {_SILVER}")
            t.append("  ")
            t.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
            t.append("\n\n")
        t.append(" SYSTEM PROMPT", style=f"bold {_DIM}")
        t.append("\n")
        return t

    def _build_field_edit_label(self, label: str, manifest: "AgentRoleManifest | None" = None) -> Text:
        t = Text()
        if manifest is not None:
            t.append(f" {manifest.name}", style=f"bold {_SILVER}")
            t.append("  ")
            t.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
            t.append("\n\n")
        t.append(f" {label}", style=f"bold {_DIM}")
        t.append("\n")
        return t

    def _build_edit_hints(self) -> Text:
        """Key hints for field / prompt edit modes."""
        dot = f" [{_FAINT}]·[/] "
        parts = [_hint("ctrl+↵", "save"), _hint("esc", "cancel")]
        return Text.from_markup("  " + dot.join(parts))

    def _build_create_hints(self) -> Text:
        """Key hints strip shown inside the preview pane during create mode."""
        dot = f" [{_FAINT}]·[/] "
        is_armed = self._create_name_valid() and self._create_name_available()
        parts: list[str] = [_hint("tab / shift+tab", "next / prev field")]
        parts.append(_hint("↑↓", "switch option"))
        if is_armed:
            parts.append(f"[bold {_ORANGE} on #2a2a2a] ctrl+↵ [/] [{_ORANGE}]create & edit[/]")
        else:
            parts.append(f"[{_FAINT}]ctrl+↵  create & edit[/]")
        parts.append(_hint("esc", "cancel"))
        return Text.from_markup("  " + dot.join(parts))

    def _build_preview_edit_field(
        self,
        manifest: "AgentRoleManifest",
        label: str,
        current: str,
    ) -> Table:
        """Compact identity card shown above the field TextArea."""
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        header = Text()
        header.append(f" {manifest.name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        tbl.add_row(header)

        return tbl

    def _build_preview_edit_prompt(self, manifest: "AgentRoleManifest") -> Table:
        """Compact identity card shown above the system prompt TextArea."""
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        header = Text()
        header.append(f" {manifest.name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        total = len(manifest.system_prompt.splitlines())
        header.append(f"  ·  {total} lines", style=_DIM)
        tbl.add_row(header)

        return tbl

    def _build_preview_iterations(self, manifest: "AgentRoleManifest") -> Table:
        """Inline stepper for edit_iterations mode — ← / → to adjust, ↵ to save."""
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        # Identity bar
        header = Text()
        header.append(f" {manifest.name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        tbl.add_row(header)
        tbl.add_row(Text(""))

        tbl.add_row(Text(" MAX ITERATIONS", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))

        # Stepper row: [-]  N  [+]  · hint
        val = self._edit_iterations_val
        stepper = Text()
        stepper.append("   ")
        stepper.append(" ← ", style=f"bold {_SILVER} on #2a2a2a")
        stepper.append(f"  {val}  ", style=f"bold {_ORANGE}")
        stepper.append(" → ", style=f"bold {_SILVER} on #2a2a2a")
        stepper.append(f"  iterations", style=_DIM)
        tbl.add_row(stepper)
        tbl.add_row(Text(""))

        # Range note
        tbl.add_row(Text(f"   range 1 – 100", style=_FAINT))

        return tbl

    def _build_run_prompt_label(self) -> Text:
        """Section heading rendered above the run TextArea widget."""
        t = Text()
        t.append(" PROMPT", style=f"bold {_DIM}")
        return t

    def _build_run_hints(self) -> Text:
        """Key hints rendered below the run TextArea widget."""
        dot = f" [{_FAINT}]·[/] "
        parts = [
            _hint("ctrl+↵", "dispatch"),
            _hint("↵", "newline"),
            _hint("esc", "cancel"),
        ]
        return Text.from_markup("  " + dot.join(parts))

    def _build_preview_color(self, manifest: "AgentRoleManifest") -> Table:
        """Right-pane color picker for edit_color mode."""
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        hdr = Text()
        hdr.append(f" {manifest.name}", style=f"bold {_SILVER}")
        hdr.append("  ")
        hdr.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        tbl.add_row(hdr)
        tbl.add_row(Text(""))

        tbl.add_row(Text(" COLOR", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))

        intro = Text()
        intro.append(" Choose a display color for this agent.", style=_DIM)
        tbl.add_row(intro)
        intro2 = Text()
        intro2.append(" Used in chat output, the agents list, and Inspector.", style=_FAINT)
        tbl.add_row(intro2)
        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        current_color = manifest.color or "inherit"
        for i, (name, color_val, desc) in enumerate(_COLOR_OPTIONS):
            is_sel = i == self._edit_color_cursor
            row = Text()
            if is_sel:
                row.append("  ▸  ", style=f"bold {_ORANGE}")
                row.append("●● ", style=f"bold {color_val}")
                row.append(f"{name:<10}", style=f"bold {color_val}")
            else:
                row.append("     ", style="")
                row.append("●● ", style=color_val)
                row.append(f"{name:<10}", style=_DIM if name != current_color else _SILVER)
            if name == current_color and name != "inherit":
                row.append(" current  ", style=f"bold {_GREEN} on #0a1208")
            elif name == current_color:
                row.append(" current  ", style=f"{_DIM} on #161614")
            else:
                row.append("          ")
            row.append(desc, style=_FAINT)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")

        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))

        # Preview
        sel_name, sel_color, _ = _COLOR_OPTIONS[self._edit_color_cursor]
        effective_color = sel_color if sel_name != "inherit" else _tier_color(manifest.source)
        preview = Text()
        preview.append("  PREVIEW  ", style=_DIM)
        preview.append(" ▌ ", style=f"bold {effective_color}")
        preview.append(f"[{manifest.name}]", style=f"bold {effective_color}")
        preview.append("  done (4.1s)", style=_DIM)
        tbl.add_row(preview)

        return tbl

    def _build_preview_tools(self) -> Table:
        """Right-pane tools checklist for edit_tools mode."""
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        agent = self._current_agent()
        if agent:
            hdr = Text()
            hdr.append(f" {agent.name}", style=f"bold {_SILVER}")
            hdr.append("  ")
            hdr.append(f" {agent.source} ", style=f"bold {_tier_color(agent.source)} on #161614")
            tbl.add_row(hdr)
            tbl.add_row(Text(""))

        tbl.add_row(Text(" TOOLS", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))

        preamble = Text()
        preamble.append("  Toggle which tools this agent may call. Tools marked ", style=_DIM)
        preamble.append("⚠", style=_ORANGE)
        preamble.append(" have broad capability.", style=_DIM)
        tbl.add_row(preamble)
        tbl.add_row(Text(""))

        # Group tools by category in consistent order
        cat_order = ["filesystem", "shell", "network", "agents", "tasks", "other"]
        by_cat: dict[str, list[str]] = {}
        for tool in _NATIVE_TOOLS:
            cat = _TOOL_CATEGORIES.get(tool, "other")
            by_cat.setdefault(cat, []).append(tool)

        flat_idx = 0
        for cat in cat_order:
            if cat not in by_cat:
                continue
            sep = Text(f"  ─── {cat}", style=_FAINT)
            tbl.add_row(sep)
            for tool in by_cat[cat]:
                is_sel = flat_idx == self._edit_tools_cursor
                is_allowed = tool in self._edit_tools
                check = f"[{'✓' if is_allowed else ' '}]"
                row = Text()
                if is_sel:
                    row.append("  ▸ ", style=f"bold {_ORANGE}")
                else:
                    row.append("    ", style="")
                check_style = f"bold {_ORANGE}" if is_allowed else _DIM
                row.append(f"{check}  ", style=check_style)
                name_style = _TEXT if is_allowed else _DIM
                row.append(f"{tool:<20}   ", style=name_style)
                warn = _TOOL_WARN.get(tool, "")
                if warn:
                    row.append(f"{warn:<15}", style=f"bold {_ORANGE}")
                else:
                    row.append(" " * 15)
                desc = _TOOL_DESCRIPTIONS.get(tool, "")
                row.append(desc, style=_FAINT)
                tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")
                flat_idx += 1
            tbl.add_row(Text(""))

        tbl.add_row(Rule(style=_RULE))

        # Status bar
        allowed_count = len(self._edit_tools)
        denied_count = len(_NATIVE_TOOLS) - allowed_count
        changes = len(set(self._edit_tools).symmetric_difference(set(self._edit_tools_saved)))
        status = Text()
        if changes:
            status.append("  ● ", style=f"bold {_ORANGE}")
            status.append("unsaved  ", style=_ORANGE)
        else:
            status.append("  ○ ", style=_DIM)
            status.append("saved     ", style=_DIM)
        status.append(f" {allowed_count} allowed  ·  {denied_count} denied", style=_DIM)
        if changes:
            added   = [t for t in self._edit_tools if t not in self._edit_tools_saved]
            removed = [t for t in self._edit_tools_saved if t not in self._edit_tools]
            detail_parts = []
            if added:
                detail_parts.append(f"+ {', '.join(added[:3])}")
            if removed:
                detail_parts.append(f"− {', '.join(removed[:3])}")
            detail = f"  ({', '.join(detail_parts)})" if detail_parts else ""
            status.append(f"  ·  {changes} change{'s' if changes != 1 else ''} from saved{detail}", style=_ORANGE)
        tbl.add_row(status)

        return tbl

    def _build_preview_model(self) -> Table:
        """Right-pane model picker for edit_model mode."""
        from ...config.model_catalog import PROVIDERS, fmt_ctx, fmt_price

        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        agent = self._current_agent()
        if agent:
            hdr = Text()
            hdr.append(f" {agent.name}", style=f"bold {_SILVER}")
            hdr.append("  ")
            hdr.append(f" {agent.source} ", style=f"bold {_tier_color(agent.source)} on #161614")
            tbl.add_row(hdr)
            tbl.add_row(Text(""))

        tbl.add_row(Text(" MODEL", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))

        current_model = agent.model if agent else None

        # Current row
        current_h = Text()
        current_h.append("  CURRENT  ", style=_DIM)
        if current_model:
            current_h.append(" override ", style=f"bold {_ORANGE} on #1a0800")
            current_h.append(f"  ·  {current_model}", style=f"bold {_BLUE}")
        else:
            current_h.append(" inherit ", style=f"{_DIM} on #161614")
            current_h.append("  ·  uses session model", style=_DIM)
        tbl.add_row(current_h)
        tbl.add_row(Text(""))
        tbl.add_row(Text("  ─── pick a model", style=_FAINT))

        # Inherit option (index 0)
        is_sel = self._edit_model_cursor == 0
        inherit_row = Text()
        inherit_row.append("  ▸ " if is_sel else "    ", style=f"bold {_ORANGE}" if is_sel else "")
        inherit_row.append("● " if is_sel else "○ ", style=f"bold {_SILVER}" if is_sel else _DIM)
        inherit_row.append("inherit  ", style=f"bold {_SILVER}" if is_sel else _DIM)
        inherit_row.append("use the session model", style=_FAINT)
        if current_model is None:
            inherit_row.append("  ")
            inherit_row.append(" current ", style=f"bold {_GREEN} on #0a1208")
        tbl.add_row(inherit_row, style=f"on {_TINT_ORG}" if is_sel else "")

        # Provider/model options
        flat_idx = 1
        for provider in PROVIDERS:
            for model in provider["models"]:
                is_sel = flat_idx == self._edit_model_cursor
                is_cur = model["id"] == current_model
                row = Text()
                row.append("  ▸ " if is_sel else "    ", style=f"bold {_ORANGE}" if is_sel else "")
                row.append("● " if is_sel else "○ ", style=f"bold {_SILVER}" if is_sel else _DIM)
                row.append(f"{provider['name']} › ", style=_DIM)
                row.append(f"{model['id']}", style=f"bold {_BLUE}" if is_sel else _BLUE)
                ctx_str   = f"{fmt_ctx(model['ctx']):>4}"
                price_str = f"${model['in_price']:>6.2f}/${model['out_price']:>6.2f}"
                row.append(f"  {ctx_str} · {price_str}", style=_FAINT)
                if is_cur:
                    row.append("  ")
                    row.append(" current ", style=f"bold {_GREEN} on #0a1208")
                tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")
                flat_idx += 1

        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))

        # Preview
        sel_model = self._edit_model_flat[self._edit_model_cursor] if self._edit_model_flat else None
        preview = Text()
        preview.append("  PREVIEW  ", style=_DIM)
        if sel_model:
            preview.append(f"{sel_model}", style=f"bold {_BLUE}")
            preview.append("  ")
            preview.append(" override ", style=f"bold {_ORANGE} on #1a0800")
        else:
            preview.append("inherit ", style=_DIM)
            preview.append("· uses session model", style=_FAINT)
        tbl.add_row(preview)

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

    # ── Create form widget-content builders ────────────────────────────────────

    def _build_create_top(self) -> Text:
        """FROM header shown in #ag-dup-top for create mode."""
        t = Text()
        t.append(" FROM  ", style=_DIM)
        if self._create_starting_point == "blank":
            t.append("— blank slate —", style=f"bold {_ORANGE}")
        else:
            templates = self._get_template_list()
            if 0 <= self._create_template_cursor < len(templates):
                tmpl = templates[self._create_template_cursor]
                t.append(f" {tmpl.name} ", style=f"bold {_SILVER}")
                t.append(f" {tmpl.source} ", style=f"bold {_tier_color(tmpl.source)} on #161614")
                t.append(f"  ·  {_format_source_path(tmpl)}", style=_FAINT)
            else:
                t.append("— pick a template below —", style=_FAINT)
        t.append("\n\n")
        t.append("─── name ───────────────────────────────────────────────────", style=_DIM)
        return t

    def _build_create_validation(self) -> Text:
        """Validation line shown in #ag-dup-validation for create mode."""
        t = Text()
        if not self._create_name:
            t.append("  letters, digits, dashes  ·  2–40 chars", style=_FAINT)
        elif not self._create_name_valid():
            t.append("  ✗  invalid format", style=f"bold {_ORANGE}")
            t.append("  ·  letters, digits, dashes · must start with letter or digit",
                     style=_FAINT)
        elif not self._create_name_available():
            t.append("  ✗  name already exists", style=f"bold {_ORANGE}")
            conflict = next((m for m in self._registry.values()
                             if m.name == self._create_name), None)
            if conflict:
                t.append(f"  ·  conflict: {conflict.source}  "
                         f"{_format_source_path(conflict)}", style=_FAINT)
            suggestion = self._create_suggest()
            t.append("  ·  try ", style=_FAINT)
            t.append(suggestion, style=_DIM)
            t.append("  (tab)", style=_FAINT)
        else:
            t.append("  ✓  available", style=f"bold {_GREEN}")
            t.append(f"  ·  {self._create_target_path_preview(self._create_tier)}", style=_FAINT)
        return t

    def _build_create_desc_label(self) -> Text:
        """Label shown in #ag-run-prompt-label above the description TextArea in create mode."""
        t = Text()
        t.append("  ── description ────────────────────────────────────────────", style=_DIM)
        if self._create_desc_inherited:
            templates = self._get_template_list()
            if 0 <= self._create_template_cursor < len(templates):
                tmpl_name = templates[self._create_template_cursor].name
                t.append(f"\n  inherited from {tmpl_name}", style=_FAINT)
                t.append("  ·  edit to override", style=_FAINT)
        else:
            t.append("\n  optional — describes what this agent does", style=_FAINT)
        return t

    def _build_create_banner(self) -> Text:
        """Post-create undo banner shown in #ag-create-banner."""
        t = Text()
        name_display = self._create_undo_path.stem if self._create_undo_path else ""
        if self._create_undo_path:
            try:
                path_display = "~/" + str(self._create_undo_path.relative_to(Path.home()))
            except ValueError:
                path_display = str(self._create_undo_path)
        else:
            path_display = ""
        t.append("  ✓  created ", style=f"bold {_ORANGE}")
        t.append(name_display, style="bold")
        t.append(f"  ·  {path_display}", style=_FAINT)
        t.append("  ·  undo with ", style=_FAINT)
        t.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
        t.append(" within 5s", style=_FAINT)
        return t

    def _build_preview_create(self) -> Table:
        """Scroll content for create mode: tier + starting point + template picker + preview."""
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        tbl.add_row(Text(""))

        # ── Tier selector ─────────────────────────────────────────────────────
        tbl.add_row(Text(" ─── tier ─────────────────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        tier_focused = (self._create_focus == "tier")
        _unsel_dim = {"user": _GOLD_DIM, "project": _GREEN_DIM}
        for tier in ["user", "project"]:
            is_sel  = self._create_tier == tier
            bullet  = "●" if is_sel else "○"
            ptr_sty = f"bold {_ORANGE}" if (is_sel and tier_focused) else (_DIM if not is_sel else _SILVER)
            # Collides if name is typed, valid, unavailable, and the conflict is in this tier's path
            collides = (
                bool(self._create_name)
                and self._create_name_valid()
                and not self._create_name_available()
                and any(m.name == self._create_name and m.source == tier
                        for m in self._registry.values())
            )
            row = Text()
            row.append(f"  {'▸' if (is_sel and tier_focused) else ' '} {bullet} ", style=ptr_sty)
            row.append(f"{tier:<8}", style=f"bold {_tier_color(tier)}" if is_sel
                       else _unsel_dim.get(tier, _DIM))
            row.append(f"  {self._create_target_path_preview(tier)}", style=_FAINT)
            if collides:
                row.append("  ← collides", style=f"bold {_ORANGE}")
            tbl.add_row(row, style=f"on {_TINT_ORG}" if (is_sel and tier_focused) else "")
        tbl.add_row(Text(""))

        # ── Starting point ────────────────────────────────────────────────────
        tbl.add_row(Text(" ─── starting point ───────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        sp_focused = (self._create_focus == "starting_point")
        for option, label in [("blank",    "fresh agent · read-only filesystem tools"),
                               ("template", "copy fields from an existing agent")]:
            is_sel  = self._create_starting_point == option
            bullet  = "●" if is_sel else "○"
            ptr_sty = f"bold {_ORANGE}" if (is_sel and sp_focused) else ""
            row = Text()
            row.append(f"  {'▸' if (is_sel and sp_focused) else ' '} {bullet} ", style=ptr_sty)
            row.append(f"{option:<16}", style=f"bold {_SILVER}" if is_sel else _DIM)
            row.append(label, style=_TEXT if is_sel else _DIM)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if (is_sel and sp_focused) else "")

        # Template picker (only when starting_point == "template")
        if self._create_starting_point == "template":
            tbl.add_row(Text(""))
            tbl.add_row(Text("      ── pick a template ──────────────────────────────────",
                             style=_FAINT))
            templates = self._get_template_list()
            _tdim = {"builtin": _DIM, "user": _GOLD_DIM, "project": _GREEN_DIM}
            for i, tmpl in enumerate(templates):
                is_sel  = (i == self._create_template_cursor)
                ptr     = "  ▸" if is_sel else "   "
                row = Text()
                row.append(f"    {ptr} ", style=f"bold {_ORANGE}" if is_sel else _DIM)
                row.append(f"{tmpl.name:<20}", style=f"bold {_tier_color(tmpl.source)}"
                           if is_sel else _tdim.get(tmpl.source, _DIM))
                row.append(f"  {tmpl.source:<9}", style=_DIM)
                row.append(self._template_capability_summary(tmpl), style=_FAINT)
                tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")
        tbl.add_row(Text(""))

        # ── What will be created (preview block) ──────────────────────────────
        tbl.add_row(Text(" ─── what will be created ─────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        is_valid = self._create_name_valid() and self._create_name_available()

        def _prow(label: str, value: str, value_style: str = _TEXT) -> Text:
            row = Text()
            row.append("  ✓  " if is_valid else "  ·  ",
                       style=f"bold {_GREEN}" if is_valid else _DIM)
            row.append(f"{label:<16}", style=_DIM if is_valid else _FAINT)
            row.append(value, style=value_style if is_valid else _FAINT)
            return row

        if self._create_name:
            name_sty = (f"bold {_tier_color(self._create_tier)}" if is_valid
                        else f"strike {_FAINT}")
            tbl.add_row(_prow("name", self._create_name, name_sty))
        else:
            ph = Text()
            ph.append("  ·  ", style=_DIM)
            ph.append("name            ", style=_DIM)
            ph.append("<name>", style=_FAINT)
            tbl.add_row(ph)

        tier_str = f"{self._create_tier}  ·  {self._create_target_path_preview(self._create_tier)}"
        tbl.add_row(_prow("tier", tier_str, f"bold {_tier_color(self._create_tier)}"))

        if self._create_starting_point == "blank":
            desc_val = self._create_desc.strip()
            desc_disp = (desc_val[:35] + "…") if len(desc_val) > 35 else desc_val
            tbl.add_row(_prow("description", desc_disp if desc_disp else "scaffolded if empty"))
            tbl.add_row(_prow("tools",
                              "read_file · list_directory · search_file · get_file_outline"))
            tbl.add_row(_prow("model", "inherit", _BLUE))
            tbl.add_row(_prow("max_iterations", "20"))
            color_name = "gold" if self._create_tier == "user" else "green"
            color_hex  = _GOLD if self._create_tier == "user" else _GREEN
            tbl.add_row(_prow("color", f"● {color_name} (tier default)", color_hex))
            prompt_name = self._create_name or "<name>"
            tbl.add_row(_prow("system prompt", f"# {prompt_name}" + (" + description" if self._create_desc.strip() else " only")))
        else:
            templates = self._get_template_list()
            if 0 <= self._create_template_cursor < len(templates):
                tmpl = templates[self._create_template_cursor]
                # Description row — show inherited prefix if not overridden
                desc_src = self._create_desc.strip() or tmpl.description
                desc_disp = (desc_src[:35] + "…") if len(desc_src) > 35 else desc_src
                t_desc = Text()
                t_desc.append("  ✓  " if is_valid else "  ·  ",
                               style=f"bold {_GREEN}" if is_valid else _DIM)
                t_desc.append("description     ", style=_DIM if is_valid else _FAINT)
                if self._create_desc_inherited:
                    t_desc.append("inherited — ", style=f"italic {_DIM}" if is_valid else _FAINT)
                t_desc.append(desc_disp, style=_TEXT if is_valid else _FAINT)
                tbl.add_row(t_desc)
                # Tools
                tool_count = len(tmpl.tools) if tmpl.tools is not None else len(_NATIVE_TOOLS)
                t_tools = Text()
                t_tools.append("  ✓  " if is_valid else "  ·  ",
                                style=f"bold {_GREEN}" if is_valid else _DIM)
                t_tools.append("tools           ", style=_DIM if is_valid else _FAINT)
                t_tools.append("copied — ", style=f"italic {_DIM}" if is_valid else _FAINT)
                t_tools.append(f"{tool_count} tools", style=_TEXT if is_valid else _FAINT)
                tbl.add_row(t_tools)
                # Model
                model_val = tmpl.model or "inherit"
                model_sty = _BLUE if tmpl.model is None else _TEXT
                t_model = Text()
                t_model.append("  ✓  " if is_valid else "  ·  ",
                                style=f"bold {_GREEN}" if is_valid else _DIM)
                t_model.append("model           ", style=_DIM if is_valid else _FAINT)
                t_model.append("copied — ", style=f"italic {_DIM}" if is_valid else _FAINT)
                t_model.append(model_val, style=model_sty if is_valid else _FAINT)
                tbl.add_row(t_model)
                tbl.add_row(_prow("max_iterations", str(tmpl.max_iterations)))
                # System prompt
                prompt_lines = len(tmpl.system_prompt.splitlines())
                t_prompt = Text()
                t_prompt.append("  ✓  " if is_valid else "  ·  ",
                                 style=f"bold {_GREEN}" if is_valid else _DIM)
                t_prompt.append("system prompt   ", style=_DIM if is_valid else _FAINT)
                t_prompt.append("copied — ", style=f"italic {_DIM}" if is_valid else _FAINT)
                t_prompt.append(f"{prompt_lines} lines from {tmpl.name}",
                                 style=_TEXT if is_valid else _FAINT)
                tbl.add_row(t_prompt)
            else:
                tbl.add_row(Text("  pick a template above", style=_FAINT))

        if not is_valid and self._create_name:
            tbl.add_row(Text(""))
            tbl.add_row(Text("  △ resolve the name to continue", style=f"bold {_ORANGE}"))
        else:
            tbl.add_row(Text(""))
            note = Text()
            note.append("  ·  ", style=_DIM)
            note.append("opens in detail view", style=_FAINT)
            note.append("  —  ", style=_FAINT)
            note.append("model, system prompt, tools, color", style=_DIM)
            note.append(" editable there", style=_FAINT)
            tbl.add_row(note)

        return tbl

    def _build_dup_top(self, manifest: "AgentRoleManifest") -> Text:
        """FROM line + new-name section heading rendered above the Input widget."""
        t = Text()
        t.append(" FROM  ", style=_DIM)
        t.append(f"  {manifest.name}  ", style=f"bold {_SILVER}")
        t.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        if manifest.source == "builtin":
            # D.11: call out read-only constraint so the user knows why they're duplicating
            t.append("  ·  read-only", style=_FAINT)
        t.append(f"  ·  {_format_source_path(manifest)}", style=_FAINT)
        t.append("\n\n")
        t.append("─── new name ───────────────────────────────────────────────", style=_DIM)
        return t

    def _build_dup_validation(self) -> Text:
        """Validation line rendered immediately below the Input widget.

        Glyph rule: ✗ = validation error (name conflict, missing field)
                    △ = capability/risk warning (destructive, read-only, recursion)
        """
        t = Text()
        if not self._dup_name:
            t.append("  enter a unique name", style=_FAINT)
        elif self._dup_name_available():
            t.append("  ✓  available", style=f"bold {_GREEN}")
        else:
            t.append("  ✗  name already exists", style=f"bold {_ORANGE}")
            suggestion = self._dup_suggest()
            t.append("  ·  try ", style=_FAINT)
            t.append(suggestion, style=_DIM)
            t.append("  (tab)", style=_FAINT)
        return t

    def _build_preview_duplicate(self, manifest: "AgentRoleManifest") -> Table:
        """Scrollable portion of the dup form: tier selector + checklist + result.

        The FROM line, name heading, Input widget, and validation line are
        rendered as separate widgets above this scroll area (see _build_dup_top
        and _build_dup_validation), so this method starts at the tier selector.
        """
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        tbl.add_row(Text(""))

        # Tier selector — D.6: unselected project label uses _GREEN_DIM
        tbl.add_row(Text(" ─── target tier ─────────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        for tier in ["user", "project"]:
            is_sel = self._dup_tier == tier
            tier_focused = self._dup_focus == "tier"
            bullet = "●" if is_sel else "○"
            ptr_style = f"bold {_ORANGE}" if (is_sel and tier_focused) else (_DIM if not is_sel else _SILVER)
            _unsel_dim = {"user": _GOLD_DIM, "project": _GREEN_DIM}
            row = Text()
            row.append(f"  {'▸' if (is_sel and tier_focused) else ' '} {bullet} ", style=ptr_style)
            row.append(f"{tier:<8}", style=f"bold {_tier_color(tier)}" if is_sel else _unsel_dim.get(tier, _DIM))
            row.append(f"  {self._dup_target_path_preview(tier)}", style=_FAINT)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if (is_sel and tier_focused) else "")
        tbl.add_row(Text(""))

        # What gets copied — D.8: exhaustive checklist covering every field.
        # All fields are copied verbatim because _do_duplicate() rewrites the raw
        # YAML changing only the name key.
        tbl.add_row(Text(" ─── what gets copied ────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        tools = manifest.tools
        tools_str = f"{len(tools)} tools" if tools is not None else "all native tools"
        prompt_lines = len(manifest.system_prompt.splitlines())
        desc_preview = (manifest.description[:30] + "…") if len(manifest.description) > 30 else manifest.description
        model_val = manifest.model if manifest.model else "inherit"
        color_val = manifest.color if manifest.color else "tier default"
        checklist = [
            ("description",    desc_preview or "—"),
            ("tools",          tools_str),
            ("system prompt",  f"{prompt_lines} lines"),
            ("max_iterations", str(manifest.max_iterations)),
            ("model",          model_val),
            ("color",          color_val),
        ]
        for item, value in checklist:
            row = Text()
            row.append("  ✓  ", style=f"bold {_GREEN}")
            row.append(f"{item:<16}", style=_DIM)
            row.append(value, style=_FAINT)
            tbl.add_row(row)
        tbl.add_row(Text(""))

        # Result preview — D.4: always rendered; struck-through on conflict.
        # D.10: use ── result ── rule to match other section headings.
        avail = self._dup_name_available()
        tbl.add_row(Text(" ─── result ──────────────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        result = Text()
        name_display = self._dup_name if self._dup_name else "<name>"
        path_display = self._dup_target_path_preview(self._dup_tier)
        if avail:
            result.append(f" new {self._dup_tier} agent  ", style=_DIM)
            result.append(name_display, style=f"bold {_tier_color(self._dup_tier)}")
            result.append(f"  at  {path_display}", style=_FAINT)
            result.append("  ·  opens in detail", style=_DIM)
        elif self._dup_name:
            # D.4: struck-through result on conflict — no layout shift
            result.append(f" new {self._dup_tier} agent  ", style=f"strike {_FAINT}")
            result.append(name_display, style=f"strike {_ORANGE}")
            result.append(f"  at  {path_display}", style=f"strike {_FAINT}")
            result.append("  ·  resolve name conflict first", style=_FAINT)
        else:
            result.append("fill in name above to preview", style=_FAINT)
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
                _hint("tab", "next field / accept suggestion"),
                _hint("↑↓", "switch tier"),
                _hint("↵", "create & edit"),
                _hint("esc", "cancel"),
            ]
            suffix = ""
        elif self._mode == "run":
            hints = [
                _hint("ctrl+↵", "dispatch agent"),
                _hint("↵", "newline"),
                _hint("esc", "cancel"),
            ]
            suffix = f"  [{_FAINT}]ctrl+o opens Inspector after dispatch[/]"
        elif self._mode == "edit_tools":
            hints = [
                _hint("↑↓", "nav"),
                _hint("space", "toggle"),
                _hint("a", "select all in category"),
                _hint("↵", "save & close"),
                _hint("esc", "cancel"),
            ]
            suffix = ""
        elif self._mode == "edit_model":
            hints = [
                _hint("↑↓", "nav"),
                _hint("↵", "select & save"),
                _hint("esc", "cancel"),
            ]
            suffix = ""
        elif self._mode == "edit_color":
            hints = [
                _hint("↑↓", "nav"),
                _hint("↵", "save & close"),
                _hint("esc", "cancel"),
            ]
            suffix = ""
        elif self._mode == "edit_description":
            hints = [_hint("↵", "save"), _hint("esc", "cancel")]
            suffix = ""
        elif self._mode == "edit_iterations":
            hints = [_hint("←→", "adjust"), _hint("↵", "save"), _hint("esc", "cancel")]
            suffix = ""
        elif self._mode == "edit_prompt":
            hints = [
                _hint("ctrl+↵", "save"),
                _hint("↵", "newline"),
                _hint("esc", "cancel"),
            ]
            suffix = ""
        elif self._mode == "detail":
            bar = f"  [{_FAINT}]|[/]  "
            nav_h  = dot.join([_hint("↑↓", "scroll")])
            if not is_builtin:
                edit_h = dot.join([
                    _hint("b", "desc"), _hint("i", "iter"), _hint("t", "tools"),
                    _hint("m", "model"), _hint("k", "color"), _hint("s", "prompt"),
                ])
                act_h  = dot.join([_hint("r", "run"), _hint("y", "dup"), _hint("d", "delete")])
            else:
                edit_h = ""
                act_h  = dot.join([_hint("r", "run"), _hint("y", "dup")])
            exit_h = dot.join([_hint("esc", "back")])
            parts = [nav_h]
            if edit_h:
                parts.append(edit_h)
            parts += [act_h, exit_h]
            t = Text.from_markup("  " + bar.join(parts))
            return t
        elif self._mode == "create":
            is_armed = self._create_name_valid() and self._create_name_available()
            hints = [
                _hint("tab / shift+tab", "next / prev field"),
                _hint("↑↓", "switch option"),
            ]
            if is_armed:
                hints.append(f"[bold {_ORANGE} on #2a2a2a] ctrl+↵ [/] [{_ORANGE}]create & edit[/]")
            else:
                hints.append(f"[{_FAINT}]ctrl+↵  create & edit[/]")
            hints.append(_hint("esc", "cancel"))
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
                f"[bold {_ORANGE} on #2a2a2a] n [/] [{_ORANGE}]new[/]",
                _hint("r", "run"),
                _hint("y", "dup"),
            ]
            if not is_builtin:
                hints += [_hint("d", "delete")]
            hints.append(_hint("esc", "close"))
            suffix = "" if not is_builtin else f"  [{_FAINT}]read-only — edit/delete hidden[/]"

        t = Text.from_markup("  " + dot.join(hints) + (suffix or ""))
        return t

    # ── Actions (BINDINGS) ────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple) -> bool | None:  # type: ignore[override]
        return True

    def action_nav_up(self) -> None:
        if self._mode in ("edit_prompt", "edit_description") and isinstance(self.focused, TextArea):
            self.query_one("#ag-run-input", TextArea).action_cursor_up()
            return
        if self._mode == "create":
            if self._create_focus == "tier":
                self._create_tier = "user"
                self._refresh()
            elif self._create_focus == "starting_point":
                self._create_starting_point = "blank"
                self._refresh()
            elif self._create_focus == "template_picker":
                self._create_template_cursor = max(0, self._create_template_cursor - 1)
                self._sync_template_selection()
                self._refresh()
            return
        if self._mode == "edit_tools":
            self._edit_tools_cursor = max(0, self._edit_tools_cursor - 1)
            self._refresh()
            return
        if self._mode == "edit_model":
            self._edit_model_cursor = max(0, self._edit_model_cursor - 1)
            self._refresh()
            return
        if self._mode == "edit_color":
            self._edit_color_cursor = max(0, self._edit_color_cursor - 1)
            self._refresh()
            return
        if self._focus_pane == "detail":
            self.query_one("#ag-preview-scroll", VerticalScroll).scroll_relative(y=-3)
            return
        if self._mode == "duplicate" and self._dup_focus == "tier":
            self._dup_tier = "user"
            self._refresh()
            return
        if self._mode in ("confirm_delete", "duplicate", "run"):
            return
        if self._visible:
            self._selected = max(0, self._selected - 1)
            self._refresh()

    def action_nav_down(self) -> None:
        if self._mode in ("edit_prompt", "edit_description") and isinstance(self.focused, TextArea):
            self.query_one("#ag-run-input", TextArea).action_cursor_down()
            return
        if self._mode == "create":
            if self._create_focus == "tier":
                self._create_tier = "project"
                self._refresh()
            elif self._create_focus == "starting_point":
                self._create_starting_point = "template"
                self._create_focus = "template_picker"
                self._sync_template_selection()
                self._refresh()
            elif self._create_focus == "template_picker":
                templates = self._get_template_list()
                self._create_template_cursor = min(
                    len(templates) - 1, self._create_template_cursor + 1
                )
                self._sync_template_selection()
                self._refresh()
            return
        if self._mode == "edit_tools":
            self._edit_tools_cursor = min(len(_NATIVE_TOOLS) - 1, self._edit_tools_cursor + 1)
            self._refresh()
            return
        if self._mode == "edit_model":
            max_idx = len(self._edit_model_flat) - 1 if self._edit_model_flat else 0
            self._edit_model_cursor = min(max_idx, self._edit_model_cursor + 1)
            self._refresh()
            return
        if self._mode == "edit_color":
            self._edit_color_cursor = min(len(_COLOR_OPTIONS) - 1, self._edit_color_cursor + 1)
            self._refresh()
            return
        if self._focus_pane == "detail":
            self.query_one("#ag-preview-scroll", VerticalScroll).scroll_relative(y=3)
            return
        if self._mode == "duplicate" and self._dup_focus == "tier":
            self._dup_tier = "project"
            self._refresh()
            return
        if self._mode in ("confirm_delete", "duplicate", "run"):
            return
        if self._visible:
            self._selected = min(len(self._visible) - 1, self._selected + 1)
            self._refresh()

    def action_esc_action(self) -> None:
        if self._create_undo_active:
            self._do_undo_create()
            return
        edit_modes = ("confirm_delete", "duplicate", "detail", "run",
                      "edit_tools", "edit_model", "edit_color",
                      "edit_description", "edit_iterations", "edit_prompt", "create")
        if self._mode in edit_modes:
            was_create = (self._mode == "create")
            self._mode = "browse"
            self._del_confirmed = False
            self._dup_name = ""
            self._dup_focus = "name"
            self._focus_pane = "list"
            if was_create:
                self._reset_create_state()
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
        # Create mode: Enter inserts newline in textarea or advances from name input.
        # Submission is via ctrl+enter (handled in on_key).
        if self._mode == "create":
            focused = self.focused
            if isinstance(focused, TextArea) and focused.id == "ag-run-input":
                focused.insert("\n")
                return
            if isinstance(focused, Input) and focused.id == "ag-dup-name":
                if self._create_name_available():
                    self._create_focus = "description"
                    self._update_create_widget_focus()
                    self._refresh()
                return
            return
        # Stepper: Enter saves iterations directly
        if self._mode == "edit_iterations":
            self._do_save_iterations()
            return
        # TextArea modes: Enter inserts newline (ctrl+↵ saves)
        if self._mode in ("run", "edit_prompt", "edit_description") \
                and isinstance(self.focused, TextArea):
            self.query_one("#ag-run-input", TextArea).insert("\n")
            return
        if self._mode == "edit_tools":
            self._do_save_tools()
        elif self._mode == "edit_model":
            self._do_save_model()
        elif self._mode == "edit_color":
            self._do_save_color()
        elif self._mode == "duplicate":
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
            pass  # Enter in detail mode: no-op (run is 'r', edit via e/t/m/k/s)

    def action_confirm_primary(self) -> None:
        """ctrl+enter / ctrl+j — submit create, save prompt/description, dispatch run."""
        mode = self._mode
        if mode == "create":
            if self._create_name_valid() and self._create_name_available():
                self._do_create()
        elif mode == "edit_prompt":
            self._do_save_prompt()
        elif mode == "edit_description":
            self._do_save_description()
        elif mode == "run":
            run_area = self.query_one("#ag-run-input", TextArea)
            prompt = run_area.text.strip().replace("\n", " ")
            if prompt:
                self.dismiss(f"/agent {self._run_agent_name} {prompt}")

    def action_cycle_scope(self) -> None:
        if self._mode == "create":
            order = ["name", "description", "tier", "starting_point"]
            if self._create_starting_point == "template":
                sp_idx = order.index("starting_point")
                order.insert(sp_idx + 1, "template_picker")
            curr = self._create_focus if self._create_focus in order else "name"
            # Tab when name conflicts: accept suggestion instead of advancing
            if curr == "name" and not self._create_name_available() and self._create_name_valid():
                focused = self.focused
                if isinstance(focused, Input) and focused.id == "ag-dup-name":
                    suggestion = self._create_suggest()
                    focused.value = suggestion
                    self._create_name = suggestion
                    self._refresh()
                    return
            next_focus = order[(order.index(curr) + 1) % len(order)]
            self._create_focus = next_focus
            self._update_create_widget_focus()
            self._refresh()
            return
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

    def action_cycle_scope_back(self) -> None:
        if self._mode == "create":
            order = ["name", "description", "tier", "starting_point"]
            if self._create_starting_point == "template":
                sp_idx = order.index("starting_point")
                order.insert(sp_idx + 1, "template_picker")
            curr = self._create_focus if self._create_focus in order else "name"
            prev_focus = order[(order.index(curr) - 1) % len(order)]
            self._create_focus = prev_focus
            self._update_create_widget_focus()
            self._refresh()
            return
        if self._mode == "duplicate":
            if self._dup_focus == "tier":
                self._dup_focus = "name"
                self.query_one("#ag-dup-name", Input).focus()
            else:
                self._dup_focus = "tier"
                self.query_one("#ag-panel", Vertical).focus()
            self._refresh()
            return

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
                prev_mode = self._mode
                self._mode = "browse"
                if prev_mode == "create":
                    self._reset_create_state()
                else:
                    self._dup_name = ""
                    self._dup_focus = "name"
                self.query_one("#ag-panel", Vertical).focus()
                self._refresh()
                event.stop()
            elif (
                key == "tab"
                and focused.id == "ag-dup-name"
                and self._mode == "duplicate"
                and self._dup_name
                and not self._dup_name_available()
            ):
                # D.5: tab in conflict state accepts the auto-suggested non-colliding name
                suggestion = self._dup_suggest()
                focused.value = suggestion
                self._dup_name = suggestion
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

        # edit_tools: space to toggle, a to select-all-in-category
        if mode == "edit_tools":
            if key == "space":
                self._toggle_tool_at_cursor()
                event.stop()
            elif key == "a":
                self._toggle_category_at_cursor()
                event.stop()
            return

        # Run mode: all other keys ignored (ctrl+enter handled by action_confirm_primary)
        if mode == "run":
            return

        # edit_iterations: stepper — ← → adjust, ↵ saves (handled by action_confirm)
        if mode == "edit_iterations":
            if key == "left":
                self._edit_iterations_val = max(1, self._edit_iterations_val - 1)
                self._refresh()
                event.stop()
            elif key == "right":
                self._edit_iterations_val = min(100, self._edit_iterations_val + 1)
                self._refresh()
                event.stop()
            return

        # edit_prompt / edit_description / create: ctrl+enter handled by action_confirm_primary
        if mode in ("edit_prompt", "edit_description", "create"):
            return

        # edit_color / edit_model: only esc/enter handled via BINDINGS
        if mode in ("edit_color", "edit_model"):
            return

        # Browse / search / detail modes
        agent = self._current_agent()
        is_builtin = agent is not None and agent.source == "builtin"

        if key == "n":
            self._start_create()
            event.stop()
        elif key == "d":
            self._start_delete()
            event.stop()
        elif key == "y":
            self._start_duplicate()
            event.stop()
        elif key == "r":
            self._action_run()
            event.stop()
        elif key == "t" and not is_builtin:
            self._start_edit_tools()
            event.stop()
        elif key == "m" and not is_builtin:
            self._start_edit_model()
            event.stop()
        elif key == "k" and not is_builtin:
            self._start_edit_color()
            event.stop()
        elif key == "b" and not is_builtin:
            self._start_edit_description()
            event.stop()
        elif key == "i" and not is_builtin:
            self._start_edit_iterations()
            event.stop()
        elif key == "s" and not is_builtin:
            self._start_edit_prompt()
            event.stop()
        elif key == "slash":
            self.query_one("#ag-search", ModalSearchBar).focus_input()
            self._mode = "search"
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
            if self._mode == "create":
                self._create_name = event.value.strip()
            else:
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
            if self._mode == "create":
                if self._create_name_available():
                    self._create_focus = "tier"
                    self._update_create_widget_focus()
                    self._refresh()
            elif self._dup_name_available():
                self._dup_focus = "tier"
                self.query_one("#ag-panel", Vertical).focus()
                self._refresh()
        else:
            self.query_one("#ag-panel", Vertical).focus()
            self._mode = "browse" if not self._query else "search"
            self._refresh()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._mode == "create" and event.text_area.id == "ag-run-input":
            self._create_desc = event.text_area.text
            if self._create_desc_inherited:
                self._create_desc_inherited = False
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

    # ── Create new agent ──────────────────────────────────────────────────────

    def _start_create(self) -> None:
        self._reset_create_state()
        self._mode = "create"
        self._refresh()
        inp = self.query_one("#ag-dup-name", Input)
        inp.value = ""
        inp.focus()

    def _reset_create_state(self) -> None:
        self._create_name = ""
        self._create_tier = "user"
        self._create_desc = ""
        self._create_desc_inherited = False
        self._create_starting_point = "blank"
        self._create_template_cursor = 0
        self._create_focus = "name"
        # Clear the TextArea content if the widget is already mounted
        try:
            ta = self.query_one("#ag-run-input", TextArea)
            ta.load_text("")
        except Exception:
            pass

    def _do_create(self) -> None:
        name = self._create_name
        tier = self._create_tier
        desc = self._create_desc.strip()

        if self._create_starting_point == "blank":
            tools: Optional[list[str]] = list(_BLANK_TOOLS)
            model: Optional[str] = None
            max_iterations = 20
            system_prompt = self._create_scaffold_prompt(name, desc)
        else:
            templates = self._get_template_list()
            if not (0 <= self._create_template_cursor < len(templates)):
                return
            tmpl = templates[self._create_template_cursor]
            tools = list(tmpl.tools) if tmpl.tools is not None else None
            model = tmpl.model
            max_iterations = tmpl.max_iterations
            system_prompt = tmpl.system_prompt
            if not desc:
                desc = tmpl.description

        desc_val = desc if desc else "a helpful assistant."
        raw: dict[str, object] = {
            "name": name,
            "description": desc_val,
            "system_prompt": system_prompt,
            "max_iterations": max_iterations,
        }
        if tools is not None:
            raw["tools"] = tools
        if model:
            raw["model"] = model

        target = self._create_target_path(tier)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(
                yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            tmp.rename(target)
        except OSError:
            return

        self._registry_changed = True
        self._create_undo_path = target
        self._create_undo_active = True

        self._reload_registry()
        new_idx = next(
            (i for i, m in enumerate(self._visible) if m.name == name and m.source == tier),
            0,
        )
        self._selected = new_idx
        self._mode = "browse"
        self._focus_pane = "detail"
        self._reset_create_state()
        self.query_one("#ag-panel", Vertical).focus()
        self._refresh()
        self.set_timer(5.0, self._expire_undo)

    def _expire_undo(self) -> None:
        self._create_undo_active = False
        self._create_undo_path = None
        self._refresh()

    def _do_undo_create(self) -> None:
        if not self._create_undo_active or self._create_undo_path is None:
            return
        try:
            self._create_undo_path.unlink()
        except OSError:
            pass
        self._create_undo_active = False
        self._create_undo_path = None
        self._registry_changed = True
        self._reload_registry()
        self._selected = min(self._selected, max(0, len(self._visible) - 1))
        self._refresh()

    # ── Phase 3: Run ──────────────────────────────────────────────────────────

    def _action_run(self) -> None:
        agent = self._current_agent()
        if agent is None:
            return
        self._run_agent_name = agent.name
        self._mode = "run"
        self._focus_pane = "detail"
        self._refresh()
        run_area = self.query_one("#ag-run-input", TextArea)
        run_area.clear()
        run_area.focus()

    # ── Phase 4: Edit color ────────────────────────────────────────────────────

    def _start_edit_color(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin":
            return
        current = agent.color or "inherit"
        self._edit_color_cursor = next(
            (i for i, (name, _, _) in enumerate(_COLOR_OPTIONS) if name == current),
            len(_COLOR_OPTIONS) - 1,  # default to inherit
        )
        self._mode = "edit_color"
        self._focus_pane = "detail"
        self._refresh()

    def _do_save_color(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source_path is None or agent.source == "builtin":
            return
        sel_name, _, _ = _COLOR_OPTIONS[self._edit_color_cursor]
        from ...agents.persist import update_agent_yaml
        try:
            if sel_name == "inherit":
                update_agent_yaml(agent.source_path, {"color": None})
            else:
                update_agent_yaml(agent.source_path, {"color": sel_name})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self._refresh()

    # ── Phase 4: Edit tools ────────────────────────────────────────────────────

    def _start_edit_tools(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin":
            return
        self._edit_tools = list(agent.tools) if agent.tools is not None else list(_NATIVE_TOOLS)
        self._edit_tools_saved = list(self._edit_tools)
        self._edit_tools_cursor = 0
        self._mode = "edit_tools"
        self._focus_pane = "detail"
        self._refresh()

    def _toggle_tool_at_cursor(self) -> None:
        flat_idx = 0
        cat_order = ["filesystem", "shell", "network", "agents", "tasks", "other"]
        by_cat: dict[str, list[str]] = {}
        for tool in _NATIVE_TOOLS:
            cat = _TOOL_CATEGORIES.get(tool, "other")
            by_cat.setdefault(cat, []).append(tool)
        for cat in cat_order:
            if cat not in by_cat:
                continue
            for tool in by_cat[cat]:
                if flat_idx == self._edit_tools_cursor:
                    if tool in self._edit_tools:
                        self._edit_tools.remove(tool)
                    else:
                        self._edit_tools.append(tool)
                    self._refresh()
                    return
                flat_idx += 1

    def _toggle_category_at_cursor(self) -> None:
        flat_idx = 0
        cat_order = ["filesystem", "shell", "network", "agents", "tasks", "other"]
        by_cat: dict[str, list[str]] = {}
        for tool in _NATIVE_TOOLS:
            cat = _TOOL_CATEGORIES.get(tool, "other")
            by_cat.setdefault(cat, []).append(tool)
        for cat in cat_order:
            if cat not in by_cat:
                continue
            tools_in_cat = by_cat[cat]
            for tool in tools_in_cat:
                if flat_idx == self._edit_tools_cursor:
                    all_allowed = all(t in self._edit_tools for t in tools_in_cat)
                    if all_allowed:
                        for t in tools_in_cat:
                            if t in self._edit_tools:
                                self._edit_tools.remove(t)
                    else:
                        for t in tools_in_cat:
                            if t not in self._edit_tools:
                                self._edit_tools.append(t)
                    self._refresh()
                    return
                flat_idx += 1

    def _do_save_tools(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source_path is None or agent.source == "builtin":
            return
        from ...agents.persist import update_agent_yaml
        tools_to_save: Optional[list[str]] = self._edit_tools
        if sorted(self._edit_tools) == sorted(_NATIVE_TOOLS):
            tools_to_save = None  # save as null → all tools
        try:
            update_agent_yaml(agent.source_path, {"tools": tools_to_save})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self._refresh()

    # ── Phase 4: Edit model ────────────────────────────────────────────────────

    def _start_edit_model(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin":
            return
        from ...config.model_catalog import PROVIDERS
        flat: list[Optional[str]] = [None]
        flat.extend(m["id"] for p in PROVIDERS for m in p["models"])
        self._edit_model_flat = flat
        current = agent.model
        try:
            self._edit_model_cursor = self._edit_model_flat.index(current)
        except ValueError:
            self._edit_model_cursor = 0
        self._mode = "edit_model"
        self._focus_pane = "detail"
        self._refresh()

    def _do_save_model(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source_path is None or agent.source == "builtin":
            return
        sel = self._edit_model_flat[self._edit_model_cursor] if self._edit_model_flat else None
        from ...agents.persist import update_agent_yaml
        try:
            update_agent_yaml(agent.source_path, {"model": sel})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self._refresh()

    # ── Field editors (description / iterations / system prompt) ──────────────

    def _start_edit_description(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin" or agent.source_path is None:
            return
        ta = self.query_one("#ag-run-input", TextArea)
        ta.load_text(agent.description)
        self._mode = "edit_description"
        self._focus_pane = "detail"
        self._refresh()
        ta.focus()

    def _do_save_description(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin" or agent.source_path is None:
            return
        new_val = self.query_one("#ag-run-input", TextArea).text.strip().replace("\n", " ")
        if new_val:
            from ...agents.persist import update_agent_yaml
            update_agent_yaml(agent.source_path, {"description": new_val})
            self._registry_changed = True
            self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self.query_one("#ag-panel", Vertical).focus()
        self._refresh()

    def _start_edit_iterations(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin" or agent.source_path is None:
            return
        self._edit_iterations_val = agent.max_iterations
        self._mode = "edit_iterations"
        self._focus_pane = "detail"
        self._refresh()

    def _do_save_iterations(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin" or agent.source_path is None:
            return
        from ...agents.persist import update_agent_yaml
        update_agent_yaml(agent.source_path, {"max_iterations": self._edit_iterations_val})
        self._registry_changed = True
        self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self.query_one("#ag-panel", Vertical).focus()
        self._refresh()

    def _start_edit_prompt(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin" or agent.source_path is None:
            return
        ta = self.query_one("#ag-run-input", TextArea)
        ta.load_text(agent.system_prompt)
        self._mode = "edit_prompt"
        self._focus_pane = "detail"
        self._refresh()
        ta.focus()

    def _do_save_prompt(self) -> None:
        agent = self._current_agent()
        if agent is None or agent.source == "builtin" or agent.source_path is None:
            return
        new_prompt = self.query_one("#ag-run-input", TextArea).text.strip()
        if new_prompt:
            from ...agents.persist import update_agent_yaml
            update_agent_yaml(agent.source_path, {"system_prompt": new_prompt})
            self._registry_changed = True
            self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self.query_one("#ag-panel", Vertical).focus()
        self._refresh()

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
