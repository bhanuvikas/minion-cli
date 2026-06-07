"""SkillsScreen — /skills modal for the Textual TUI.

States:
  browse           — full list + detail pane (default)
  search           — live-filter while query box active
  detail           — right pane focused
  confirm_delete   — inline confirm strip; second d executes deletion
  duplicate        — duplicate form; name input + tier selector
  run              — single-line argument input
  create           — full creation form
  edit_description — single-line description TextArea
  edit_prompt      — multi-line prompt TextArea
  edit_tools       — checkbox grid (13 tools)
  edit_iterations  — stepper ←→
  edit_output_format — radio (stream/markdown) + optional thinking label

Layered esc:
  edit/create/run    → back to browse
  search active      → clear query → full list
  otherwise          → dismiss modal
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
    from ...skills.manifest import SkillManifest
    from ...skills.registry import SkillRegistry

# Default tool set when creating a blank skill
_BLANK_TOOLS: list[str] = ["read_file", "list_directory", "search_file", "get_file_outline"]

_FORMAT_OPTIONS: list[tuple[str, str]] = [
    ("stream",   "live streaming output"),
    ("markdown", "collect then render as markdown"),
]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _format_skill_source_path(manifest: "SkillManifest") -> str:
    if manifest.source == "builtin":
        return f"minion://builtin/skills/{manifest.name}.yaml"
    if manifest.source_path is None:
        return f"minion://builtin/skills/{manifest.name}.yaml"
    try:
        return "~/" + str(manifest.source_path.relative_to(Path.home()))
    except ValueError:
        return str(manifest.source_path)


# ── Screen ────────────────────────────────────────────────────────────────────


class SkillsScreen(ModalScreen):  # type: ignore[type-arg]
    """Full-screen split-pane skill browser opened by /skills."""

    CSS = f"""
SkillsScreen {{
    align: center middle;
    background: #000000 40%;
}}
#sk-panel {{
    width: 90%;
    height: 90%;
    background: {_BG};
    border: round {_RULE};
}}
#sk-header {{
    height: auto;
    padding: 0 2;
    border-bottom: solid {_RULE};
}}
#sk-body {{
    height: 1fr;
}}
#sk-list-pane {{
    width: 50%;
    border-right: solid {_RULE};
}}
#sk-list-pane.lhs-focused {{
    border-right: solid {_ORANGE};
}}
#sk-list-pane.rhs-focused {{
    border-right: solid {_BLUE};
}}
#sk-list-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {_DIM};
}}
#sk-list {{
    height: auto;
}}
#sk-preview-pane {{
    width: 50%;
    padding: 0 1;
}}
#sk-preview-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {_DIM};
}}
#sk-preview {{
    height: auto;
}}
#sk-dup-top {{
    height: auto;
    display: none;
    padding: 1 1 0 1;
}}
#sk-dup-validation {{
    height: auto;
    display: none;
    padding: 0 1;
}}
#sk-dup-name {{
    height: auto;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    padding: 0 1;
}}
#sk-dup-name:focus {{
    border: solid {_ORANGE};
}}
#sk-preview-scroll.run-compact {{
    height: 12;
}}
#sk-preview-scroll.text-edit-compact {{
    height: auto;
}}
#sk-run-prompt-label {{
    height: auto;
    display: none;
    margin-top: 1;
}}
#sk-run-input {{
    height: auto;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    padding: 0 1;
}}
#sk-run-input:focus {{
    border: solid {_ORANGE};
}}
#sk-text-edit {{
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
#sk-text-edit.single-line {{
    height: 12;
}}
#sk-text-edit.prompt-edit {{
    height: 1fr;
}}
#sk-text-edit:focus {{
    border: solid {_ORANGE};
}}
#sk-text-edit .text-area--cursor-line {{
    background: #1a1a1a;
}}
#sk-text-edit.inherited {{
    color: {_DIM};
}}
#sk-thinking-label {{
    height: auto;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    padding: 0 1;
}}
#sk-thinking-label:focus {{
    border: solid {_ORANGE};
}}
#sk-run-hints {{
    height: 2;
    display: none;
    background: {_BG};
    border-top: solid {_RULE};
    padding: 0 2;
    margin: 0 -1;
}}
#sk-footer {{
    height: 2;
    padding: 0 2;
    background: {_BG};
    border-top: solid {_RULE};
}}
#sk-create-banner {{
    height: auto;
    display: none;
    padding: 0 1;
    color: {_ORANGE};
}}
"""

    BINDINGS = [
        Binding("escape",    "esc_action",       show=False, priority=True),
        Binding("up",        "nav_up",           show=False, priority=True),
        Binding("down",      "nav_down",         show=False, priority=True),
        Binding("enter",     "confirm",          show=False, priority=True),
        Binding("tab",       "cycle_scope",      show=False, priority=True),
        Binding("shift+tab", "cycle_scope_back", show=False, priority=True),
        Binding("ctrl+enter","confirm_primary",  show=False, priority=True),
        Binding("ctrl+j",    "confirm_primary",  show=False, priority=True),
    ]

    def __init__(
        self,
        skill_registry: "SkillRegistry",
        cwd: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._registry: dict[str, "SkillManifest"] = {k: v for k, v in skill_registry.items()}
        self._cwd: Path = cwd or Path.cwd()
        self._mode: str = "browse"
        self._scope: str = "all"
        self._query: str = ""
        self._selected: int = 0
        self._focus_pane: str = "list"
        self._visible: "list[SkillManifest]" = []
        self._del_confirmed: bool = False
        self._dup_name: str = ""
        self._dup_tier: str = "user"
        self._dup_focus: str = "name"   # "name" | "tier"
        self._registry_changed: bool = False
        self._run_skill_name: str = ""
        # Edit tools
        self._edit_tools: list[str] = []
        self._edit_tools_saved: list[str] = []
        self._edit_tools_cursor: int = 0
        # Edit iterations
        self._edit_iterations_val: int = 20
        # Edit output format
        self._edit_format_cursor: int = 0   # 0=stream, 1=markdown
        self._edit_thinking_label: str = ""
        # Create form
        self._create_name: str = ""
        self._create_tier: str = "user"
        self._create_desc: str = ""
        self._create_desc_inherited: bool = False
        self._create_starting_point: str = "blank"
        self._create_template_cursor: int = 0
        self._create_focus: str = "name"
        self._create_undo_path: Optional[Path] = None
        self._create_undo_active: bool = False

    # ── Data ──────────────────────────────────────────────────────────────────

    def _rebuild_visible(self) -> None:
        skills = list(self._registry.values())
        skills.sort(key=lambda m: (_TIER_ORDER.get(m.source, 3), m.name))
        if self._scope != "all" and not self._query:
            skills = [m for m in skills if m.source == self._scope]
        if self._query:
            q = self._query.lower()
            skills = [
                m for m in skills
                if q in m.name.lower() or q in m.description.lower()
            ]
        self._visible = skills
        if self._selected >= len(self._visible):
            self._selected = max(0, len(self._visible) - 1)

    def _reload_registry(self) -> None:
        from ...skills.registry import load_skill_registry
        self._registry = {k: v for k, v in load_skill_registry(self._cwd).items()}
        self._rebuild_visible()

    def _current_skill(self) -> "Optional[SkillManifest]":
        if not self._visible:
            return None
        return self._visible[self._selected]

    def _shadow_set(self) -> set[str]:
        return {m.name for m in self._registry.values() if m.source in ("user", "project")}

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
            return Path.home() / ".minion" / "skills" / f"{name}.yaml"
        return self._cwd / ".minion" / "skills" / f"{name}.yaml"

    def _dup_target_path_preview(self, tier: str) -> str:
        name = self._dup_name or "<name>"
        if tier == "user":
            return f"~/.minion/skills/{name}.yaml"
        return f".minion/skills/{name}.yaml"

    def _find_fallback(self, manifest: "SkillManifest") -> "Optional[SkillManifest]":
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
            return Path.home() / ".minion" / "skills" / f"{name}.yaml"
        return self._cwd / ".minion" / "skills" / f"{name}.yaml"

    def _create_target_path_preview(self, tier: str) -> str:
        name = self._create_name or "<name>"
        if tier == "user":
            return f"~/.minion/skills/{name}.yaml"
        return f".minion/skills/{name}.yaml"

    def _create_scaffold_prompt(self, name: str, desc: str) -> str:
        body = desc.strip()
        return f"# {name}\n\n{body}" if body else f"# {name}"

    def _get_template_list(self) -> "list[SkillManifest]":
        return sorted(self._registry.values(), key=lambda m: (_TIER_ORDER.get(m.source, 3), m.name))

    def _template_capability_summary(self, m: "SkillManifest") -> str:
        tools = m.tools if m.tools is not None else _NATIVE_TOOLS
        count = len(tools)
        fmt = m.output_format or "stream"
        lines = len(m.prompt.splitlines())
        return f"{fmt} · {count} tools · {lines} lines"

    def _update_create_widget_focus(self) -> None:
        if self._create_focus == "name":
            self.query_one("#sk-dup-name", Input).focus()
        elif self._create_focus == "description":
            self.query_one("#sk-text-edit", TextArea).focus()
        else:
            self.query_one("#sk-panel", Vertical).focus()

    def _sync_template_selection(self) -> None:
        templates = self._get_template_list()
        if 0 <= self._create_template_cursor < len(templates):
            tmpl = templates[self._create_template_cursor]
            ta = self.query_one("#sk-text-edit", TextArea)
            ta.load_text(tmpl.description)
            self._create_desc = tmpl.description
            self._create_desc_inherited = True

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self.query_one("#sk-header", Static).update(self._build_header())
        self.query_one("#sk-footer", Static).update(self._build_footer())
        self.query_one("#sk-list", Static).update(self._build_list())
        self.query_one("#sk-preview", Static).update(self._build_preview())

        _NON_BROWSE = {"run", "edit_tools", "edit_description", "edit_iterations",
                       "edit_prompt", "edit_output_format", "duplicate", "create"}
        list_pane = self.query_one("#sk-list-pane", Vertical)
        if self._focus_pane == "detail" or self._mode in _NON_BROWSE:
            list_pane.remove_class("lhs-focused")
            list_pane.add_class("rhs-focused")
        else:
            list_pane.remove_class("rhs-focused")
            list_pane.add_class("lhs-focused")

        in_dup    = self._mode == "duplicate"
        in_create = self._mode == "create"
        in_either = in_dup or in_create
        skill = self._current_skill()

        # ── dup-top / dup-name / dup-validation ──────────────────────────────
        self.query_one("#sk-dup-top", Static).display = in_either
        if in_create:
            self.query_one("#sk-dup-top", Static).update(self._build_create_top())
        elif in_dup and skill:
            self.query_one("#sk-dup-top", Static).update(self._build_dup_top(skill))

        self.query_one("#sk-dup-validation", Static).display = in_either
        if in_create:
            self.query_one("#sk-dup-validation", Static).update(self._build_create_validation())
        elif in_dup:
            self.query_one("#sk-dup-validation", Static).update(self._build_dup_validation())

        dup_input = self.query_one("#sk-dup-name", Input)
        dup_input.display = in_either
        if in_create and self._create_focus == "name":
            dup_input.focus()
        elif in_dup and self._dup_focus == "name":
            dup_input.focus()

        # ── banner ────────────────────────────────────────────────────────────
        banner = self.query_one("#sk-create-banner", Static)
        banner.display = self._create_undo_active
        if self._create_undo_active:
            banner.update(self._build_create_banner())

        # ── mode flags ────────────────────────────────────────────────────────
        in_run    = self._mode == "run"
        in_prompt = self._mode == "edit_prompt"
        in_field  = self._mode == "edit_description"
        in_format = self._mode == "edit_output_format"

        preview_scroll = self.query_one("#sk-preview-scroll", VerticalScroll)

        if in_run:
            preview_scroll.remove_class("text-edit-compact")
            preview_scroll.add_class("run-compact")
        elif in_prompt or in_field or in_create:
            preview_scroll.remove_class("run-compact")
            preview_scroll.add_class("text-edit-compact")
        else:
            preview_scroll.remove_class("run-compact")
            preview_scroll.remove_class("text-edit-compact")

        # sk-run-input (single-line, run mode only)
        self.query_one("#sk-run-input", Input).display = in_run
        if in_run:
            self.query_one("#sk-run-input", Input).focus()

        # sk-text-edit (TextArea for description/prompt/create)
        ta = self.query_one("#sk-text-edit", TextArea)
        ta.display = in_prompt or in_field or in_create
        if in_field or in_create:
            ta.add_class("single-line")
            ta.remove_class("prompt-edit")
        elif in_prompt:
            ta.remove_class("single-line")
            ta.add_class("prompt-edit")
        else:
            ta.remove_class("single-line")
            ta.remove_class("prompt-edit")
        if in_create and self._create_desc_inherited:
            ta.add_class("inherited")
        else:
            ta.remove_class("inherited")
        if in_create and self._create_focus == "description":
            ta.focus()

        # sk-thinking-label (below scroll, edit_output_format + markdown)
        show_thinking = in_format and self._edit_format_cursor == 1
        self.query_one("#sk-thinking-label", Input).display = show_thinking

        # label above run-input / text-edit
        show_label = in_run or in_prompt or in_field or in_create or (in_format and self._edit_format_cursor == 1)
        self.query_one("#sk-run-prompt-label", Static).display = show_label
        if show_label:
            if in_create:
                lbl = self._build_create_desc_label()
            elif in_run:
                lbl = self._build_run_prompt_label()
            elif in_prompt:
                lbl = self._build_prompt_edit_label()
            elif in_format:
                lbl = self._build_field_edit_label("THINKING LABEL")
            else:
                lbl = self._build_field_edit_label("DESCRIPTION")
            self.query_one("#sk-run-prompt-label", Static).update(lbl)

        # hints strip
        show_hints = in_run or in_prompt or in_field or in_create or in_format
        self.query_one("#sk-run-hints", Static).display = show_hints
        if show_hints:
            if in_create:
                hints_txt: Text = self._build_create_hints()
            elif in_run:
                hints_txt = self._build_run_hints()
            elif in_format:
                hints_txt = self._build_format_hints()
            else:
                hints_txt = self._build_edit_hints()
            self.query_one("#sk-run-hints", Static).update(hints_txt)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> Table:
        t = Text()
        t.append("┌─ ", style=_FAINT)
        t.append("/skills", style="bold")
        sep = " › " if self._mode == "create" else " — "
        t.append(sep, style=_DIM)

        if self._mode == "create":
            t.append("create new skill", style=f"bold {_ORANGE}")
        elif self._mode == "confirm_delete":
            t.append("delete skill", style=_DIM)
            t.append("  ")
            t.append(" press d again to confirm ", style=f"bold {_ORANGE} on #2a0e06")
        elif self._mode == "duplicate":
            skill = self._current_skill()
            name = skill.name if skill else "skill"
            t.append(f"{name} › duplicate", style=_DIM)
        elif self._mode == "run":
            t.append(f"{self._run_skill_name} › ", style=_DIM)
            t.append("run", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(" ctrl+↵ to dispatch ", style=f"{_SILVER} on #161614")
        elif self._mode == "edit_tools":
            skill = self._current_skill()
            name = skill.name if skill else "skill"
            allowed = len(self._edit_tools)
            total = len(_NATIVE_TOOLS)
            t.append(f"{name} › ", style=_DIM)
            t.append("edit tools", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(f" {allowed} of {total} allowed ", style=f"{_ORANGE} on #1a0800")
        elif self._mode == "edit_description":
            skill = self._current_skill()
            name = skill.name if skill else "skill"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit description", style=f"bold {_ORANGE}")
        elif self._mode == "edit_iterations":
            skill = self._current_skill()
            name = skill.name if skill else "skill"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit max iterations", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(f" {self._edit_iterations_val} ", style=f"bold {_ORANGE} on #1a0800")
        elif self._mode == "edit_prompt":
            skill = self._current_skill()
            name = skill.name if skill else "skill"
            t.append(f"{name} › ", style=_DIM)
            t.append("edit prompt", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(" ctrl+↵ to save ", style=f"{_SILVER} on #161614")
        elif self._mode == "edit_output_format":
            skill = self._current_skill()
            name = skill.name if skill else "skill"
            fmt = _FORMAT_OPTIONS[self._edit_format_cursor][0]
            t.append(f"{name} › ", style=_DIM)
            t.append("edit output format", style=f"bold {_ORANGE}")
            t.append("  ")
            t.append(f" {fmt} ", style=f"bold {_ORANGE} on #1a0800")
        elif self._mode == "detail":
            skill = self._current_skill()
            name = skill.name if skill else "skill"
            t.append(f"{name} · detail", style=_DIM)
            t.append("  ")
            t.append(" focused ", style=f"{_DIM} on #161614")
        elif self._query:
            count = len(self._visible)
            t.append("browse skills", style=_DIM)
            t.append("  ")
            noun = "match" if count == 1 else "matches"
            t.append(f" {count} {noun} ", style=f"{_ORANGE} on #1a0800")
        else:
            t.append("browse skills", style=_DIM)

        _NON_BROWSE = {"run", "edit_tools", "edit_description", "edit_iterations",
                       "edit_prompt", "edit_output_format", "duplicate", "create"}
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
            t.append("skills/builtin/", style=f"italic {_FAINT}")
            t.append("  read-only  " + _FILL, style=_FAINT)
        elif tier == "user":
            t.append("  ─── USER ", style=f"bold {_GOLD_DIM}")
            t.append("~/.minion/skills/", style=f"italic {_FAINT}")
            t.append("  " + _FILL, style=_FAINT)
        else:
            t.append("  ─── PROJECT ", style=f"bold {_GREEN_DIM}")
            t.append(".minion/skills/", style=f"italic {_FAINT}")
            t.append("  " + _FILL, style=_FAINT)
        return t

    def _make_skill_row_table(self, name_w: int = 18) -> Table:
        t = Table.grid(expand=True, padding=0)
        t.add_column(no_wrap=True, width=3)
        t.add_column(no_wrap=True, width=name_w)
        t.add_column(no_wrap=True, ratio=1, overflow="ellipsis")
        t.add_column(no_wrap=True, width=2)
        t.add_column(no_wrap=True, width=6)
        return t

    def _add_skill_inner_row(
        self,
        inner: Table,
        manifest: "SkillManifest",
        idx: int,
        shadowed: bool,
        shadows_builtin: bool,
    ) -> None:
        is_selected = idx == self._selected
        is_danger   = self._mode == "confirm_delete" and is_selected
        row_style   = f"on {_TINT_ORG}" if is_danger else ""

        ptr = Text(no_wrap=True)
        if is_selected and self._focus_pane == "list":
            ptr.append("▸ ", style=f"bold {_ORANGE}")
            ptr.append(" ")
        else:
            ptr.append("   ")

        _tier_bright = {"builtin": _ORANGE, "user": _GOLD, "project": _GREEN}
        _tier_dim    = {"builtin": _DIM,    "user": _GOLD_DIM, "project": _GREEN_DIM}
        if is_danger:
            name_t = Text(manifest.name, style=f"strike {_ORANGE}", no_wrap=True)
        elif is_selected:
            name_t = Text(manifest.name, style=f"bold {_tier_bright.get(manifest.source, _ORANGE)}", no_wrap=True)
        else:
            name_t = Text(manifest.name, style=_tier_dim.get(manifest.source, _DIM), no_wrap=True)

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

        if (self._mode == "create" and self._create_name
                and manifest.name == self._create_name):
            name_t.append("  ← shadows", style=f"bold #7a3a26")

        tools = manifest.tools
        count_str = "all" if tools is None else str(len(tools))
        count_t = Text(count_str, style=_DIM, no_wrap=True)

        inner.add_row(ptr, name_t, desc_t, Text(""), count_t, style=row_style)

    def _add_confirm_strip_row(self, inner: Table) -> None:
        ptr = Text("▌  ", style=f"bold {_ORANGE}", no_wrap=True)
        msg = Text()
        msg.append("delete this skill?  ·  ", style=_ORANGE)
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
                no_match.append(f'  no skills match "{self._query}"  ·  ', style=_DIM)
                no_match.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
                no_match.append(" to clear", style=_DIM)
                outer.add_row(no_match)
            else:
                outer.add_row(Text("  no skills loaded", style=_FAINT))
            return outer

        shadow_builtins = self._shadow_set()
        builtin_names   = self._builtin_names()

        tiers_seen: list[str] = []
        by_tier: dict[str, list[int]] = {}
        for idx, m in enumerate(self._visible):
            if m.source not in by_tier:
                tiers_seen.append(m.source)
                by_tier[m.source] = []
            by_tier[m.source].append(idx)

        name_w = min(max((len(m.name) for m in self._visible), default=8) + 2, 24)

        _ALL_TIERS = ["builtin", "user", "project"]
        tiers_to_render = _ALL_TIERS if (self._scope == "all" and not self._query) else tiers_seen

        for i, tier in enumerate(tiers_to_render):
            if i > 0:
                outer.add_row(Text(""))
            outer.add_row(self._build_tier_header(tier))
            if tier not in by_tier:
                if self._scope == "all" and not self._query and tier in ("user", "project"):
                    hint = Text()
                    hint.append(f"   no {tier} skills", style=_DIM)
                    hint.append("  ·  press ", style=_FAINT)
                    hint.append(" n ", style=f"bold {_SILVER} on #2a2a2a")
                    hint.append(" to create one", style=_DIM)
                    outer.add_row(hint)
                else:
                    outer.add_row(Text(f"   no {tier} skills", style=_FAINT))
            else:
                inner = self._make_skill_row_table(name_w)
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
                    self._add_skill_inner_row(inner, manifest, idx, shadowed, shadows_builtin)
                    if self._mode == "confirm_delete" and idx == self._selected:
                        self._add_confirm_strip_row(inner)
                outer.add_row(inner)

        return outer

    # ── Preview / right pane ──────────────────────────────────────────────────

    def _build_preview(self) -> Table:
        skill = self._current_skill()
        if self._mode == "create":
            return self._build_preview_create()
        if self._mode == "confirm_delete" and skill:
            return self._build_preview_delete(skill)
        if self._mode == "duplicate" and skill:
            return self._build_preview_duplicate(skill)
        if self._mode == "run":
            return self._build_preview_run()
        if self._mode == "edit_tools":
            return self._build_preview_tools()
        if self._mode == "edit_description" and skill:
            return self._build_preview_edit_field(skill, "DESCRIPTION", skill.description)
        if self._mode == "edit_iterations" and skill:
            return self._build_preview_iterations(skill)
        if self._mode == "edit_prompt" and skill:
            return self._build_preview_edit_prompt(skill)
        if self._mode == "edit_output_format" and skill:
            return self._build_preview_output_format(skill)
        if skill is None:
            tbl = Table.grid(expand=True, padding=0)
            tbl.add_column()
            tbl.add_row(Text(""))
            tbl.add_row(Text("  select a skill to see details", style=_FAINT))
            return tbl
        return self._build_preview_browse(skill)

    def _build_preview_browse(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        is_chained = bool(manifest.steps)

        # Header
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
        tbl.add_row(Text(f"   {_format_skill_source_path(manifest)}", style=_FAINT))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # TOOLS
        tools_header = Text()
        tools_header.append(" TOOLS", style=f"bold {_DIM}")
        if is_chained:
            tools_header.append(" · — inherited from steps", style=_DIM)
        else:
            if manifest.tools is None:
                tools_header.append(" · all allowed", style=_DIM)
            else:
                tools_header.append(f" · {len(manifest.tools)} of {len(_NATIVE_TOOLS)} allowed", style=_DIM)
            if manifest.source != "builtin":
                tools_header.append("  ")
                tools_header.append(" t ", style=f"bold {_SILVER} on #2a2a2a")
                tools_header.append(" edit", style=_DIM)
        tbl.add_row(tools_header)
        if not is_chained:
            tbl.add_row(self._build_tools_section(manifest))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # OUTPUT FORMAT
        fmt_header = Text()
        fmt_header.append(" OUTPUT FORMAT", style=f"bold {_DIM}")
        if manifest.source != "builtin":
            fmt_header.append("  ")
            fmt_header.append(" o ", style=f"bold {_SILVER} on #2a2a2a")
            fmt_header.append(" edit", style=_DIM)
        fmt_val = manifest.output_format or "stream"
        tbl.add_row(fmt_header)
        tbl.add_row(Text(f"   {fmt_val}", style=f"bold {_GOLD}"))
        if fmt_val == "markdown" and manifest.thinking_label:
            think_row = Text()
            think_row.append("   thinking label  ", style=_FAINT)
            think_row.append(f'"{manifest.thinking_label}"', style=_DIM)
            tbl.add_row(think_row)
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # MAX ITERATIONS
        iter_header = Text()
        iter_header.append(" MAX ITERATIONS", style=f"bold {_DIM}")
        if manifest.source != "builtin":
            iter_header.append("  ")
            iter_header.append(" i ", style=f"bold {_SILVER} on #2a2a2a")
            iter_header.append(" edit", style=_DIM)
        iter_val = manifest.max_iterations
        suffix = " (per step)" if is_chained else ""
        tbl.add_row(iter_header)
        tbl.add_row(Text(f"   {iter_val}{suffix}", style=_DIM))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # ARGS (only if non-empty)
        if manifest.args:
            args_header = Text()
            args_header.append(" ARGS", style=f"bold {_DIM}")
            args_header.append(f"  ·  {len(manifest.args)}", style=_DIM)
            tbl.add_row(args_header)
            args_tbl = Table.grid(expand=True, padding=0)
            args_tbl.add_column(width=2, no_wrap=True)
            args_tbl.add_column(no_wrap=True, width=16)
            args_tbl.add_column(ratio=1)
            args_tbl.add_column(no_wrap=True, width=10)
            for arg in manifest.args:
                req_t = Text("required" if arg.required else "optional", style=_FAINT)
                args_tbl.add_row(
                    Text(""),
                    Text(arg.name, style=_TEXT),
                    Text(arg.description, style=_DIM),
                    req_t,
                )
            tbl.add_row(args_tbl)
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))

        # STEPS (only if chained)
        if is_chained:
            steps_header = Text()
            steps_header.append(" STEPS", style=f"bold {_DIM}")
            steps_header.append(f"  ·  {len(manifest.steps)}", style=_DIM)
            tbl.add_row(steps_header)
            for i, step_name in enumerate(manifest.steps):
                step_skill = self._registry.get(step_name)
                row = Text()
                row.append(f"   {i + 1}.  ", style=_FAINT)
                row.append(f"/{step_name}", style=f"bold {_SILVER}")
                if step_skill:
                    row.append(f"  {step_skill.description}", style=_DIM)
                else:
                    row.append("  (not found)", style=f"bold {_ORANGE}")
                tbl.add_row(row)
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))

        # PROMPT
        prompt_header = Text()
        prompt_header.append(" PROMPT", style=f"bold {_DIM}")
        if is_chained:
            prompt_header.append(" · — none (uses step prompts)", style=_DIM)
        else:
            total_lines = len(manifest.prompt.splitlines())
            prompt_header.append(f"  ·  {total_lines} lines", style=_DIM)
            if manifest.source != "builtin":
                prompt_header.append("  ")
                prompt_header.append(" p ", style=f"bold {_SILVER} on #2a2a2a")
                prompt_header.append(" edit", style=_DIM)
        tbl.add_row(prompt_header)
        if not is_chained:
            tbl.add_row(self._build_prompt_preview(manifest))

        # Precedence block
        if manifest.source == "project" and manifest.name in self._builtin_names():
            tbl.add_row(Text(""))
            tbl.add_row(self._build_precedence_block(manifest))

        return tbl

    def _build_tools_section(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tools = manifest.tools
        if tools is None:
            tbl.add_row(Text("  all native tools allowed", style=_FAINT))
        elif not tools:
            tbl.add_row(Text("  no tools allowed", style=_FAINT))
        else:
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

    def _build_prompt_preview(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        content_text = Text()
        for line in manifest.prompt.splitlines():
            parts = line.split("{arg}")
            if len(parts) == 1:
                content_text.append((line or " ") + "\n", style=_FAINT)
            else:
                for i, part in enumerate(parts):
                    if part:
                        content_text.append(part, style=_FAINT)
                    if i < len(parts) - 1:
                        content_text.append("{arg}", style=f"bold {_GOLD}")
                content_text.append("\n")
        panel = Panel(content_text, border_style=_RULE, style="on #0f0f0d", padding=(0, 1), expand=True)
        tbl.add_row(panel)
        return tbl

    def _build_precedence_block(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(" PRECEDENCE", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))
        active = Text()
        active.append("  ●  this   ", style=f"bold {_GREEN}")
        active.append(_format_skill_source_path(manifest), style=_FAINT)
        active.append("  ← used", style=f"bold {_GREEN}")
        tbl.add_row(active)
        builtin_m = next(
            (m for m in self._registry.values() if m.name == manifest.name and m.source == "builtin"),
            None,
        )
        if builtin_m:
            below = Text()
            below.append("  ○  below  ", style=_FAINT)
            below.append(_format_skill_source_path(builtin_m), style=_FAINT)
            tbl.add_row(below)
        return tbl

    def _build_preview_run(self) -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        skill = next((m for m in self._registry.values() if m.name == self._run_skill_name), None)

        identity = Text()
        identity.append(f" {self._run_skill_name}", style=f"bold {_SILVER}")
        if skill:
            identity.append("  ")
            identity.append(f" {skill.source} ", style=f"bold {_tier_color(skill.source)} on #161614")
            if skill.source != "builtin" and skill.source_path:
                mtime = _age(skill.source_path)
                if mtime != "unknown":
                    identity.append(f"  ·  edited {mtime}", style=_DIM)
        tbl.add_row(identity)
        tbl.add_row(Text(""))

        if skill and skill.description:
            tbl.add_row(Text(f"  {skill.description}", style=_TEXT))
            tbl.add_row(Text(""))

        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # Compact context
        if skill:
            if skill.tools is None:
                tools_str = f"all {len(_NATIVE_TOOLS)} native tools"
            else:
                tools_str = f"{len(skill.tools)} of {len(_NATIVE_TOOLS)} allowed"
        else:
            tools_str = "unknown"
        tools_row = Text()
        tools_row.append("  tools     ", style=_FAINT)
        tools_row.append(tools_str, style=_TEXT)
        tbl.add_row(tools_row)

        fmt_row = Text()
        fmt_row.append("  format    ", style=_FAINT)
        fmt_row.append(skill.output_format if skill else "stream", style=f"bold {_GOLD}")
        tbl.add_row(fmt_row)

        limit_row = Text()
        limit_row.append("  limit     ", style=_FAINT)
        limit_row.append(f"{skill.max_iterations if skill else 20} iterations", style=_TEXT)
        tbl.add_row(limit_row)

        if skill and skill.args:
            arg0 = skill.args[0]
            tbl.add_row(Text(""))
            arg_row = Text()
            arg_row.append("  argument  ", style=_FAINT)
            arg_row.append(arg0.name, style=_TEXT)
            if arg0.description:
                arg_row.append(f"  — {arg0.description}", style=_DIM)
            tbl.add_row(arg_row)

        tbl.add_row(Text(""))
        dispatch_row = Text()
        dispatch_row.append("  dispatches as  ", style=_FAINT)
        dispatch_row.append(f"/{self._run_skill_name} <arg>", style=_DIM)
        tbl.add_row(dispatch_row)

        return tbl

    def _build_preview_edit_field(
        self,
        manifest: "SkillManifest",
        label: str,
        current: str,
    ) -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        header = Text()
        header.append(f" {manifest.name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        tbl.add_row(header)
        return tbl

    def _build_preview_edit_prompt(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        header = Text()
        header.append(f" {manifest.name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        total = len(manifest.prompt.splitlines())
        header.append(f"  ·  {total} lines", style=_DIM)
        tbl.add_row(header)
        return tbl

    def _build_preview_iterations(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        header = Text()
        header.append(f" {manifest.name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        tbl.add_row(header)
        tbl.add_row(Text(""))
        tbl.add_row(Text(" MAX ITERATIONS", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))
        val = self._edit_iterations_val
        stepper = Text()
        stepper.append("   ")
        stepper.append(" ← ", style=f"bold {_SILVER} on #2a2a2a")
        stepper.append(f"  {val}  ", style=f"bold {_ORANGE}")
        stepper.append(" → ", style=f"bold {_SILVER} on #2a2a2a")
        stepper.append("  iterations", style=_DIM)
        tbl.add_row(stepper)
        tbl.add_row(Text(""))
        tbl.add_row(Text("   range 1 – 100", style=_FAINT))
        return tbl

    def _build_preview_output_format(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        hdr = Text()
        hdr.append(f" {manifest.name}", style=f"bold {_SILVER}")
        hdr.append("  ")
        hdr.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        tbl.add_row(hdr)
        tbl.add_row(Text(""))
        tbl.add_row(Text(" Choose how output is rendered.", style=_DIM))
        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        for i, (fmt, desc) in enumerate(_FORMAT_OPTIONS):
            is_sel = i == self._edit_format_cursor
            row = Text()
            if is_sel:
                row.append("  ▸  ", style=f"bold {_ORANGE}")
                row.append("● ", style=f"bold {_SILVER}")
                row.append(f"{fmt:<12}", style=f"bold {_SILVER}")
            else:
                row.append("     ")
                row.append("○ ", style=_DIM)
                row.append(f"{fmt:<12}", style=_DIM)
            row.append(desc, style=_DIM if is_sel else _FAINT)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")

        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(""))

        # Preview
        fmt_val = _FORMAT_OPTIONS[self._edit_format_cursor][0]
        label_val = self._edit_thinking_label.strip()
        preview = Text()
        preview.append("  PREVIEW  ", style=_DIM)
        preview.append(fmt_val, style=f"bold {_GOLD}")
        if fmt_val == "markdown" and label_val:
            preview.append(f'  · "{label_val}"', style=_DIM)
        preview.append("  ⠋", style=_FAINT)
        tbl.add_row(preview)

        return tbl

    def _build_preview_tools(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()

        skill = self._current_skill()
        if skill:
            hdr = Text()
            hdr.append(f" {skill.name}", style=f"bold {_SILVER}")
            hdr.append("  ")
            hdr.append(f" {skill.source} ", style=f"bold {_tier_color(skill.source)} on #161614")
            tbl.add_row(hdr)
            tbl.add_row(Text(""))

        preamble = Text()
        preamble.append("  Toggle which tools this skill may call. Tools marked ", style=_DIM)
        preamble.append("⚠", style=_ORANGE)
        preamble.append(" have broad capability.", style=_DIM)
        tbl.add_row(preamble)
        tbl.add_row(Text(""))

        cat_order = ["filesystem", "shell", "network", "agents", "tasks", "other"]
        by_cat: dict[str, list[str]] = {}
        for tool in _NATIVE_TOOLS:
            cat = _TOOL_CATEGORIES.get(tool, "other")
            by_cat.setdefault(cat, []).append(tool)

        flat_idx = 0
        for cat in cat_order:
            if cat not in by_cat:
                continue
            tbl.add_row(Text(f"  ─── {cat}", style=_FAINT))
            for tool in by_cat[cat]:
                is_sel     = flat_idx == self._edit_tools_cursor
                is_allowed = tool in self._edit_tools
                check = f"[{'✓' if is_allowed else ' '}]"
                row = Text()
                row.append("  ▸ " if is_sel else "    ", style=f"bold {_ORANGE}" if is_sel else "")
                row.append(f"{check}  ", style=f"bold {_ORANGE}" if is_allowed else _DIM)
                row.append(f"{tool:<20}   ", style=_TEXT if is_allowed else _DIM)
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

        allowed_count = len(self._edit_tools)
        denied_count  = len(_NATIVE_TOOLS) - allowed_count
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

    def _build_preview_delete(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        tbl.add_row(Text(""))

        warn = Text()
        warn.append(" ⚠  About to delete ", style=f"bold {_ORANGE}")
        warn.append(manifest.name, style=f"bold {_SILVER}")
        warn.append(f"     {manifest.source} tier", style=_DIM)
        tbl.add_row(warn)
        tbl.add_row(Text(""))
        tbl.add_row(Text(" The skill file will be permanently removed.", style=_DIM))
        tbl.add_row(Text(" No backup is created. No undo.", style=_DIM))
        tbl.add_row(Text(""))
        tbl.add_row(Rule(style=_RULE))

        tbl.add_row(Text(" FILE", style=f"bold {_DIM}"))
        tbl.add_row(Text(f"   {_format_skill_source_path(manifest)}", style=_FAINT))
        mtime = _age(manifest.source_path)
        line_count = len(manifest.prompt.splitlines())
        tbl.add_row(Text(f"   last edited {mtime}  ·  {line_count} lines of prompt", style=_FAINT))
        tbl.add_row(Text(""))

        tbl.add_row(Rule(style=_RULE))
        tbl.add_row(Text(" FALLBACK AFTER DELETE", style=f"bold {_DIM}"))
        tbl.add_row(Text("  Skill calls to this name resolve to:", style=_DIM))
        fallback = self._find_fallback(manifest)
        if fallback:
            fb_t = Text()
            fb_t.append(f"  → {fallback.source}  ", style=_FAINT)
            fb_t.append(_format_skill_source_path(fallback), style=_FAINT)
            tbl.add_row(fb_t)
        else:
            tbl.add_row(Text("  → — none —", style=_FAINT))
        tbl.add_row(Text(""))

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
                t.append(f"  ·  {_format_skill_source_path(tmpl)}", style=_FAINT)
            else:
                t.append("— pick a template below —", style=_FAINT)
        t.append("\n\n")
        t.append(" ─── name ──────────────────────────────────────────────────", style=_DIM)
        return t

    def _build_create_validation(self) -> Text:
        t = Text()
        if not self._create_name:
            t.append("  letters, digits, dashes  ·  2–40 chars", style=_FAINT)
        elif not self._create_name_valid():
            t.append("  ✗  invalid format", style=f"bold {_ORANGE}")
            t.append("  ·  letters, digits, dashes · must start with letter or digit", style=_FAINT)
        elif not self._create_name_available():
            t.append("  ✗  name already exists", style=f"bold {_ORANGE}")
            conflict = next((m for m in self._registry.values() if m.name == self._create_name), None)
            if conflict:
                t.append(f"  ·  conflict: {conflict.source}  {_format_skill_source_path(conflict)}", style=_FAINT)
            suggestion = self._create_suggest()
            t.append("  ·  try ", style=_FAINT)
            t.append(suggestion, style=_DIM)
            t.append("  (tab)", style=_FAINT)
        else:
            t.append("  ✓  available", style=f"bold {_GREEN}")
            t.append(f"  ·  {self._create_target_path_preview(self._create_tier)}", style=_FAINT)
        return t

    def _build_create_desc_label(self) -> Text:
        t = Text()
        t.append("  ── description ────────────────────────────────────────────", style=_DIM)
        if self._create_desc_inherited:
            templates = self._get_template_list()
            if 0 <= self._create_template_cursor < len(templates):
                tmpl_name = templates[self._create_template_cursor].name
                t.append(f"\n  inherited from {tmpl_name}", style=_FAINT)
                t.append("  ·  edit to override", style=_FAINT)
        else:
            t.append("\n  optional — describes what this skill does", style=_FAINT)
        return t

    def _build_create_banner(self) -> Text:
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
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        tbl.add_row(Text(""))

        # Tier selector
        tbl.add_row(Text(" ─── tier ─────────────────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        tier_focused = self._create_focus == "tier"
        _unsel_dim = {"user": _GOLD_DIM, "project": _GREEN_DIM}
        for tier in ["user", "project"]:
            is_sel = self._create_tier == tier
            bullet = "●" if is_sel else "○"
            ptr_sty = f"bold {_ORANGE}" if (is_sel and tier_focused) else (_DIM if not is_sel else _SILVER)
            collides = (
                bool(self._create_name)
                and self._create_name_valid()
                and not self._create_name_available()
                and any(m.name == self._create_name and m.source == tier for m in self._registry.values())
            )
            row = Text()
            row.append(f"  {'▸' if (is_sel and tier_focused) else ' '} {bullet} ", style=ptr_sty)
            row.append(f"{tier:<8}", style=f"bold {_tier_color(tier)}" if is_sel else _unsel_dim.get(tier, _DIM))
            row.append(f"  {self._create_target_path_preview(tier)}", style=_FAINT)
            if collides:
                row.append("  ← collides", style=f"bold {_ORANGE}")
            tbl.add_row(row, style=f"on {_TINT_ORG}" if (is_sel and tier_focused) else "")
        tbl.add_row(Text(""))

        # Starting point
        tbl.add_row(Text(" ─── starting point ───────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        sp_focused = self._create_focus == "starting_point"
        for option, label in [("blank",    "fresh skill · stream output"),
                               ("template", "copy fields from an existing skill")]:
            is_sel = self._create_starting_point == option
            bullet = "●" if is_sel else "○"
            ptr_sty = f"bold {_ORANGE}" if (is_sel and sp_focused) else ""
            row = Text()
            row.append(f"  {'▸' if (is_sel and sp_focused) else ' '} {bullet} ", style=ptr_sty)
            row.append(f"{option:<16}", style=f"bold {_SILVER}" if is_sel else _DIM)
            row.append(label, style=_TEXT if is_sel else _DIM)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if (is_sel and sp_focused) else "")

        if self._create_starting_point == "template":
            tbl.add_row(Text(""))
            tbl.add_row(Text("      ── pick a template ──────────────────────────────────", style=_FAINT))
            templates = self._get_template_list()
            _tdim = {"builtin": _DIM, "user": _GOLD_DIM, "project": _GREEN_DIM}
            for i, tmpl in enumerate(templates):
                is_sel = i == self._create_template_cursor
                ptr    = "  ▸" if is_sel else "   "
                row = Text()
                row.append(f"    {ptr} ", style=f"bold {_ORANGE}" if is_sel else _DIM)
                row.append(f"{tmpl.name:<20}", style=f"bold {_tier_color(tmpl.source)}" if is_sel else _tdim.get(tmpl.source, _DIM))
                row.append(f"  {tmpl.source:<9}", style=_DIM)
                row.append(self._template_capability_summary(tmpl), style=_FAINT)
                tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")
        tbl.add_row(Text(""))

        # What will be created
        tbl.add_row(Text(" ─── what will be created ─────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        is_valid = self._create_name_valid() and self._create_name_available()

        def _prow(label: str, value: str, value_style: str = _TEXT) -> Text:
            r = Text()
            r.append("  ✓  " if is_valid else "  ·  ", style=f"bold {_GREEN}" if is_valid else _DIM)
            r.append(f"{label:<16}", style=_DIM if is_valid else _FAINT)
            r.append(value, style=value_style if is_valid else _FAINT)
            return r

        if self._create_name:
            name_sty = (f"bold {_tier_color(self._create_tier)}" if is_valid else f"strike {_FAINT}")
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
            desc_val  = self._create_desc.strip()
            desc_disp = (desc_val[:35] + "…") if len(desc_val) > 35 else desc_val
            tbl.add_row(_prow("description", desc_disp if desc_disp else "optional"))
            tbl.add_row(_prow("tools", "all native tools"))
            tbl.add_row(_prow("output_format", "stream"))
            tbl.add_row(_prow("max_iterations", "20"))
            prompt_name = self._create_name or "<name>"
            tbl.add_row(_prow("prompt", f"# {prompt_name}" + (" + description" if self._create_desc.strip() else " only")))
        else:
            templates = self._get_template_list()
            if 0 <= self._create_template_cursor < len(templates):
                tmpl = templates[self._create_template_cursor]
                desc_src  = self._create_desc.strip() or tmpl.description
                desc_disp = (desc_src[:35] + "…") if len(desc_src) > 35 else desc_src
                t_desc = Text()
                t_desc.append("  ✓  " if is_valid else "  ·  ", style=f"bold {_GREEN}" if is_valid else _DIM)
                t_desc.append("description     ", style=_DIM if is_valid else _FAINT)
                if self._create_desc_inherited:
                    t_desc.append("inherited — ", style=f"italic {_DIM}" if is_valid else _FAINT)
                t_desc.append(desc_disp, style=_TEXT if is_valid else _FAINT)
                tbl.add_row(t_desc)
                tool_count = len(tmpl.tools) if tmpl.tools is not None else len(_NATIVE_TOOLS)
                t_tools = Text()
                t_tools.append("  ✓  " if is_valid else "  ·  ", style=f"bold {_GREEN}" if is_valid else _DIM)
                t_tools.append("tools           ", style=_DIM if is_valid else _FAINT)
                t_tools.append("copied — ", style=f"italic {_DIM}" if is_valid else _FAINT)
                t_tools.append(f"{tool_count} tools", style=_TEXT if is_valid else _FAINT)
                tbl.add_row(t_tools)
                tbl.add_row(_prow("output_format", tmpl.output_format or "stream"))
                tbl.add_row(_prow("max_iterations", str(tmpl.max_iterations)))
                prompt_lines = len(tmpl.prompt.splitlines())
                t_prompt = Text()
                t_prompt.append("  ✓  " if is_valid else "  ·  ", style=f"bold {_GREEN}" if is_valid else _DIM)
                t_prompt.append("prompt          ", style=_DIM if is_valid else _FAINT)
                t_prompt.append("copied — ", style=f"italic {_DIM}" if is_valid else _FAINT)
                t_prompt.append(f"{prompt_lines} lines from {tmpl.name}", style=_TEXT if is_valid else _FAINT)
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
            note.append("tools, output format, prompt", style=_DIM)
            note.append(" editable there", style=_FAINT)
            tbl.add_row(note)
        return tbl

    def _build_dup_top(self, manifest: "SkillManifest") -> Text:
        t = Text()
        t.append(" FROM  ", style=_DIM)
        t.append(f"  {manifest.name}  ", style=f"bold {_SILVER}")
        t.append(f" {manifest.source} ", style=f"bold {_tier_color(manifest.source)} on #161614")
        if manifest.source == "builtin":
            t.append("  ·  read-only", style=_FAINT)
        t.append(f"  ·  {_format_skill_source_path(manifest)}", style=_FAINT)
        t.append("\n\n")
        t.append("─── new name ───────────────────────────────────────────────", style=_DIM)
        return t

    def _build_dup_validation(self) -> Text:
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

    def _build_preview_duplicate(self, manifest: "SkillManifest") -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        tbl.add_row(Text(""))

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

        tbl.add_row(Text(" ─── what gets copied ────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        tools     = manifest.tools
        tools_str = f"{len(tools)} tools" if tools is not None else "all native tools"
        prompt_lines = len(manifest.prompt.splitlines())
        desc_preview = (manifest.description[:30] + "…") if len(manifest.description) > 30 else manifest.description
        fmt_val   = manifest.output_format or "stream"
        think_val = manifest.thinking_label or "—"
        checklist = [
            ("description",    desc_preview or "—"),
            ("tools",          tools_str),
            ("prompt",         f"{prompt_lines} lines"),
            ("output_format",  fmt_val),
            ("thinking_label", think_val),
            ("max_iterations", str(manifest.max_iterations)),
        ]
        if manifest.steps:
            checklist.append(("steps", f"{len(manifest.steps)} steps"))
        for item, value in checklist:
            row = Text()
            row.append("  ✓  ", style=f"bold {_GREEN}")
            row.append(f"{item:<16}", style=_DIM)
            row.append(value, style=_FAINT)
            tbl.add_row(row)
        tbl.add_row(Text(""))

        avail = self._dup_name_available()
        tbl.add_row(Text(" ─── result ──────────────────────────────────────────", style=_DIM))
        tbl.add_row(Text(""))
        result = Text()
        name_display = self._dup_name if self._dup_name else "<name>"
        path_display = self._dup_target_path_preview(self._dup_tier)
        if avail:
            result.append(f" new {self._dup_tier} skill  ", style=_DIM)
            result.append(name_display, style=f"bold {_tier_color(self._dup_tier)}")
            result.append(f"  at  {path_display}", style=_FAINT)
            result.append("  ·  opens in detail", style=_DIM)
        elif self._dup_name:
            result.append(f" new {self._dup_tier} skill  ", style=f"strike {_FAINT}")
            result.append(name_display, style=f"strike {_ORANGE}")
            result.append(f"  at  {path_display}", style=f"strike {_FAINT}")
            result.append("  ·  resolve name conflict first", style=_FAINT)
        else:
            result.append("fill in name above to preview", style=_FAINT)
        tbl.add_row(result)
        return tbl

    # ── Footer & hint helpers ─────────────────────────────────────────────────

    def _build_run_prompt_label(self) -> Text:
        t = Text()
        t.append(" ARGUMENT", style=f"bold {_DIM}")
        skill = next((m for m in self._registry.values() if m.name == self._run_skill_name), None)
        if skill and skill.args:
            arg0 = skill.args[0]
            t.append(f"  ·  {arg0.name}", style=_DIM)
            if arg0.description:
                t.append(f"  — {arg0.description}", style=_FAINT)
        return t

    def _build_prompt_edit_label(self) -> Text:
        t = Text()
        t.append(" PROMPT", style=f"bold {_DIM}")
        t.append("  ·  edit the full skill prompt", style=_DIM)
        return t

    def _build_field_edit_label(self, label: str) -> Text:
        t = Text()
        t.append(f" {label}", style=f"bold {_DIM}")
        return t

    def _build_edit_hints(self) -> Text:
        dot = f" [{_FAINT}]·[/] "
        parts = [_hint("ctrl+↵", "save"), _hint("esc", "cancel")]
        return Text.from_markup("  " + dot.join(parts))

    def _build_run_hints(self) -> Text:
        dot = f" [{_FAINT}]·[/] "
        parts = [_hint("ctrl+↵", "run skill"), _hint("esc", "cancel")]
        return Text.from_markup("  " + dot.join(parts))

    def _build_format_hints(self) -> Text:
        dot = f" [{_FAINT}]·[/] "
        parts = [_hint("↑↓", "pick format"), _hint("ctrl+↵", "save"), _hint("esc", "cancel")]
        if self._edit_format_cursor == 1:
            parts.insert(1, _hint("tab", "thinking label field"))
        return Text.from_markup("  " + dot.join(parts))

    def _build_create_hints(self) -> Text:
        dot = f" [{_FAINT}]·[/] "
        is_armed = self._create_name_valid() and self._create_name_available()
        parts: list[str] = [_hint("tab / shift+tab", "next / prev field"), _hint("↑↓", "switch option")]
        if is_armed:
            parts.append(f"[bold {_ORANGE} on #2a2a2a] ctrl+↵ [/] [{_ORANGE}]create & edit[/]")
        else:
            parts.append(f"[{_FAINT}]ctrl+↵  create & edit[/]")
        parts.append(_hint("esc", "cancel"))
        return Text.from_markup("  " + dot.join(parts))

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self) -> Text:
        dot = f" [{_FAINT}]·[/] "
        skill = self._current_skill()
        is_builtin = skill is not None and skill.source == "builtin"

        if self._mode == "confirm_delete":
            hints = [_hint("d", "confirm delete"), _hint("esc", "cancel")]
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
            hints = [_hint("ctrl+↵", "run skill"), _hint("esc", "cancel")]
            suffix = ""
        elif self._mode == "edit_tools":
            hints = [
                _hint("↑↓", "nav"),
                _hint("space", "toggle"),
                _hint("a", "select all in category"),
                _hint("↵", "save & close"),
                _hint("esc", "cancel"),
            ]
            suffix = ""
        elif self._mode in ("edit_description", "edit_prompt"):
            hints = [_hint("ctrl+↵", "save"), _hint("esc", "cancel")]
            suffix = ""
        elif self._mode == "edit_iterations":
            hints = [_hint("←→", "adjust"), _hint("↵", "save"), _hint("esc", "cancel")]
            suffix = ""
        elif self._mode == "edit_output_format":
            hints = [_hint("↑↓", "pick format"), _hint("ctrl+↵", "save"), _hint("esc", "cancel")]
            if self._edit_format_cursor == 1:
                hints.insert(1, _hint("tab", "thinking label"))
            suffix = ""
        elif self._mode == "detail":
            bar = f"  [{_FAINT}]|[/]  "
            nav_h = dot.join([_hint("↑↓", "scroll")])
            if not is_builtin:
                edit_h = dot.join([
                    _hint("b", "desc"), _hint("i", "iter"), _hint("t", "tools"),
                    _hint("o", "format"), _hint("p", "prompt"),
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
            return Text.from_markup("  " + bar.join(parts))
        elif self._mode == "create":
            is_armed = self._create_name_valid() and self._create_name_available()
            hints = [_hint("tab / shift+tab", "next / prev field"), _hint("↑↓", "switch option")]
            if is_armed:
                hints.append(f"[bold {_ORANGE} on #2a2a2a] ctrl+↵ [/] [{_ORANGE}]create & edit[/]")
            else:
                hints.append(f"[{_FAINT}]ctrl+↵  create & edit[/]")
            hints.append(_hint("esc", "cancel"))
            suffix = ""
        elif self._query:
            hints = [_hint("↑↓", "nav matches"), _hint("↵", "focus"), _hint("esc", "clear search")]
            suffix = ""
        else:
            hints = [
                _hint("↑↓", "nav"),
                _hint("tab", "scope"),
                _hint("↵", "focus"),
                _hint("/", "search"),
                _hint("r", "run"),
                _hint("y", "dup"),
                f"[bold {_ORANGE} on #2a2a2a] n [/] [{_ORANGE}]new[/]",
            ]
            if not is_builtin:
                hints += [_hint("e", "edit"), _hint("d", "delete")]
            hints.append(_hint("esc", "close"))
            suffix = "" if not is_builtin else f"  [{_FAINT}]read-only — edit/delete hidden[/]"

        return Text.from_markup("  " + dot.join(hints) + (suffix or ""))

    # ── Actions (BINDINGS) ────────────────────────────────────────────────────

    def check_action(self, action: str, parameters: tuple) -> bool | None:  # type: ignore[override]
        return True

    def action_nav_up(self) -> None:
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
        if self._mode == "edit_output_format":
            if self._edit_format_cursor > 0:
                self._edit_format_cursor -= 1
                self._refresh()
            return
        if self._focus_pane == "detail":
            self.query_one("#sk-preview-scroll", VerticalScroll).scroll_relative(y=-3)
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
                self._create_template_cursor = min(len(templates) - 1, self._create_template_cursor + 1)
                self._sync_template_selection()
                self._refresh()
            return
        if self._mode == "edit_tools":
            self._edit_tools_cursor = min(len(_NATIVE_TOOLS) - 1, self._edit_tools_cursor + 1)
            self._refresh()
            return
        if self._mode == "edit_output_format":
            if self._edit_format_cursor < len(_FORMAT_OPTIONS) - 1:
                self._edit_format_cursor += 1
                self._refresh()
            return
        if self._focus_pane == "detail":
            self.query_one("#sk-preview-scroll", VerticalScroll).scroll_relative(y=3)
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
        edit_modes = (
            "confirm_delete", "duplicate", "detail", "run",
            "edit_tools", "edit_description", "edit_iterations",
            "edit_prompt", "edit_output_format", "create",
        )
        if self._mode in edit_modes:
            was_create = self._mode == "create"
            self._mode = "browse"
            self._del_confirmed = False
            self._dup_name = ""
            self._dup_focus = "name"
            self._focus_pane = "list"
            if was_create:
                self._reset_create_state()
            self.query_one("#sk-panel", Vertical).focus()
            self._refresh()
        elif self._query or self._mode == "search":
            self.query_one("#sk-search", ModalSearchBar).clear()
            self._query = ""
            self._mode = "browse"
            self._rebuild_visible()
            self.query_one("#sk-panel", Vertical).focus()
            self._refresh()
        else:
            self.dismiss(self._registry_changed)

    def action_confirm(self) -> None:
        if self._mode == "create":
            focused = self.focused
            if isinstance(focused, TextArea) and focused.id == "sk-text-edit":
                focused.insert("\n")
                return
            if isinstance(focused, Input) and focused.id == "sk-dup-name":
                if self._create_name_available():
                    self._create_focus = "description"
                    self._update_create_widget_focus()
                    self._refresh()
                return
            return
        if self._mode == "edit_iterations":
            self._do_save_iterations()
            return
        if self._mode == "edit_tools":
            self._do_save_tools()
            return
        if self._mode == "edit_output_format":
            self._do_save_output_format()
            return
        if self._mode == "edit_prompt" and isinstance(self.focused, TextArea):
            self.query_one("#sk-text-edit", TextArea).insert("\n")
            return
        if self._mode == "edit_description" and isinstance(self.focused, TextArea):
            self.query_one("#sk-text-edit", TextArea).insert("\n")
            return
        if self._mode == "duplicate":
            if self._dup_focus == "name":
                self._dup_focus = "tier"
                self.query_one("#sk-panel", Vertical).focus()
                self._refresh()
            elif self._dup_focus == "tier" and self._dup_name_available():
                self._do_duplicate()
        elif self._mode in ("browse", "search") and self._visible:
            self._mode = "detail"
            self._focus_pane = "detail"
            self._refresh()

    def action_confirm_primary(self) -> None:
        """ctrl+enter / ctrl+j — create, save edits, or dispatch run."""
        mode = self._mode
        if mode == "create":
            if self._create_name_valid() and self._create_name_available():
                self._do_create()
        elif mode == "edit_prompt":
            self._do_save_prompt()
        elif mode == "edit_description":
            self._do_save_description()
        elif mode == "edit_output_format":
            self._do_save_output_format()
        elif mode == "run":
            arg = self.query_one("#sk-run-input", Input).value.strip()
            cmd = f"/{self._run_skill_name}"
            if arg:
                cmd += f" {arg}"
            self.dismiss(cmd)

    def action_cycle_scope(self) -> None:
        if self._mode == "create":
            order = ["name", "description", "tier", "starting_point"]
            if self._create_starting_point == "template":
                sp_idx = order.index("starting_point")
                order.insert(sp_idx + 1, "template_picker")
            curr = self._create_focus if self._create_focus in order else "name"
            if curr == "name" and not self._create_name_available() and self._create_name_valid():
                focused = self.focused
                if isinstance(focused, Input) and focused.id == "sk-dup-name":
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
        if self._mode == "edit_output_format":
            if self._edit_format_cursor == 1:
                self.query_one("#sk-thinking-label", Input).focus()
            return
        if self._mode == "duplicate":
            if self._dup_focus == "name":
                self._dup_focus = "tier"
                self.query_one("#sk-panel", Vertical).focus()
            else:
                self._dup_focus = "name"
                self.query_one("#sk-dup-name", Input).focus()
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
                self.query_one("#sk-dup-name", Input).focus()
            else:
                self._dup_focus = "tier"
                self.query_one("#sk-panel", Vertical).focus()
            self._refresh()
            return

    # ── Key dispatch ──────────────────────────────────────────────────────────

    def on_key(self, event: Key) -> None:
        key = event.key

        try:
            focused = self.focused
        except Exception:
            focused = None

        # When dup-name input has focus, only handle esc and tab-accept
        if isinstance(focused, Input) and focused.id == "sk-dup-name":
            if key == "escape":
                prev_mode = self._mode
                self._mode = "browse"
                if prev_mode == "create":
                    self._reset_create_state()
                else:
                    self._dup_name = ""
                    self._dup_focus = "name"
                self.query_one("#sk-panel", Vertical).focus()
                self._refresh()
                event.stop()
            elif (
                key == "tab"
                and focused.id == "sk-dup-name"
                and self._mode == "duplicate"
                and self._dup_name
                and not self._dup_name_available()
            ):
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

        if mode == "edit_tools":
            if key == "space":
                self._toggle_tool_at_cursor()
                event.stop()
            elif key == "a":
                self._toggle_category_at_cursor()
                event.stop()
            return

        if mode == "run":
            return

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

        if mode == "edit_output_format":
            if key in ("left", "right"):
                # ← → also navigate format options
                delta = -1 if key == "left" else 1
                new_cursor = max(0, min(len(_FORMAT_OPTIONS) - 1, self._edit_format_cursor + delta))
                if new_cursor != self._edit_format_cursor:
                    self._edit_format_cursor = new_cursor
                    self._refresh()
                    event.stop()
            return

        if mode in ("edit_prompt", "edit_description", "create"):
            return

        # Browse / search / detail
        skill = self._current_skill()
        is_builtin = skill is not None and skill.source == "builtin"

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
        elif key in ("b", "e") and not is_builtin:
            self._start_edit_description()
            event.stop()
        elif key == "i" and not is_builtin:
            self._start_edit_iterations()
            event.stop()
        elif key == "p" and not is_builtin:
            self._start_edit_prompt()
            event.stop()
        elif key == "o" and not is_builtin:
            self._start_edit_output_format()
            event.stop()
        elif key == "slash":
            self.query_one("#sk-search", ModalSearchBar).focus_input()
            self._mode = "search"
            self._refresh()
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
        if event.input.id == "sk-dup-name":
            if self._mode == "create":
                self._create_name = event.value.strip()
            else:
                self._dup_name = event.value.strip()
            self._refresh()
        elif event.input.id == "sk-thinking-label":
            self._edit_thinking_label = event.value
            self._refresh()
        elif event.input.id == "sk-run-input":
            return
        else:
            self._query = event.value.strip().lower()
            self._mode = "search" if self._query else "browse"
            self._rebuild_visible()
            self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "sk-dup-name":
            if self._mode == "create":
                if self._create_name_available():
                    self._create_focus = "tier"
                    self._update_create_widget_focus()
                    self._refresh()
            elif self._dup_name_available():
                self._dup_focus = "tier"
                self.query_one("#sk-panel", Vertical).focus()
                self._refresh()
        elif event.input.id == "sk-run-input":
            # Enter on run input dispatches (same as ctrl+enter)
            arg = event.input.value.strip()
            cmd = f"/{self._run_skill_name}"
            if arg:
                cmd += f" {arg}"
            self.dismiss(cmd)
        else:
            self.query_one("#sk-panel", Vertical).focus()
            self._mode = "browse" if not self._query else "search"
            self._refresh()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._mode == "create" and event.text_area.id == "sk-text-edit":
            self._create_desc = event.text_area.text
            if self._create_desc_inherited:
                self._create_desc_inherited = False
            self._refresh()

    # ── Disk operations ───────────────────────────────────────────────────────

    def _start_delete(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin":
            return
        self._mode = "confirm_delete"
        self._del_confirmed = False
        self._refresh()

    def _do_delete(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source_path is None:
            return
        try:
            skill.source_path.unlink()
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._del_confirmed = False
        self._focus_pane = "list"
        self._refresh()

    def _start_duplicate(self) -> None:
        skill = self._current_skill()
        if skill is None:
            return
        self._dup_name = f"{skill.name}-copy"
        self._dup_tier = "user"
        self._dup_focus = "name"
        self._mode = "duplicate"
        self._refresh()
        dup_input = self.query_one("#sk-dup-name", Input)
        dup_input.value = self._dup_name
        dup_input.focus()

    def _do_duplicate(self) -> None:
        source_skill = self._current_skill()
        if source_skill is None or source_skill.source_path is None:
            return
        if not self._dup_name_available():
            return
        target_path = self._dup_target_path(self._dup_tier)
        dup_name = self._dup_name
        try:
            raw = yaml.safe_load(source_skill.source_path.read_text(encoding="utf-8")) or {}
            raw["name"] = dup_name
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(
                yaml.dump(raw, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        new_idx = next(
            (i for i, m in enumerate(self._visible) if m.name == dup_name and m.source == self._dup_tier),
            0,
        )
        self._selected = new_idx
        self._mode = "detail"
        self._focus_pane = "detail"
        self._dup_name = ""
        self._dup_focus = "name"
        self.query_one("#sk-panel", Vertical).focus()
        self._refresh()

    # ── Create ────────────────────────────────────────────────────────────────

    def _start_create(self) -> None:
        self._reset_create_state()
        self._mode = "create"
        self._refresh()
        inp = self.query_one("#sk-dup-name", Input)
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
        try:
            ta = self.query_one("#sk-text-edit", TextArea)
            ta.load_text("")
        except Exception:
            pass

    def _do_create(self) -> None:
        name = self._create_name
        tier = self._create_tier
        desc = self._create_desc.strip()

        if self._create_starting_point == "blank":
            tools: Optional[list[str]] = None   # all tools for new skills
            output_format = "stream"
            thinking_label = ""
            max_iterations = 20
            prompt = self._create_scaffold_prompt(name, desc)
            steps: list[str] = []
        else:
            templates = self._get_template_list()
            if not (0 <= self._create_template_cursor < len(templates)):
                return
            tmpl = templates[self._create_template_cursor]
            tools = list(tmpl.tools) if tmpl.tools is not None else None
            output_format = tmpl.output_format or "stream"
            thinking_label = tmpl.thinking_label or ""
            max_iterations = tmpl.max_iterations
            prompt = tmpl.prompt
            steps = list(tmpl.steps) if tmpl.steps else []
            if not desc:
                desc = tmpl.description

        desc_val = desc if desc else "a helpful skill."
        raw: dict = {
            "name": name,
            "description": desc_val,
            "prompt": prompt,
            "max_iterations": max_iterations,
            "output_format": output_format,
        }
        if tools is not None:
            raw["tools"] = tools
        if thinking_label:
            raw["thinking_label"] = thinking_label
        if steps:
            raw["steps"] = steps

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
        self.query_one("#sk-panel", Vertical).focus()
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

    # ── Run ───────────────────────────────────────────────────────────────────

    def _action_run(self) -> None:
        skill = self._current_skill()
        if skill is None:
            return
        self._run_skill_name = skill.name
        self._mode = "run"
        self._focus_pane = "detail"
        self._refresh()
        run_input = self.query_one("#sk-run-input", Input)
        run_input.value = ""
        run_input.focus()

    # ── Edit tools ────────────────────────────────────────────────────────────

    def _start_edit_tools(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin":
            return
        self._edit_tools = list(skill.tools) if skill.tools is not None else list(_NATIVE_TOOLS)
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
        skill = self._current_skill()
        if skill is None or skill.source_path is None or skill.source == "builtin":
            return
        from ...skills.persist import update_skill_yaml
        tools_to_save: Optional[list[str]] = self._edit_tools
        if sorted(self._edit_tools) == sorted(_NATIVE_TOOLS):
            tools_to_save = None
        try:
            update_skill_yaml(skill.source_path, {"tools": tools_to_save})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self._refresh()

    # ── Edit description / prompt / iterations ────────────────────────────────

    def _start_edit_description(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin" or skill.source_path is None:
            return
        ta = self.query_one("#sk-text-edit", TextArea)
        ta.load_text(skill.description)
        self._mode = "edit_description"
        self._focus_pane = "detail"
        self._refresh()
        ta.focus()

    def _do_save_description(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin" or skill.source_path is None:
            return
        new_val = self.query_one("#sk-text-edit", TextArea).text.strip().replace("\n", " ")
        if new_val:
            from ...skills.persist import update_skill_yaml
            update_skill_yaml(skill.source_path, {"description": new_val})
            self._registry_changed = True
            self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self.query_one("#sk-panel", Vertical).focus()
        self._refresh()

    def _start_edit_iterations(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin" or skill.source_path is None:
            return
        self._edit_iterations_val = skill.max_iterations
        self._mode = "edit_iterations"
        self._focus_pane = "detail"
        self._refresh()

    def _do_save_iterations(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin" or skill.source_path is None:
            return
        from ...skills.persist import update_skill_yaml
        update_skill_yaml(skill.source_path, {"max_iterations": self._edit_iterations_val})
        self._registry_changed = True
        self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self.query_one("#sk-panel", Vertical).focus()
        self._refresh()

    def _start_edit_prompt(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin" or skill.source_path is None:
            return
        ta = self.query_one("#sk-text-edit", TextArea)
        ta.load_text(skill.prompt)
        self._mode = "edit_prompt"
        self._focus_pane = "detail"
        self._refresh()
        ta.focus()

    def _do_save_prompt(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin" or skill.source_path is None:
            return
        new_prompt = self.query_one("#sk-text-edit", TextArea).text.strip()
        if new_prompt:
            from ...skills.persist import update_skill_yaml
            update_skill_yaml(skill.source_path, {"prompt": new_prompt})
            self._registry_changed = True
            self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self.query_one("#sk-panel", Vertical).focus()
        self._refresh()

    # ── Edit output format ────────────────────────────────────────────────────

    def _start_edit_output_format(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source == "builtin" or skill.source_path is None:
            return
        fmt = skill.output_format or "stream"
        self._edit_format_cursor = next(
            (i for i, (f, _) in enumerate(_FORMAT_OPTIONS) if f == fmt),
            0,
        )
        self._edit_thinking_label = skill.thinking_label or ""
        self._mode = "edit_output_format"
        self._focus_pane = "detail"
        self._refresh()
        inp = self.query_one("#sk-thinking-label", Input)
        inp.value = self._edit_thinking_label

    def _do_save_output_format(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source_path is None or skill.source == "builtin":
            return
        fmt = _FORMAT_OPTIONS[self._edit_format_cursor][0]
        label = self._edit_thinking_label.strip() if fmt == "markdown" else ""
        from ...skills.persist import update_skill_yaml
        try:
            update_skill_yaml(skill.source_path, {
                "output_format": fmt,
                "thinking_label": label if label else None,
            })
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "detail"
        self._focus_pane = "detail"
        self.query_one("#sk-panel", Vertical).focus()
        self._refresh()

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _copy_path(self) -> None:
        skill = self._current_skill()
        if skill is None or skill.source_path is None:
            return
        try:
            import subprocess
            subprocess.run(["pbcopy"], input=str(skill.source_path).encode(), check=False)
        except (OSError, FileNotFoundError):
            pass

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="sk-panel"):
            yield Static("", id="sk-header")
            with Horizontal(id="sk-body"):
                with Vertical(id="sk-list-pane"):
                    yield Static("", id="sk-create-banner")
                    yield ModalSearchBar(placeholder="search skills…", id="sk-search")
                    with VerticalScroll(id="sk-list-scroll"):
                        yield Static("", id="sk-list")
                with Vertical(id="sk-preview-pane"):
                    yield Static("", id="sk-dup-top")
                    yield Input(placeholder="new skill name…", id="sk-dup-name")
                    yield Static("", id="sk-dup-validation")
                    yield Static("", id="sk-run-prompt-label")
                    yield Input(placeholder="argument…", id="sk-run-input")
                    yield TextArea("", id="sk-text-edit")
                    with VerticalScroll(id="sk-preview-scroll"):
                        yield Static("", id="sk-preview")
                    yield Input(placeholder="thinking label (optional)…", id="sk-thinking-label")
                    yield Static("", id="sk-run-hints")
            yield Static("", id="sk-footer")

    def on_mount(self) -> None:
        self._rebuild_visible()
        panel = self.query_one("#sk-panel", Vertical)
        panel.can_focus = True
        panel.focus()
        self.query_one("#sk-dup-top", Static).display = False
        self.query_one("#sk-dup-name", Input).display = False
        self.query_one("#sk-dup-validation", Static).display = False
        self.query_one("#sk-run-prompt-label", Static).display = False
        self.query_one("#sk-run-input", Input).display = False
        self.query_one("#sk-text-edit", TextArea).display = False
        self.query_one("#sk-thinking-label", Input).display = False
        self.query_one("#sk-run-hints", Static).display = False
        self.query_one("#sk-create-banner", Static).display = False
        self._refresh()
