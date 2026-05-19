"""HooksScreen — /hooks modal for the Textual TUI.

States:
  browse         — full list, detail of focused hook, action hints.
  create_step1   — name + tier wizard step.
  create_step2   — event + tool filter wizard step.
  create_step3   — command + timeout + blocking + description (final create step).
  edit           — edit an existing user/project hook (same right-pane layout as create_step3).
  confirm_delete — delete confirmation with yes/cancel radio.

Layered esc:
  confirm_delete  → back to browse (no deletion)
  create_step*    → back to browse (no file created)
  edit            → back to browse (discard changes)
  search active   → clear query → full list
  otherwise       → dismiss modal
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from rich.panel import Panel
from rich.rule import Rule as RichRule
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
    _NATIVE_TOOLS, _TOOL_DESCRIPTIONS, _TOOL_WARN, _TOOL_CATEGORIES,
    _highlight, _tier_color, _hint,
)

if TYPE_CHECKING:
    from ...hooks.registry import HookRegistry
    from ...hooks.manifest import HookManifest


# ── Module-level constants ────────────────────────────────────────────────────

_TINT_GOLD = "#1a1200"   # focused list row background (GOLD selection)
_TINT_WARN = "#1a0a04"   # orange warning / delete / blocking tint

_EVENTS: list[tuple[str, str]] = [
    ("PreToolUse",       "before each tool call runs"),
    ("PostToolUse",      "after each tool call completes"),
    ("SessionStart",     "when a session begins"),
    ("SessionEnd",       "when a session ends"),
    ("UserPromptSubmit", "when the user submits a prompt"),
    ("StopTurn",         "after the model finishes a turn"),
]

_BLOCKING_OPTIONS = ["auto", "true", "false"]

_SLUG_RE = re.compile(r'^[a-z][a-z0-9-]*$')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hook_source(entry: Any) -> str:
    """Return source string from either a dict (builtin) or HookManifest."""
    if isinstance(entry, dict):
        return entry.get("source", "builtin")
    return str(getattr(entry, "source", "user"))


def _hook_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return entry.get("name", "")
    return str(getattr(entry, "name", ""))


def _hook_event(entry: Any) -> str:
    if isinstance(entry, dict):
        return entry.get("event", "")
    return str(getattr(entry, "event", ""))


def _hook_description(entry: Any) -> str:
    if isinstance(entry, dict):
        return entry.get("detail", "")
    return str(getattr(entry, "description", ""))


def _hook_command(entry: Any) -> str:
    if isinstance(entry, dict):
        return entry.get("detail", "")
    return str(getattr(entry, "command", ""))


def _hook_tools(entry: Any) -> list[str]:
    """Return tool filter list. Empty list means all tools."""
    if isinstance(entry, dict):
        raw = entry.get("tools", entry.get("tool", None))
        if isinstance(raw, list):
            return [str(t) for t in raw if t]
        return [str(raw)] if raw else []
    tools = getattr(entry, "tools", None)
    return list(tools) if tools else []


def _hook_timeout(entry: Any) -> int:
    if isinstance(entry, dict):
        return 30
    return int(getattr(entry, "timeout", 30))


def _hook_blocking(entry: Any) -> Optional[bool]:
    if isinstance(entry, dict):
        return None
    return getattr(entry, "blocking", None)


def _hook_type(entry: Any) -> str:
    if isinstance(entry, dict):
        return entry.get("type", "python")
    return "shell"


def _hook_source_path(entry: Any) -> Optional[Path]:
    if isinstance(entry, dict):
        return None
    return getattr(entry, "source_path", None)


def _fmt_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _hook_tier_path(tier: str, cwd: Path) -> Path:
    if tier == "user":
        return Path.home() / ".minion" / "hooks"
    return cwd / ".minion" / "hooks"


def _blocking_from_cursor(cursor: int) -> Optional[bool]:
    return {0: None, 1: True, 2: False}.get(cursor, None)


def _cursor_from_blocking(val: Optional[bool]) -> int:
    return {None: 0, True: 1, False: 2}.get(val, 0)


def _event_badge(event: str) -> Text:
    """Short event label with color: Pre* = orange, others = silver."""
    badge = Text(no_wrap=True)
    if event.startswith("Pre"):
        badge.append(event, style=_ORANGE)
    else:
        badge.append(event, style=_SILVER)
    return badge


def _wiz_section(label: str) -> Text:
    """Wizard-style section header: `─── label ──────────────`"""
    t = Text(no_wrap=True)
    t.append(f" ─── {label} ", style=_DIM)
    t.append("─" * 60, style=_DIM)
    return t


def _detail_section(label: str, hint_key: str = "", hint_value: str = "") -> Text:
    """Detail-view ALL-CAPS section header with optional inline edit hint."""
    t = Text()
    t.append(f" {label}", style=f"bold {_DIM}")
    if hint_key:
        t.append("   ")
        t.append(f" {hint_key} ", style=f"bold {_SILVER} on #2a2a2a")
        t.append(" edit", style=_DIM)
    if hint_value:
        t.append(f"  ·  {hint_value}", style=_DIM)
    return t


# ── Screen ────────────────────────────────────────────────────────────────────


class HooksScreen(ModalScreen):  # type: ignore[type-arg]
    """Full-screen split-pane hook browser opened by /hooks."""

    CSS = f"""
HooksScreen {{
    align: center middle;
    background: #000000 40%;
}}
#hk-panel {{
    width: 90%;
    height: 90%;
    background: {_BG};
    border: round {_RULE};
}}
#hk-header {{
    height: auto;
    padding: 0 2;
    border-bottom: solid {_RULE};
}}
#hk-body {{
    height: 1fr;
}}
#hk-list-pane {{
    width: 50%;
    border-right: solid {_RULE};
}}
#hk-list-pane.lhs-focused {{
    border-right: solid {_ORANGE};
}}
#hk-list-pane.rhs-focused {{
    border-right: solid {_BLUE};
}}
#hk-list-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {_DIM};
}}
#hk-list {{
    height: auto;
}}
#hk-detail-pane {{
    width: 50%;
    padding: 0 1;
}}
#hk-preview-scroll {{
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {_DIM};
}}
#hk-preview-scroll.text-edit-compact {{
    height: auto;
}}
#hk-preview {{
    height: auto;
}}
#hk-wiz-top {{
    height: auto;
    display: none;
    padding: 1 1 0 1;
}}
#hk-name-input {{
    height: auto;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    padding: 0 1;
}}
#hk-name-input:focus {{
    border: solid {_ORANGE};
}}
#hk-name-meta {{
    height: auto;
    display: none;
    padding: 0 2;
}}
#hk-edit-top {{
    height: auto;
    display: none;
    padding: 1 1 0 1;
}}
#hk-edit-top.no-top-pad {{
    padding: 0 1 0 0;
}}
#hk-cmd-area {{
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
#hk-cmd-area:focus {{
    border: solid {_ORANGE};
}}
#hk-cmd-area .text-area--cursor-line {{
    background: #1a1a1a;
}}
#hk-cmd-area.desc-edit {{
    height: 12;
}}
#hk-cmd-area.prompt-edit {{
    height: 14;
}}
#hk-cmd-meta {{
    height: auto;
    display: none;
    padding: 0 2;
}}
#hk-edit-controls {{
    height: auto;
    display: none;
    padding: 0 1;
}}
#hk-desc-input {{
    height: auto;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    padding: 0 1;
}}
#hk-desc-input:focus {{
    border: solid {_ORANGE};
}}
#hk-create-desc-label {{
    height: auto;
    display: none;
    padding: 0 1;
}}
#hk-create-desc-input {{
    height: auto;
    display: none;
    background: #1a1a1a;
    border: solid #3a3a3a;
    color: #E8E8E8;
    margin: 0 1;
    padding: 0 1;
}}
#hk-create-desc-input:focus {{
    border: solid {_ORANGE};
}}
#hk-del-confirm {{
    height: auto;
    display: none;
    padding: 0 1;
}}
#hk-run-hints {{
    height: 2;
    display: none;
    background: {_BG};
    border-top: solid {_RULE};
    padding: 0 2;
    margin: 0 -1;
}}
#hk-footer {{
    height: 2;
    padding: 0 2;
    background: {_BG};
    border-top: solid {_RULE};
}}
"""

    BINDINGS = [
        Binding("escape",     "esc_action",        show=False, priority=True),
        Binding("up",         "nav_up",             show=False, priority=True),
        Binding("down",       "nav_down",           show=False, priority=True),
        Binding("enter",      "confirm",            show=False, priority=True),
        Binding("tab",        "cycle_scope",        show=False, priority=True),
        Binding("shift+tab",  "cycle_scope_back",   show=False, priority=True),
        Binding("ctrl+enter", "confirm_primary",    show=False, priority=True),
        Binding("ctrl+j",     "confirm_primary",    show=False, priority=True),
    ]

    def __init__(
        self,
        hook_registry: "HookRegistry",
        cwd: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._registry = hook_registry
        self._cwd: Path = cwd or Path.cwd()
        self._mode: str = "browse"
        self._scope: str = "all"
        self._query: str = ""
        self._selected: int = 0
        self._focus_pane: str = "list"
        self._visible: list[Any] = []

        # Create flow
        self._create_name: str = ""
        self._create_desc: str = ""
        self._create_tier: str = "user"
        self._create_event_cursor: int = 0
        self._create_tool_cursor: int = 0   # 0 = all tools, 1-N = index into _NATIVE_TOOLS
        self._create_focus: str = "name"    # "name" | "desc" | "tier" | "event" | "command"

        # Edit / create_step3 (shared)
        self._edit_cmd: str = ""
        self._edit_timeout_val: int = 30
        self._edit_blocking_cursor: int = 0   # 0=auto, 1=true, 2=false
        self._edit_desc: str = ""
        self._edit_focus: str = "command"  # "command" | "timeout" | "blocking" | "description"

        # Per-field edit state
        self._edit_event_cursor: int = 0
        self._edit_tools: list[str] = []        # selected tool names (empty = all tools)
        self._edit_tools_saved: list[str] = []  # snapshot for change detection
        self._edit_tools_cursor: int = 0        # flat index into _NATIVE_TOOLS for nav

        # Delete flow (no radio — d again confirms, esc cancels)

        self._registry_changed: bool = False

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="hk-panel"):
            yield Static("", id="hk-header")
            with Horizontal(id="hk-body"):
                with Vertical(id="hk-list-pane"):
                    yield ModalSearchBar(placeholder="search hooks…", id="hk-search")
                    with VerticalScroll(id="hk-list-scroll"):
                        yield Static("", id="hk-list")
                with Vertical(id="hk-detail-pane"):
                    # Single scroll spanning the full pane; all form widgets live inside it
                    with VerticalScroll(id="hk-preview-scroll"):
                        yield Static("", id="hk-wiz-top")
                        yield Input(placeholder="hook-name", id="hk-name-input")
                        yield Static("", id="hk-name-meta")
                        yield Static("", id="hk-create-desc-label")
                        yield Input(placeholder="optional description", id="hk-create-desc-input")
                        yield Static("", id="hk-preview")
                        yield Static("", id="hk-edit-top")
                        yield TextArea("", id="hk-cmd-area")
                        yield Static("", id="hk-cmd-meta")
                        yield Static("", id="hk-edit-controls")
                        yield Input(placeholder="optional description", id="hk-desc-input")
                        yield Static("", id="hk-del-confirm")
                    # Hints strip pinned below the scroll
                    yield Static("", id="hk-run-hints")
            yield Static("", id="hk-footer")

    def on_mount(self) -> None:
        self._rebuild_visible()
        panel = self.query_one("#hk-panel", Vertical)
        panel.can_focus = True
        panel.focus()
        # Above-scroll and inner-scroll widgets start hidden; _refresh() controls visibility
        for wid in (
            "#hk-wiz-top", "#hk-name-meta", "#hk-create-desc-label",
            "#hk-edit-top", "#hk-cmd-meta", "#hk-edit-controls", "#hk-del-confirm",
            "#hk-run-hints",
        ):
            self.query_one(wid, Static).display = False
        self.query_one("#hk-name-input", Input).display = False
        self.query_one("#hk-create-desc-input", Input).display = False
        self.query_one("#hk-cmd-area", TextArea).display = False
        self.query_one("#hk-desc-input", Input).display = False
        self._refresh()

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _all_hooks(self) -> list[Any]:
        """Builtin dicts first, then user hooks, then project hooks, alphabetically within each."""
        builtins: list[Any] = self._registry.builtin_manifests()
        yaml_hooks = sorted(
            [m for _, m in self._registry.items()],
            key=lambda m: ({"user": 0, "project": 1}.get(m.source, 2), m.name),
        )
        return [*builtins, *yaml_hooks]

    def _rebuild_visible(self) -> None:
        hooks = self._all_hooks()
        if self._scope != "all" and not self._query:
            hooks = [h for h in hooks if _hook_source(h) == self._scope]
        if self._query:
            q = self._query.lower()
            hooks = [
                h for h in hooks
                if q in _hook_name(h).lower()
                or q in _hook_description(h).lower()
                or q in _hook_event(h).lower()
            ]
        self._visible = hooks
        if self._selected >= len(self._visible):
            self._selected = max(0, len(self._visible) - 1)

    def _reload_registry(self) -> None:
        from ...hooks.registry import HookRegistry
        self._registry = HookRegistry.load(self._cwd, self._registry._config)
        self._rebuild_visible()

    def _current_hook(self) -> Optional[Any]:
        if not self._visible:
            return None
        return self._visible[self._selected]

    def _tier_counts(self) -> dict[str, int]:
        all_hooks = self._all_hooks()
        counts: dict[str, int] = {"builtin": 0, "user": 0, "project": 0}
        for h in all_hooks:
            src = _hook_source(h)
            counts[src] = counts.get(src, 0) + 1
        return counts

    def _is_builtin(self, entry: Any) -> bool:
        return _hook_source(entry) == "builtin"

    # ── Create helpers ────────────────────────────────────────────────────────

    def _check_name_available(self, name: str, tier: str) -> tuple[bool, str]:
        if not _SLUG_RE.match(name):
            return False, "name must match [a-z][a-z0-9-]+"
        tier_dir = _hook_tier_path(tier, self._cwd)
        path = tier_dir / f"{name}.yaml"
        if path.exists():
            return False, "✗ name taken — pick another"
        return True, f"✓ available  {_fmt_path(path)}"

    def _create_target_path(self) -> Path:
        tier_dir = _hook_tier_path(self._create_tier, self._cwd)
        return tier_dir / f"{self._create_name}.yaml"

    def _create_target_path_preview(self) -> str:
        tier_dir_str = (
            f"~/.minion/hooks/{self._create_name or '<name>'}.yaml"
            if self._create_tier == "user"
            else f".minion/hooks/{self._create_name or '<name>'}.yaml"
        )
        return tier_dir_str

    def _create_name_ok(self) -> tuple[bool, str]:
        name = self._create_name
        if not name:
            return False, ""
        return self._check_name_available(name, self._create_tier)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._rebuild_visible()

        self.query_one("#hk-header", Static).update(self._build_header())
        self.query_one("#hk-footer", Static).update(self._build_footer())
        self.query_one("#hk-list", Static).update(self._build_list())

        # Divider border: orange = left focused, blue = right focused
        _EDIT_MODES = {
            "create", "confirm_delete",
            "edit_command", "edit_timeout", "edit_blocking", "edit_description", "edit_event", "edit_tool",
        }
        list_pane = self.query_one("#hk-list-pane", Vertical)
        if self._focus_pane == "detail" or self._mode in _EDIT_MODES:
            list_pane.remove_class("lhs-focused")
            list_pane.add_class("rhs-focused")
        else:
            list_pane.remove_class("rhs-focused")
            list_pane.add_class("lhs-focused")

        # #hk-preview-scroll is always visible — never hide it.
        # Reset: hide all above-scroll and inner-scroll widgets each cycle.
        for wid in (
            "#hk-wiz-top", "#hk-name-meta", "#hk-create-desc-label",
            "#hk-edit-top", "#hk-cmd-meta", "#hk-edit-controls", "#hk-del-confirm",
            "#hk-run-hints",
        ):
            self.query_one(wid, Static).display = False
        self.query_one("#hk-edit-top", Static).remove_class("no-top-pad")
        self.query_one("#hk-name-input", Input).display = False
        self.query_one("#hk-create-desc-input", Input).display = False
        cmd_area = self.query_one("#hk-cmd-area", TextArea)
        cmd_area.display = False
        cmd_area.remove_class("desc-edit")
        cmd_area.remove_class("prompt-edit")
        self.query_one("#hk-desc-input", Input).display = False
        self.query_one("#hk-preview-scroll", VerticalScroll).remove_class("text-edit-compact")

        if self._mode in ("browse", "detail", "search"):
            self.query_one("#hk-preview", Static).display = True
            self.query_one("#hk-preview", Static).update(self._build_detail())

        elif self._mode == "create":
            # Above-scroll: FROM heading + name + desc inputs
            self.query_one("#hk-wiz-top", Static).display = True
            self.query_one("#hk-wiz-top", Static).update(self._build_create_top())
            self.query_one("#hk-name-input", Input).display = True
            self.query_one("#hk-name-meta", Static).display = True
            if not self._create_name:
                name_meta = Text()
                name_meta.append("  letters, digits, dashes  ·  must start with a letter\n", style=_FAINT)
            else:
                ok, msg = self._create_name_ok()
                name_meta = Text()
                name_meta.append(f"  {msg}\n", style=f"bold {_GREEN}" if ok else _ORANGE)
            self.query_one("#hk-name-meta", Static).update(name_meta)
            self.query_one("#hk-create-desc-label", Static).display = True
            self.query_one("#hk-create-desc-label", Static).update(self._build_create_desc_label())
            self.query_one("#hk-create-desc-input", Input).display = True
            # Scroll content: tier + event + note
            self.query_one("#hk-preview", Static).update(self._build_create_scroll_content())
            # Inside-scroll: command section
            self.query_one("#hk-edit-top", Static).display = True
            self.query_one("#hk-edit-top", Static).update(self._build_edit_top())
            cmd_area = self.query_one("#hk-cmd-area", TextArea)
            cmd_area.display = True
            if cmd_area.text != self._edit_cmd:
                cmd_area.load_text(self._edit_cmd)
            self.query_one("#hk-cmd-meta", Static).display = True
            lines = self._edit_cmd.count("\n") + 1 if self._edit_cmd else 0
            chars = len(self._edit_cmd)
            self.query_one("#hk-cmd-meta", Static).update(
                Text(f"  {chars} chars · {lines} line{'s' if lines != 1 else ''}", style=_DIM)
            )
            # Summary block below command
            self.query_one("#hk-edit-controls", Static).display = True
            self.query_one("#hk-edit-controls", Static).update(self._build_create_what())
            # Hints strip pinned below scroll
            self.query_one("#hk-run-hints", Static).display = True
            self.query_one("#hk-run-hints", Static).update(self._build_run_hints())
            # Focus routing
            if self._create_focus == "name":
                self.query_one("#hk-name-input", Input).focus()
            elif self._create_focus == "desc":
                self.query_one("#hk-create-desc-input", Input).focus()
            elif self._create_focus == "command":
                cmd_area.focus()

        elif self._mode == "edit_command":
            self.query_one("#hk-preview", Static).display = False
            edit_top = self.query_one("#hk-edit-top", Static)
            edit_top.add_class("no-top-pad")
            edit_top.display = True
            edit_top.update(self._build_edit_pane_label("COMMAND"))
            cmd_area = self.query_one("#hk-cmd-area", TextArea)
            cmd_area.add_class("prompt-edit")
            cmd_area.display = True
            if cmd_area.text != self._edit_cmd:
                cmd_area.load_text(self._edit_cmd)
            self.query_one("#hk-run-hints", Static).display = True
            self.query_one("#hk-run-hints", Static).update(self._build_run_hints())
            cmd_area.focus()

        elif self._mode == "edit_timeout":
            self.query_one("#hk-preview", Static).display = True
            self.query_one("#hk-preview", Static).update(self._build_edit_timeout_pane())

        elif self._mode == "edit_blocking":
            self.query_one("#hk-preview", Static).display = True
            self.query_one("#hk-preview", Static).update(self._build_edit_blocking_pane())

        elif self._mode == "edit_event":
            self.query_one("#hk-preview", Static).display = True
            self.query_one("#hk-preview", Static).update(self._build_edit_event_pane())

        elif self._mode == "edit_tool":
            self.query_one("#hk-preview", Static).display = True
            self.query_one("#hk-preview", Static).update(self._build_edit_tool_pane())

        elif self._mode == "edit_description":
            self.query_one("#hk-preview", Static).display = False
            edit_top = self.query_one("#hk-edit-top", Static)
            edit_top.add_class("no-top-pad")
            edit_top.display = True
            edit_top.update(self._build_edit_pane_label("DESCRIPTION"))
            desc_area = self.query_one("#hk-cmd-area", TextArea)
            desc_area.add_class("desc-edit")
            desc_area.display = True
            if desc_area.text != self._edit_desc:
                desc_area.load_text(self._edit_desc)
            desc_area.focus()
            self.query_one("#hk-run-hints", Static).display = True
            self.query_one("#hk-run-hints", Static).update(self._build_run_hints())
            self.query_one("#hk-preview-scroll", VerticalScroll).add_class("text-edit-compact")

        elif self._mode == "confirm_delete":
            self.query_one("#hk-preview", Static).update(Text(""))
            self.query_one("#hk-del-confirm", Static).display = True
            self.query_one("#hk-del-confirm", Static).update(self._build_delete_confirm())

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> Table:
        t = Text()
        t.append("┌─ ", style=_FAINT)
        t.append("/hooks", style=f"bold {_TEXT}")

        _FIELD_EDIT_LABELS = {
            "edit_command": "command",
            "edit_timeout": "timeout",
            "edit_blocking": "blocking",
            "edit_description": "description",
            "edit_event": "event",
            "edit_tool": "tool filter",
        }
        _SUB_MODES = {
            "create", "confirm_delete",
            *_FIELD_EDIT_LABELS,
        }
        if self._mode in _SUB_MODES:
            t.append(" › ", style=_DIM)
            hook = self._current_hook()
            if self._mode == "create":
                t.append("create new hook", style=f"bold {_ORANGE}")
            elif self._mode in _FIELD_EDIT_LABELS and hook:
                t.append(f"{_hook_name(hook)} › ", style=_DIM)
                t.append(_FIELD_EDIT_LABELS[self._mode], style=f"bold {_ORANGE}")
            elif self._mode == "confirm_delete" and hook:
                t.append("delete hook", style=_DIM)
        elif self._query:
            t.append(" — ", style=_DIM)
            count = len(self._visible)
            t.append("browse hooks", style=_DIM)
            t.append("  ")
            noun = "match" if count == 1 else "matches"
            t.append(f" {count} {noun} ", style=f"{_ORANGE} on #1a0800")
        else:
            t.append(" — ", style=_DIM)
            t.append("browse hooks", style=_SILVER)

        row = Table.grid(expand=True, padding=0)
        row.add_column(ratio=1)
        row.add_column(no_wrap=True, justify="right")

        # Right side: tier counts in browse, path preview in create
        if self._mode == "create":
            ok, _ = self._create_name_ok()
            right = Text(justify="right", style=_FAINT)
            right.append(f" {self._create_target_path_preview()} ", style=_DIM)
            row.add_row(t, right)
        elif self._mode not in _SUB_MODES:
            counts = self._tier_counts()
            right = Text(justify="right")
            right.append(f" {counts.get('builtin', 0)} builtin ", style=f"{_FAINT} on #161614")
            right.append(" · ", style=_FAINT)
            right.append(f" {counts.get('user', 0)} user ", style=f"{_GOLD_DIM} on #161614")
            right.append(" · ", style=_FAINT)
            right.append(f" {counts.get('project', 0)} project ", style=f"{_GREEN} on #0a1208")
            row.add_row(t, right)
        elif self._mode in _FIELD_EDIT_LABELS:
            hook = self._current_hook()
            right = Text(justify="right", style=_FAINT)
            if hook and _hook_source_path(hook):
                right.append(f" {_fmt_path(_hook_source_path(hook))} ", style=_DIM)  # type: ignore[arg-type]
            row.add_row(t, right)
        elif self._mode == "confirm_delete":
            right = Text(justify="right")
            right.append(" press d again to confirm ", style=f"bold {_ORANGE} on #2a0e06")
            row.add_row(t, right)
        else:
            row.add_row(t, Text(""))

        return row  # type: ignore[return-value]

    # ── List pane ─────────────────────────────────────────────────────────────

    def _build_scope_chips(self) -> Text:
        all_hooks = self._all_hooks()
        counts = self._tier_counts()
        scopes = [
            ("all",     len(all_hooks)),
            ("builtin", counts.get("builtin", 0)),
            ("user",    counts.get("user", 0)),
            ("project", counts.get("project", 0)),
        ]
        t = Text()
        t.append("  ")
        search_overrides = bool(self._query) and self._scope != "all"
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
            t.append("hooks/builtin/", style=f"italic {_FAINT}")
            t.append("  read-only  " + _FILL, style=_FAINT)
        elif tier == "user":
            t.append("  ─── USER ", style=f"bold {_GOLD_DIM}")
            t.append("~/.minion/hooks/", style=f"italic {_FAINT}")
            t.append("  " + _FILL, style=_FAINT)
        else:
            t.append("  ─── PROJECT ", style=f"bold {_GREEN_DIM}")
            t.append(".minion/hooks/", style=f"italic {_FAINT}")
            t.append("  " + _FILL, style=_FAINT)
        return t

    def _build_list(self) -> Table:
        outer = Table.grid(expand=True, padding=0)
        outer.add_column(overflow="crop", no_wrap=True)

        outer.add_row(self._build_scope_chips())
        outer.add_row(Text(""))

        if not self._visible:
            if self._query:
                no_match = Text()
                no_match.append(f'  no hooks match "{self._query}"  ·  ', style=_DIM)
                no_match.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
                no_match.append(" to clear", style=_DIM)
                outer.add_row(no_match)
            else:
                outer.add_row(Text("  no hooks loaded", style=_FAINT))
            return outer

        # Group by tier
        tiers_seen: list[str] = []
        by_tier: dict[str, list[int]] = {}
        for idx, h in enumerate(self._visible):
            src = _hook_source(h)
            if src not in by_tier:
                tiers_seen.append(src)
                by_tier[src] = []
            by_tier[src].append(idx)

        name_w = min(max((len(_hook_name(h)) for h in self._visible), default=8) + 2, 24)

        _ALL_TIERS = ["builtin", "user", "project"]
        tiers_to_render = _ALL_TIERS if (self._scope == "all" and not self._query) else tiers_seen

        for i, tier in enumerate(tiers_to_render):
            if i > 0:
                outer.add_row(Text(""))
            outer.add_row(self._build_tier_header(tier))
            if tier not in by_tier:
                if self._scope == "all" and not self._query and tier in ("user", "project"):
                    hint = Text()
                    hint.append(f"   no {tier} hooks", style=_DIM)
                    hint.append("  ·  press ", style=_FAINT)
                    hint.append(" n ", style=f"bold {_SILVER} on #2a2a2a")
                    hint.append(" to create one", style=_DIM)
                    outer.add_row(hint)
                else:
                    outer.add_row(Text(f"   no {tier} hooks", style=_FAINT))
            else:
                inner = Table.grid(expand=True, padding=0)
                inner.add_column(no_wrap=True, width=3)                        # pointer
                inner.add_column(no_wrap=True, width=name_w)                   # name
                inner.add_column(no_wrap=True, ratio=1, overflow="ellipsis")   # description
                inner.add_column(no_wrap=True, width=18)                       # event badge
                # column header
                inner.add_row(
                    Text(""),
                    Text("name", style=_FAINT),
                    Text("description", style=_FAINT),
                    Text("event", style=_FAINT),
                )
                for idx in by_tier[tier]:
                    self._add_hook_row(inner, idx)
                    if self._mode == "confirm_delete" and idx == self._selected:
                        self._add_confirm_strip_row(inner)
                outer.add_row(inner)

        return outer

    def _add_hook_row(self, inner: Table, idx: int) -> None:
        h = self._visible[idx]
        is_selected = idx == self._selected
        is_delete = self._mode == "confirm_delete" and is_selected

        row_bg = f"on {_TINT_WARN}" if is_delete else (f"on {_TINT_GOLD}" if is_selected else "")

        # Pointer
        ptr = Text(no_wrap=True)
        if is_selected and self._focus_pane == "list":
            ptr_color = _ORANGE if is_delete else _GOLD
            ptr.append("▶ ", style=f"bold {ptr_color}")
            ptr.append(" ")
        else:
            ptr.append("   ")

        # Name color
        if is_delete:
            name_t = Text(_hook_name(h), style=f"strike {_ORANGE}", no_wrap=True)
        elif is_selected:
            tier_bright = {"builtin": _FAINT, "user": _GOLD, "project": _GREEN}
            name_t = Text(_hook_name(h), style=f"bold {tier_bright.get(_hook_source(h), _TEXT)}", no_wrap=True)
        else:
            tier_dim = {"builtin": _DIM, "user": _GOLD_DIM, "project": _GREEN_DIM}
            name_t = Text(_hook_name(h), style=tier_dim.get(_hook_source(h), _DIM), no_wrap=True)

        if is_delete:
            desc_t = Text(_hook_description(h), style=f"strike {_FAINT}")
        elif self._query:
            desc_t = _highlight(_hook_description(h), self._query, _TEXT if is_selected else _DIM)
        else:
            desc_t = Text(_hook_description(h), style=_TEXT if is_selected else _DIM)

        event_t = _event_badge(_hook_event(h)) if not is_delete else Text(_hook_event(h), style=f"strike {_FAINT}")

        inner.add_row(ptr, name_t, desc_t, event_t, style=row_bg)

    def _add_confirm_strip_row(self, inner: Table) -> None:
        ptr = Text("▌  ", style=f"bold {_ORANGE}", no_wrap=True)
        msg = Text()
        msg.append("delete this hook  ·  ", style=_ORANGE)
        msg.append(" d ", style=f"bold {_SILVER} on #2a2a2a")
        msg.append(" confirm  ·  ", style=_DIM)
        msg.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
        msg.append(" cancel", style=_DIM)
        inner.add_row(ptr, msg, Text(""), Text(""), style=f"on {_TINT_ORG}")

    # ── Detail / right-pane builders ──────────────────────────────────────────

    def _build_detail(self) -> Table:
        hook = self._current_hook()
        if hook is None:
            tbl = Table.grid(expand=True, padding=0)
            tbl.add_column()
            tbl.add_row(Text(""))
            tbl.add_row(Text("  select a hook to see details", style=_FAINT))
            return tbl
        return self._build_detail_hook(hook)

    def _build_detail_hook(self, h: Any) -> Table:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        is_builtin = self._is_builtin(h)
        source = _hook_source(h)
        name = _hook_name(h)
        hook_type = _hook_type(h)

        # Header: name + chips
        header = Text()
        header.append(f" {name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {source} ", style=f"bold {_tier_color(source)} on #161614")
        if is_builtin:
            header.append("  ")
            header.append(" read-only ", style=f"{_FAINT} on #161614")
        tbl.add_row(header)
        tbl.add_row(Text(""))

        # ── Group 1: Identity ─────────────────────────────────────────────────
        tbl.add_row(RichRule(style=_RULE))
        tbl.add_row(Text(""))

        # DESCRIPTION
        desc = _hook_description(h)
        tbl.add_row(_detail_section("DESCRIPTION", hint_key="" if is_builtin else "w", hint_value=desc if desc else ""))
        tbl.add_row(Text(f"   {desc}" if desc else "   (no description)", style=_TEXT if desc else _FAINT))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # SOURCE FILE (yaml hooks only)
        if not is_builtin:
            path = _hook_source_path(h)
            tbl.add_row(_detail_section("SOURCE FILE"))
            tbl.add_row(Text(f"   {_fmt_path(path)}" if path else "   (unknown)", style=_FAINT))
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))

        # TYPE (builtin only)
        if is_builtin:
            tbl.add_row(_detail_section("TYPE"))
            type_t = Text()
            type_t.append(f"   {hook_type}", style=f"bold {_BLUE}")
            type_t.append("  —  built-in Python handler", style=_DIM)
            tbl.add_row(type_t)
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))

        # ── Group 2: Trigger ──────────────────────────────────────────────────
        tbl.add_row(RichRule(style=_RULE))
        tbl.add_row(Text(""))

        # EVENT
        event = _hook_event(h)
        tbl.add_row(_detail_section("EVENT", hint_key="" if is_builtin else "e"))
        event_t = Text()
        event_t.append(f"   {event}", style=_TEXT)
        if event == "PreToolUse":
            event_t.append("  · can block", style=f"bold {_ORANGE}")
        tbl.add_row(event_t)
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # TOOL FILTER
        tools = _hook_tools(h)
        tbl.add_row(_detail_section("TOOL FILTER", hint_key="" if is_builtin else "f"))
        tool_display = "   " + "  ·  ".join(tools) if tools else "   (all tools)"
        tbl.add_row(Text(tool_display, style=_TEXT if tools else _FAINT))
        tbl.add_row(Text(""))
        tbl.add_row(Text(""))

        # ── Group 3: Execution (yaml hooks only) ──────────────────────────────
        if not is_builtin:
            tbl.add_row(RichRule(style=_RULE))
            tbl.add_row(Text(""))

            cmd = _hook_command(h)
            tbl.add_row(_detail_section("COMMAND", hint_key="s"))
            cmd_text = Text()
            for part in cmd.splitlines():
                cmd_text.append((part or " ") + "\n", style=_TEXT)
            tbl.add_row(Panel(cmd_text, border_style=_RULE, style="on #0f0f0d", padding=(0, 1), expand=True))
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))

            # TIMEOUT
            timeout = _hook_timeout(h)
            tbl.add_row(_detail_section("TIMEOUT", hint_key="t"))
            tbl.add_row(Text(f"   {timeout}s", style=_TEXT))
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))

            # BLOCKING
            blocking = _hook_blocking(h)
            blocking_str = "auto" if blocking is None else ("true" if blocking else "false")
            bl_val_style = _TEXT if blocking_str == "auto" else f"bold {_ORANGE}"
            bl_t = Text()
            bl_t.append(" BLOCKING", style=f"bold {_DIM}")
            bl_t.append("   ")
            bl_t.append(" b ", style=f"bold {_SILVER} on #2a2a2a")
            bl_t.append(" edit", style=_DIM)
            tbl.add_row(bl_t)
            tbl.add_row(Text(f"   {blocking_str}", style=bl_val_style))
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))

        # ── Exit-code legend ──────────────────────────────────────────────────
        tbl.add_row(RichRule(style=_RULE))
        tbl.add_row(Text(""))
        tbl.add_row(self._build_exit_legend())

        # Builtin note at the very end
        if is_builtin:
            tbl.add_row(Text(""))
            tbl.add_row(Text(""))
            note = Text()
            note.append("   └ built into minion — not editable", style=_DIM)
            note.append("  use a user hook to override or extend", style=_FAINT)
            tbl.add_row(note)
            tbl.add_row(Text(""))

        return tbl

    def _build_exit_legend(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(_detail_section("EXIT CODES"))
        row0 = Text()
        row0.append("  0  ", style=f"bold {_GREEN}")
        row0.append("proceed — stdout JSON may carry a tip", style=_SILVER)
        tbl.add_row(row0)
        row2 = Text()
        row2.append("  2  ", style=f"bold {_ORANGE}")
        row2.append("block the tool call — stderr is the reason", style=_SILVER)
        tbl.add_row(row2)
        rowx = Text()
        rowx.append("  *  ", style=_SILVER)
        rowx.append("non-blocking continue — exit code is logged", style=_DIM)
        tbl.add_row(rowx)
        return tbl

    # ── Create form builders ──────────────────────────────────────────────────

    def _build_create_top(self) -> Text:
        t = Text()
        t.append(" FROM  ", style=_DIM)
        t.append("— blank hook —", style=f"bold {_ORANGE}")
        t.append("\n\n")
        t.append(" ─── name ", style=_DIM)
        t.append("─" * 60, style=_DIM)
        return t

    def _build_create_scroll_content(self) -> Table:
        """Scroll content for create mode: tier → event (summary follows in #hk-edit-controls)."""
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(self._build_tier_radio())
        tbl.add_row(self._build_event_radio())
        return tbl

    def _build_create_what(self) -> Table:
        """'What will be created' summary block shown in #hk-edit-controls after command."""
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(""))
        tbl.add_row(_wiz_section("what will be created"))
        tbl.add_row(Text(""))
        is_valid, _ = self._create_name_ok()

        def _prow(label: str, value: str, value_style: str = _TEXT) -> Text:
            row = Text()
            row.append("  ✓  " if is_valid else "  ·  ",
                       style=f"bold {_GREEN}" if is_valid else _DIM)
            row.append(f"{label:<14}", style=_DIM if is_valid else _FAINT)
            row.append(value, style=value_style if is_valid else _FAINT)
            return row

        # name
        if self._create_name:
            name_sty = (f"bold {_tier_color(self._create_tier)}" if is_valid
                        else f"strike {_FAINT}")
            tbl.add_row(_prow("name", self._create_name, name_sty))
        else:
            ph = Text()
            ph.append("  ·  ", style=_DIM)
            ph.append("name          ", style=_DIM)
            ph.append("<name>", style=_FAINT)
            tbl.add_row(ph)

        # tier + file
        tier_str = f"{self._create_tier}  ·  {self._create_target_path_preview()}"
        tbl.add_row(_prow("tier", tier_str, f"bold {_tier_color(self._create_tier)}"))

        # event
        event_name = _EVENTS[self._create_event_cursor][0]
        tbl.add_row(_prow("event", event_name,
                          _ORANGE if event_name.startswith("Pre") else _SILVER))

        # description
        desc = self._create_desc.strip()
        desc_disp = (desc[:40] + "…") if len(desc) > 40 else desc
        tbl.add_row(_prow("description", desc_disp if desc_disp else "(none — optional)"))

        # command
        cmd = self._edit_cmd.strip()
        if cmd:
            first_line = cmd.splitlines()[0]
            cmd_disp = (first_line[:40] + "…") if len(first_line) > 40 else first_line
            tbl.add_row(_prow("command", cmd_disp))
        else:
            no_cmd = Text()
            no_cmd.append("  ·  ", style=_DIM)
            no_cmd.append("command       ", style=_DIM)
            no_cmd.append("<required>", style=_ORANGE)
            tbl.add_row(no_cmd)

        # defaults (editable in detail view after creation)
        tbl.add_row(_prow("timeout", "30s"))
        tbl.add_row(_prow("blocking", "auto  (PreToolUse blocks, others don't)"))
        tbl.add_row(_prow("tool filter", "(all tools)"))

        tbl.add_row(Text(""))
        note = Text()
        note.append("  ·  ", style=_DIM)
        note.append("opens in detail view", style=_FAINT)
        note.append("  —  ", style=_FAINT)
        note.append("timeout, blocking, tool filter", style=_DIM)
        note.append(" editable there", style=_FAINT)
        tbl.add_row(note)
        tbl.add_row(Text(""))
        return tbl

    def _build_run_hints(self) -> Text:
        """Key hints strip shown at bottom of right pane (create/edit modes)."""
        dot = f" [{_FAINT}]·[/] "
        if self._mode in ("edit_description", "edit_command"):
            parts = [_hint("ctrl+↵", "save"), _hint("↵", "newline"), _hint("esc", "cancel")]
            return Text.from_markup("  " + dot.join(parts))
        # create mode
        ok, _ = self._create_name_ok()
        has_cmd = bool(self._edit_cmd.strip())
        can_create = ok and has_cmd
        parts = [_hint("tab / shift+tab", "next / prev field")]
        parts.append(_hint("↑↓", "switch option"))
        if can_create:
            parts.append(f"[bold {_ORANGE} on #2a2a2a] ctrl+↵ [/] [{_ORANGE}]create & edit[/]")
        else:
            parts.append(f"[{_FAINT}]ctrl+↵  create & edit[/]")
        parts.append(_hint("esc", "cancel"))
        return Text.from_markup("  " + dot.join(parts))

    def _build_create_desc_label(self) -> Text:
        t = Text()
        t.append(" ─── description ", style=_DIM)
        t.append("─" * 60, style=_DIM)
        t.append("\n  optional — describes what this hook does", style=_FAINT)
        return t

    def _build_tier_radio(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(""))
        tbl.add_row(_wiz_section("tier"))
        tbl.add_row(Text(""))
        tier_focused = self._create_focus == "tier"
        for tier, path_preview in [("user", "~/.minion/hooks/"), ("project", ".minion/hooks/")]:
            is_sel = self._create_tier == tier
            bullet = "●" if is_sel else "○"
            ptr = "▸" if (is_sel and tier_focused) else " "
            ptr_style = f"bold {_ORANGE}" if (is_sel and tier_focused) else (_DIM if not is_sel else _SILVER)
            row = Text()
            row.append(f"  {ptr} {bullet} ", style=ptr_style)
            row.append(f"{tier:<8}", style=f"bold {_tier_color(tier)}" if is_sel else _DIM)
            row.append(f"  {path_preview}", style=_FAINT)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if (is_sel and tier_focused) else "")
        return tbl

    def _build_event_radio(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(""))
        tbl.add_row(_wiz_section("event"))
        tbl.add_row(Text(""))
        event_focused = self._create_focus == "event"
        for i, (ev_name, ev_desc) in enumerate(_EVENTS):
            is_sel = i == self._create_event_cursor
            row_style = f"on {_TINT_GOLD}" if (is_sel and event_focused) else ""
            name_t = Text(no_wrap=True)
            if is_sel and event_focused:
                name_t.append("  ▸ ● ", style=f"bold {_ORANGE}")
                name_t.append(ev_name, style=f"bold {_GOLD}")
            elif is_sel:
                name_t.append("    ● ", style=_SILVER)
                name_t.append(ev_name, style=_SILVER)
            else:
                name_t.append("    ○ ", style=_DIM)
                name_t.append(ev_name, style=_DIM)
            tbl.add_row(name_t, style=row_style)
            desc_t = Text(no_wrap=True)
            desc_t.append(f"       {ev_desc}", style=_SILVER if (is_sel and event_focused) else _DIM)
            if ev_name == "PreToolUse":
                desc_t.append("  · can block", style=f"bold {_ORANGE}" if (is_sel and event_focused) else f"bold {_FAINT}")
            tbl.add_row(desc_t, style=row_style)
        return tbl

    def _build_tool_radio(self) -> Table:
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(""))
        focused = self._create_focus == "tool"
        tbl.add_row(_wiz_section("tool filter"))

        # Option 0: all tools
        is_sel = self._create_tool_cursor == 0
        row_style = f"on {_TINT_GOLD}" if (is_sel and focused) else ""
        row_t = Text(no_wrap=True)
        if is_sel and focused:
            row_t.append("  ● ", style=f"bold {_GOLD}")
            row_t.append("(all tools)", style=f"bold {_GOLD}")
        elif is_sel:
            row_t.append("  ● ", style=_SILVER)
            row_t.append("(all tools)", style=_SILVER)
        else:
            row_t.append("  ○ ", style=_DIM)
            row_t.append("(all tools)", style=_DIM)
        tbl.add_row(row_t, style=row_style)
        desc_t = Text("     fires on every tool call", style=_DIM if not (is_sel and focused) else _SILVER, no_wrap=True)
        tbl.add_row(desc_t, style=row_style)

        # Group by category
        cat_order = ["filesystem", "shell", "network", "agents", "tasks", "other"]
        by_cat: dict[str, list[str]] = {}
        for tool in _NATIVE_TOOLS:
            cat = _TOOL_CATEGORIES.get(tool, "other")
            by_cat.setdefault(cat, []).append(tool)

        flat_idx = 1  # 0 is "all tools"
        for cat in cat_order:
            if cat not in by_cat:
                continue
            tbl.add_row(Text(f"  ─── {cat}", style=_FAINT))
            for tool in by_cat[cat]:
                is_sel = flat_idx == self._create_tool_cursor
                row_style = f"on {_TINT_GOLD}" if (is_sel and focused) else ""
                row_t = Text(no_wrap=True)
                if is_sel and focused:
                    row_t.append("  ● ", style=f"bold {_GOLD}")
                    row_t.append(f"{tool:<20}   ", style=f"bold {_GOLD}")
                elif is_sel:
                    row_t.append("  ● ", style=_SILVER)
                    row_t.append(f"{tool:<20}   ", style=_SILVER)
                else:
                    row_t.append("  ○ ", style=_DIM)
                    row_t.append(f"{tool:<20}   ", style=_DIM)
                warn = _TOOL_WARN.get(tool, "")
                if warn:
                    row_t.append(f"{warn}  ", style=f"bold {_ORANGE}")
                desc = _TOOL_DESCRIPTIONS.get(tool, "")
                row_t.append(desc, style=_FAINT)
                tbl.add_row(row_t, style=row_style)
                flat_idx += 1

        return tbl

    # ── Edit / create step 3 builders ─────────────────────────────────────────

    def _build_edit_timeout_pane(self) -> Table:
        hook = self._current_hook()
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        if hook:
            hdr = Text()
            hdr.append(f" {_hook_name(hook)}", style=f"bold {_SILVER}")
            hdr.append("  ")
            hdr.append(f" {_hook_source(hook)} ", style=f"bold {_tier_color(_hook_source(hook))} on #161614")
            tbl.add_row(hdr)
            tbl.add_row(Text(""))
        tbl.add_row(_detail_section("TIMEOUT"))
        tbl.add_row(Text(""))
        stepper = Text()
        stepper.append("   ")
        stepper.append(" ← ", style=f"bold {_SILVER} on #2a2a2a")
        stepper.append(f"  {self._edit_timeout_val}  ", style=f"bold {_ORANGE}")
        stepper.append(" → ", style=f"bold {_SILVER} on #2a2a2a")
        stepper.append("  seconds", style=_DIM)
        tbl.add_row(stepper)
        tbl.add_row(Text(""))
        tbl.add_row(Text("   range 1 – 3600", style=_FAINT))
        return tbl

    def _build_edit_blocking_pane(self) -> Table:
        hook = self._current_hook()
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        if hook:
            hdr = Text()
            hdr.append(f" {_hook_name(hook)}", style=f"bold {_SILVER}")
            hdr.append("  ")
            hdr.append(f" {_hook_source(hook)} ", style=f"bold {_tier_color(_hook_source(hook))} on #161614")
            tbl.add_row(hdr)
            tbl.add_row(Text(""))
        tbl.add_row(_detail_section("BLOCKING"))
        tbl.add_row(Text(""))
        _OPTIONS = [
            ("auto",  "event-default: PreToolUse blocks, others don't"),
            ("true",  "always block the tool call"),
            ("false", "never block the tool call"),
        ]
        for i, (label, desc) in enumerate(_OPTIONS):
            is_sel = i == self._edit_blocking_cursor
            row = Text()
            if is_sel:
                row.append("  ▸  ", style=f"bold {_ORANGE}")
                row.append(f"{label:<8}", style=f"bold {_ORANGE}")
            else:
                row.append("     ", style="")
                row.append(f"{label:<8}", style=_DIM)
            row.append(desc, style=_FAINT)
            tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")
        return tbl

    def _build_edit_event_pane(self) -> Table:
        hook = self._current_hook()
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        if hook:
            hdr = Text()
            hdr.append(f" {_hook_name(hook)}", style=f"bold {_SILVER}")
            hdr.append("  ")
            hdr.append(f" {_hook_source(hook)} ", style=f"bold {_tier_color(_hook_source(hook))} on #161614")
            tbl.add_row(hdr)
            tbl.add_row(Text(""))
        tbl.add_row(_detail_section("EVENT"))
        tbl.add_row(Text(""))
        for i, (ev_name, ev_desc) in enumerate(_EVENTS):
            is_sel = i == self._edit_event_cursor
            row = Text()
            if is_sel:
                row.append("  ▸  ", style=f"bold {_ORANGE}")
                row.append(f"{ev_name:<20}", style=f"bold {_ORANGE}")
            else:
                row.append("     ", style="")
                row.append(f"{ev_name:<20}", style=_DIM)
            row.append(ev_desc, style=_FAINT)
            if ev_name == "PreToolUse":
                row.append("  · can block", style=f"bold {_ORANGE}" if is_sel else f"bold {_FAINT}")
            tbl.add_row(row, style=f"on {_TINT_ORG}" if is_sel else "")
        return tbl

    def _build_edit_tool_pane(self) -> Table:
        hook = self._current_hook()
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()
        if hook:
            hdr = Text()
            hdr.append(f" {_hook_name(hook)}", style=f"bold {_SILVER}")
            hdr.append("  ")
            hdr.append(f" {_hook_source(hook)} ", style=f"bold {_tier_color(_hook_source(hook))} on #161614")
            tbl.add_row(hdr)
            tbl.add_row(Text(""))

        tbl.add_row(Text(" TOOL FILTER", style=f"bold {_DIM}"))
        tbl.add_row(Text(""))

        preamble = Text()
        preamble.append("  Toggle which tools trigger this hook. ", style=_DIM)
        preamble.append("⚠", style=_ORANGE)
        preamble.append(" = broad capability.", style=_DIM)
        tbl.add_row(preamble)
        tbl.add_row(Text("  Empty selection = fire on every tool call.", style=_FAINT))
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
                is_cur = flat_idx == self._edit_tools_cursor
                is_checked = tool in self._edit_tools
                check = "[✓]" if is_checked else "[ ]"
                row = Text(no_wrap=True)
                if is_cur:
                    row.append("  ▸ ", style=f"bold {_ORANGE}")
                else:
                    row.append("    ")
                row.append(f"{check}  ", style=f"bold {_ORANGE}" if is_checked else _DIM)
                row.append(f"{tool:<20}   ", style=_TEXT if is_checked else _DIM)
                warn = _TOOL_WARN.get(tool, "")
                if warn:
                    row.append(f"{warn:<15}", style=f"bold {_ORANGE}")
                else:
                    row.append(" " * 15)
                row.append(_TOOL_DESCRIPTIONS.get(tool, ""), style=_FAINT)
                tbl.add_row(row, style=f"on {_TINT_ORG}" if is_cur else "")
                flat_idx += 1
            tbl.add_row(Text(""))

        tbl.add_row(RichRule(style=_RULE))
        allowed = len(self._edit_tools)
        changes = len(set(self._edit_tools).symmetric_difference(set(self._edit_tools_saved)))
        status = Text()
        if changes:
            status.append("  ● ", style=f"bold {_ORANGE}")
            status.append("unsaved  ", style=_ORANGE)
        else:
            status.append("  ○ ", style=_DIM)
            status.append("saved     ", style=_DIM)
        if allowed:
            status.append(f" {allowed} tool{'s' if allowed != 1 else ''} selected", style=_DIM)
        else:
            status.append(" fires on all tools", style=_DIM)
        if changes:
            added   = [t for t in self._edit_tools if t not in self._edit_tools_saved]
            removed = [t for t in self._edit_tools_saved if t not in self._edit_tools]
            parts = []
            if added:
                parts.append(f"+ {', '.join(added[:3])}")
            if removed:
                parts.append(f"− {', '.join(removed[:3])}")
            if parts:
                status.append(f"  ({', '.join(parts)})", style=_ORANGE)
        tbl.add_row(status)
        return tbl

    def _build_edit_top(self) -> Table:
        if self._mode == "create":
            tbl = Table.grid(expand=True, padding=0)
            tbl.add_column()
            tbl.add_row(Text(""))
            tbl.add_row(_wiz_section("command"))
            tbl.add_row(Text(""))
            return tbl

        hook = self._current_hook()
        name = _hook_name(hook) if hook else ""
        tier = _hook_source(hook) if hook else "user"
        section_label = {
            "edit_command": "command",
            "edit_description": "description",
        }.get(self._mode, "command")

        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column()
        tbl.add_row(Text(""))
        header = Text()
        header.append(f" {name}", style=f"bold {_SILVER}")
        header.append("  ")
        header.append(f" {tier} ", style=f"bold {_tier_color(tier)} on #161614")
        tbl.add_row(header)
        tbl.add_row(Text("  name locked — equals file name", style=_DIM))
        tbl.add_row(Text(""))
        tbl.add_row(_wiz_section(section_label))
        return tbl

    def _build_edit_controls(self) -> Table:
        """Timeout stepper + blocking toggle rendered side-by-side."""
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column(ratio=1)
        tbl.add_column(ratio=1)

        # Timeout stepper
        to_tbl = Table.grid(expand=True, padding=0)
        to_tbl.add_column()
        to_tbl.add_row(Text(""))
        to_focused = self._edit_focus == "timeout"
        to_tbl.add_row(_wiz_section("timeout"))
        stepper_t = Text()
        stepper_t.append("  [−]  ", style=f"{_SILVER} on #2a2a2a" if to_focused else _FAINT)
        stepper_t.append(str(self._edit_timeout_val), style=f"bold {_ORANGE}" if to_focused else _TEXT)
        stepper_t.append("  [+]  ", style=f"{_SILVER} on #2a2a2a" if to_focused else _FAINT)
        stepper_t.append("seconds", style=_SILVER)
        to_tbl.add_row(stepper_t)
        if to_focused:
            to_tbl.add_row(Text("  ←→ adjust", style=_DIM))

        # Blocking toggle
        bl_tbl = Table.grid(expand=True, padding=0)
        bl_tbl.add_column()
        bl_tbl.add_row(Text(""))
        bl_focused = self._edit_focus == "blocking"
        bl_tbl.add_row(_wiz_section("blocking"))
        toggle_t = Text()
        for j, opt in enumerate(_BLOCKING_OPTIONS):
            is_active = j == self._edit_blocking_cursor
            if is_active and bl_focused:
                toggle_t.append(f" {opt} ", style=f"bold {_ORANGE} on {_TINT_WARN}")
            elif is_active:
                toggle_t.append(f" {opt} ", style=f"bold {_TEXT} on #2a2a2a")
            else:
                toggle_t.append(f" {opt} ", style=_DIM)
            if j < len(_BLOCKING_OPTIONS) - 1:
                toggle_t.append(" · ", style=_FAINT)
        bl_tbl.add_row(toggle_t)
        bl_tbl.add_row(Text("  auto: PreToolUse blocks, others don't", style=_FAINT))

        tbl.add_row(to_tbl, bl_tbl)

        # Description section below
        outer = Table.grid(expand=True, padding=0)
        outer.add_column()
        outer.add_row(tbl)
        outer.add_row(Text(""))
        outer.add_row(_wiz_section("description — optional"))
        return outer  # type: ignore[return-value]

    # ── Delete confirm builder ────────────────────────────────────────────────

    def _build_delete_confirm(self) -> Table:
        hook = self._current_hook()
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column()

        if hook is None:
            tbl.add_row(Text("  (no hook selected)", style=_FAINT))
            return tbl

        name = _hook_name(hook)
        source = _hook_source(hook)
        path = _hook_source_path(hook)

        tbl.add_row(Text(""))
        warn = Text()
        warn.append(" ⚠  About to delete ", style=f"bold {_ORANGE}")
        warn.append(name, style=f"bold {_SILVER}")
        warn.append(f"     {source} tier", style=_DIM)
        tbl.add_row(warn)
        tbl.add_row(Text(""))
        tbl.add_row(Text(" The hook file will be permanently removed.", style=_DIM))
        tbl.add_row(Text(" No backup is created. No undo.", style=_DIM))
        tbl.add_row(Text(""))

        tbl.add_row(RichRule(style=_RULE))
        tbl.add_row(Text(" SOURCE FILE", style=f"bold {_DIM}"))
        if path:
            tbl.add_row(Text(f"   {_fmt_path(path)}", style=_FAINT))
        tbl.add_row(Text(""))

        tbl.add_row(RichRule(style=_RULE))
        confirm = Text()
        confirm.append("  ")
        confirm.append(" d ", style=f"bold {_SILVER} on #2a2a2a")
        confirm.append("  confirm delete      ", style=_DIM)
        confirm.append(" esc ", style=f"bold {_SILVER} on #2a2a2a")
        confirm.append("  cancel", style=_DIM)
        tbl.add_row(confirm)

        return tbl

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self) -> Text:
        dot = f" [{_FAINT}]·[/] "
        hook = self._current_hook()
        is_builtin = hook is not None and self._is_builtin(hook)

        if self._mode == "confirm_delete":
            hints = [
                _hint("d", "confirm delete"),
                _hint("esc", "cancel"),
            ]
            suffix = f"  [{_FAINT}]irreversible — no backup[/]"

        elif self._mode == "create":
            ok, _ = self._create_name_ok()
            has_cmd = bool(self._edit_cmd.strip())
            can_create = ok and has_cmd
            hints = [
                _hint("tab / shift+tab", "next / prev field"),
                _hint("↑↓", "switch option"),
                f"[bold {_ORANGE} on #2a2a2a] ctrl+↵ [/] [{_ORANGE}]create & edit[/]"
                    if can_create else f"[{_FAINT}]ctrl+↵  create & edit[/]",
                _hint("esc", "cancel"),
            ]
            suffix = ""

        elif self._mode == "edit_command":
            hints = [_hint("ctrl+↵", "save"), _hint("↵", "newline"), _hint("esc", "cancel")]
            suffix = ""

        elif self._mode == "edit_timeout":
            hints = [_hint("←→", "adjust"), _hint("↑↓", "adjust"), _hint("↵", "save"), _hint("esc", "cancel")]
            suffix = ""

        elif self._mode == "edit_blocking":
            hints = [_hint("↑↓", "nav"), _hint("↵", "save & close"), _hint("esc", "cancel")]
            suffix = ""

        elif self._mode == "edit_event":
            hints = [_hint("↑↓", "nav"), _hint("↵", "save & close"), _hint("esc", "cancel")]
            suffix = ""

        elif self._mode == "edit_tool":
            hints = [_hint("↑↓", "nav"), _hint("space", "toggle"), _hint("a", "toggle category"), _hint("↵", "save & close"), _hint("esc", "cancel")]
            suffix = ""

        elif self._mode == "edit_description":
            hints = [_hint("ctrl+↵", "save"), _hint("↵", "newline"), _hint("esc", "cancel")]
            suffix = ""

        elif self._mode == "detail":
            hints = [_hint("↑↓", "scroll"), _hint("esc", "back")]
            if not is_builtin:
                hints += [_hint("e", "event"), _hint("f", "filter"), _hint("s", "command"), _hint("t", "timeout"), _hint("b", "blocking"), _hint("w", "desc"), _hint("d", "delete")]
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
            ]
            if not is_builtin:
                hints += [_hint("e", "event"), _hint("f", "filter"), _hint("s", "command"), _hint("t", "timeout"), _hint("b", "blocking"), _hint("w", "desc"), _hint("d", "delete")]
            hints.append(_hint("esc", "close"))
            suffix = (
                f"  [{_FAINT}]edit keys hidden — read-only[/]"
                if is_builtin
                else ""
            )

        return Text.from_markup("  " + dot.join(hints) + (suffix or ""))

    # ── Actions (BINDINGS) ────────────────────────────────────────────────────

    def action_nav_up(self) -> None:
        if self._mode in ("edit_command", "edit_description") and isinstance(self.focused, TextArea):
            self.query_one("#hk-cmd-area", TextArea).action_cursor_up()
            return
        if self._mode == "create":
            if self._create_focus == "tier":
                self._create_tier = "user"
                self._refresh()
            elif self._create_focus == "event":
                self._create_event_cursor = max(0, self._create_event_cursor - 1)
                self._refresh()
            return
        if self._mode == "edit_blocking":
            self._edit_blocking_cursor = (self._edit_blocking_cursor - 1) % len(_BLOCKING_OPTIONS)
            self._refresh()
            return
        if self._mode == "edit_event":
            self._edit_event_cursor = (self._edit_event_cursor - 1) % len(_EVENTS)
            self._refresh()
            return
        if self._mode == "edit_tool":
            self._edit_tools_cursor = max(0, self._edit_tools_cursor - 1)
            self._refresh()
            return
        if self._mode == "edit_timeout":
            self._edit_timeout_val = max(1, self._edit_timeout_val - 1)
            self._refresh()
            return
        if self._focus_pane == "detail":
            self.query_one("#hk-preview-scroll", VerticalScroll).scroll_relative(y=-3)
            return
        if self._mode == "confirm_delete":
            return
        if self._visible:
            self._selected = max(0, self._selected - 1)
            self._refresh()

    def action_nav_down(self) -> None:
        if self._mode in ("edit_command", "edit_description") and isinstance(self.focused, TextArea):
            self.query_one("#hk-cmd-area", TextArea).action_cursor_down()
            return
        if self._mode == "create":
            if self._create_focus == "tier":
                self._create_tier = "project"
                self._refresh()
            elif self._create_focus == "event":
                self._create_event_cursor = min(len(_EVENTS) - 1, self._create_event_cursor + 1)
                self._refresh()
            return
        if self._mode == "edit_blocking":
            self._edit_blocking_cursor = (self._edit_blocking_cursor + 1) % len(_BLOCKING_OPTIONS)
            self._refresh()
            return
        if self._mode == "edit_event":
            self._edit_event_cursor = (self._edit_event_cursor + 1) % len(_EVENTS)
            self._refresh()
            return
        if self._mode == "edit_tool":
            self._edit_tools_cursor = min(len(_NATIVE_TOOLS) - 1, self._edit_tools_cursor + 1)
            self._refresh()
            return
        if self._mode == "edit_timeout":
            self._edit_timeout_val = min(3600, self._edit_timeout_val + 1)
            self._refresh()
            return
        if self._focus_pane == "detail":
            self.query_one("#hk-preview-scroll", VerticalScroll).scroll_relative(y=3)
            return
        if self._mode == "confirm_delete":
            return
        if self._visible:
            self._selected = min(len(self._visible) - 1, self._selected + 1)
            self._refresh()

    def action_esc_action(self) -> None:
        edit_modes = (
            "confirm_delete", "create", "detail",
            "edit_command", "edit_timeout", "edit_blocking", "edit_description", "edit_event", "edit_tool",
        )
        if self._mode in edit_modes:
            self._mode = "browse"
            self._focus_pane = "list"
            self._reset_create_state()
            self.query_one("#hk-panel", Vertical).focus()
            self._refresh()
        elif self._query:
            self.query_one("#hk-search", ModalSearchBar).clear()
            self._query = ""
            self._mode = "browse"
            self._rebuild_visible()
            self.query_one("#hk-panel", Vertical).focus()
            self._refresh()
        else:
            self.dismiss(self._registry_changed)

    def action_confirm(self) -> None:
        if self._mode == "create":
            if self._create_focus == "name":
                ok, _ = self._create_name_ok()
                if ok:
                    self._create_focus = "desc"
                    self.query_one("#hk-create-desc-input", Input).focus()
                    self._refresh()
            elif self._create_focus == "desc":
                self._create_focus = "tier"
                self.query_one("#hk-panel", Vertical).focus()
                self._refresh()
            elif self._create_focus == "tier":
                self._create_focus = "event"
                self._refresh()
            elif self._create_focus == "event":
                self._create_focus = "command"
                self.query_one("#hk-cmd-area", TextArea).focus()
                self._refresh()
            elif self._create_focus == "command":
                focused = self.focused
                if isinstance(focused, TextArea):
                    focused.insert("\n")
            return

        if self._mode == "edit_command":
            focused = self.focused
            if isinstance(focused, TextArea):
                focused.insert("\n")
            return

        if self._mode == "edit_timeout":
            self._do_save_timeout()
            return

        if self._mode == "edit_blocking":
            self._do_save_blocking()
            return

        if self._mode == "edit_event":
            self._do_save_event()
            return

        if self._mode == "edit_tool":
            self._do_save_tool()
            return

        if self._mode == "edit_description":
            if isinstance(self.focused, TextArea):
                self.query_one("#hk-cmd-area", TextArea).insert("\n")
            return

        if self._mode == "browse" and self._visible:
            self._mode = "detail"
            self._focus_pane = "detail"
            self._refresh()

    def action_confirm_primary(self) -> None:
        """ctrl+enter / ctrl+j — save edit or complete create."""
        if self._mode == "create":
            ok, _ = self._create_name_ok()
            if not ok:
                return
            cmd = self.query_one("#hk-cmd-area", TextArea).text.strip()
            if not cmd:
                return
            self._do_create(cmd)
        elif self._mode == "edit_command":
            cmd = self.query_one("#hk-cmd-area", TextArea).text.strip()
            if not cmd:
                return
            self._do_save_command(cmd)
        elif self._mode == "edit_description":
            self._edit_desc = self.query_one("#hk-cmd-area", TextArea).text.strip()
            self._do_save_description()

    def action_cycle_scope(self) -> None:
        if self._mode == "create":
            order = ["name", "desc", "tier", "event", "command"]
            curr = self._create_focus if self._create_focus in order else "name"
            self._create_focus = order[(order.index(curr) + 1) % len(order)]
            if self._create_focus == "name":
                self.query_one("#hk-name-input", Input).focus()
            elif self._create_focus == "desc":
                self.query_one("#hk-create-desc-input", Input).focus()
            elif self._create_focus == "command":
                self.query_one("#hk-cmd-area", TextArea).focus()
            else:
                self.query_one("#hk-panel", Vertical).focus()
            self._refresh()
            return

        if self._mode not in ("browse", "search", "detail"):
            return
        scopes = ["all", "builtin", "user", "project"]
        idx = scopes.index(self._scope) if self._scope in scopes else 0
        self._scope = scopes[(idx + 1) % len(scopes)]
        self._rebuild_visible()
        self._refresh()

    def action_cycle_scope_back(self) -> None:
        if self._mode == "create":
            order = ["name", "desc", "tier", "event", "command"]
            curr = self._create_focus if self._create_focus in order else "name"
            self._create_focus = order[(order.index(curr) - 1) % len(order)]
            if self._create_focus == "name":
                self.query_one("#hk-name-input", Input).focus()
            elif self._create_focus == "desc":
                self.query_one("#hk-create-desc-input", Input).focus()
            elif self._create_focus == "command":
                self.query_one("#hk-cmd-area", TextArea).focus()
            else:
                self.query_one("#hk-panel", Vertical).focus()
            self._refresh()
            return

    # ── Key dispatch ──────────────────────────────────────────────────────────

    def on_key(self, event: Key) -> None:
        key = event.key

        # Delegate escape to action handler when in Input so esc works from any input
        try:
            focused = self.focused
        except Exception:
            focused = None

        if isinstance(focused, Input) and focused.id in ("hk-name-input", "hk-create-desc-input", "hk-desc-input"):
            if key == "escape":
                self.action_esc_action()
                event.stop()
            elif key == "tab":
                self.action_cycle_scope()
                event.stop()
            elif key == "shift+tab":
                self.action_cycle_scope_back()
                event.stop()
            return

        if isinstance(focused, TextArea) and focused.id == "hk-cmd-area":
            if key == "tab":
                self.action_cycle_scope()
                event.stop()
            elif key == "shift+tab":
                self.action_cycle_scope_back()
                event.stop()
            return

        mode = self._mode

        if mode == "confirm_delete":
            if key == "d":
                self._do_delete()
                event.stop()
            return

        if mode == "create":
            if self._create_focus == "tier" and key in ("t", "left", "right"):
                self._create_tier = "project" if self._create_tier == "user" else "user"
                self._refresh()
                event.stop()
            if key in ("tab", "shift+tab"):
                event.stop()
            return

        if mode == "edit_timeout":
            if key == "left":
                self._edit_timeout_val = max(1, self._edit_timeout_val - 1)
                self._refresh()
                event.stop()
            elif key == "right":
                self._edit_timeout_val = min(3600, self._edit_timeout_val + 1)
                self._refresh()
                event.stop()
            return

        if mode == "edit_tool":
            if key == "space":
                self._toggle_tool_at_cursor()
                event.stop()
            elif key == "a":
                self._toggle_category_at_cursor()
                event.stop()
            return

        if mode in ("edit_command", "edit_blocking", "edit_description", "edit_event"):
            return

        # Browse / search / detail
        hook = self._current_hook()
        is_builtin = hook is not None and self._is_builtin(hook)

        if key == "n":
            self._start_create()
            event.stop()
        elif key == "e" and not is_builtin and hook is not None:
            self._start_edit_event()
            event.stop()
        elif key == "f" and not is_builtin and hook is not None:
            self._start_edit_tool()
            event.stop()
        elif key == "s" and not is_builtin and hook is not None:
            self._start_edit_command()
            event.stop()
        elif key == "t" and not is_builtin and hook is not None:
            self._start_edit_timeout()
            event.stop()
        elif key == "b" and not is_builtin and hook is not None:
            self._start_edit_blocking()
            event.stop()
        elif key == "w" and not is_builtin and hook is not None:
            self._start_edit_description()
            event.stop()
        elif key == "d" and not is_builtin and hook is not None:
            self._start_delete()
            event.stop()
        elif key == "slash":
            self.query_one("#hk-search", ModalSearchBar).focus_input()
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

    # ── Input event handlers ──────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "hk-name-input":
            self._create_name = event.value
            self._refresh()
        elif event.input.id == "hk-create-desc-input":
            self._create_desc = event.value
        elif event.input.id == "hk-desc-input":
            self._edit_desc = event.value
        else:
            # ModalSearchBar inner Input has no id — any other Input.Changed is the search bar
            self._query = event.value.strip().lower()
            self._mode = "search" if self._query else "browse"
            self._rebuild_visible()
            self._refresh()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "hk-name-input":
            ok, _ = self._create_name_ok()
            if ok:
                self._create_focus = "desc"
                self.query_one("#hk-create-desc-input", Input).focus()
                self._refresh()
        elif event.input.id == "hk-create-desc-input":
            self._create_focus = "tier"
            self.query_one("#hk-panel", Vertical).focus()
            self._refresh()
        else:
            # search bar Enter — dismiss keyboard focus back to panel
            self.query_one("#hk-panel", Vertical).focus()
            self._mode = "browse" if not self._query else "search"
            self._refresh()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "hk-cmd-area":
            self._edit_cmd = event.text_area.text
            lines = self._edit_cmd.count("\n") + 1 if self._edit_cmd else 0
            chars = len(self._edit_cmd)
            try:
                self.query_one("#hk-cmd-meta", Static).update(
                    Text(f"  {chars} chars · {lines} line{'s' if lines != 1 else ''}", style=_DIM)
                )
            except Exception:
                pass
            # Immediately refresh footer, hints, and summary so ctrl+↵ lights up
            if self._mode == "create":
                try:
                    self.query_one("#hk-footer", Static).update(self._build_footer())
                    self.query_one("#hk-run-hints", Static).update(self._build_run_hints())
                    self.query_one("#hk-edit-controls", Static).update(self._build_create_what())
                except Exception:
                    pass

    # ── Mode transitions ──────────────────────────────────────────────────────

    def _start_create(self) -> None:
        self._reset_create_state()
        self._mode = "create"
        self._focus_pane = "list"
        self.query_one("#hk-create-desc-input", Input).value = ""
        self._refresh()
        self.call_after_refresh(lambda: self.query_one("#hk-name-input", Input).focus())

    def _start_edit_command(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        self._edit_cmd = _hook_command(hook)
        self._mode = "edit_command"
        self._focus_pane = "detail"
        self._refresh()

    def _start_edit_timeout(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        self._edit_timeout_val = _hook_timeout(hook)
        self._mode = "edit_timeout"
        self._focus_pane = "detail"
        self._refresh()

    def _start_edit_blocking(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        self._edit_blocking_cursor = _cursor_from_blocking(_hook_blocking(hook))
        self._mode = "edit_blocking"
        self._focus_pane = "detail"
        self._refresh()

    def _start_edit_tool(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        self._edit_tools = list(_hook_tools(hook))
        self._edit_tools_saved = list(self._edit_tools)
        self._edit_tools_cursor = 0
        self._mode = "edit_tool"
        self._focus_pane = "detail"
        self._refresh()

    def _start_edit_event(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        current_event = _hook_event(hook)
        self._edit_event_cursor = next(
            (i for i, (name, _) in enumerate(_EVENTS) if name == current_event), 0
        )
        self._mode = "edit_event"
        self._focus_pane = "detail"
        self._refresh()

    def _build_edit_pane_label(self, label: str) -> Text:
        """Combined name+tier identity card and section label for edit panes."""
        hook = self._current_hook()
        t = Text()
        name = _hook_name(hook) if hook else "—"
        tier = _hook_source(hook) if hook else "user"
        t.append(f" {name}", style=f"bold {_SILVER}")
        t.append("  ")
        t.append(f" {tier} ", style=f"bold {_tier_color(tier)} on #161614")
        t.append("\n\n")
        t.append(f" {label}", style=f"bold {_DIM}")
        t.append("\n")
        return t

    def _start_edit_description(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        self._edit_desc = _hook_description(hook)
        self._mode = "edit_description"
        self._focus_pane = "detail"
        self._refresh()

    def _start_delete(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        self._mode = "confirm_delete"
        self.query_one("#hk-panel", Vertical).focus()
        self._refresh()

    def _reset_create_state(self) -> None:
        self._create_name = ""
        self._create_desc = ""
        self._create_tier = "user"
        self._create_event_cursor = 0
        self._create_tool_cursor = 0
        self._create_focus = "name"
        self._edit_cmd = ""
        self._edit_timeout_val = 30
        self._edit_blocking_cursor = 0
        self._edit_desc = ""
        self._edit_focus = "command"

    # ── Execution actions ─────────────────────────────────────────────────────

    def _do_create(self, cmd: str) -> None:
        from ...hooks.persist import create_hook_yaml
        event_name = _EVENTS[self._create_event_cursor][0]
        path = self._create_target_path()
        try:
            create_hook_yaml(
                path,
                name=self._create_name,
                event=event_name,
                command=cmd,
                tools=None,
                description=self._create_desc,
                timeout=30,
                blocking=None,
            )
        except OSError:
            return  # silently skip on write error
        self._registry_changed = True
        self._reload_registry()
        # Select the newly created hook
        new_name = self._create_name
        self._reset_create_state()
        self._mode = "browse"
        self._focus_pane = "list"
        self._scope = "all"
        self._rebuild_visible()
        for i, h in enumerate(self._visible):
            if _hook_name(h) == new_name:
                self._selected = i
                break
        self._refresh()

    def _do_save_command(self, cmd: str) -> None:
        from ...hooks.persist import update_hook_yaml
        hook = self._current_hook()
        if hook is None:
            return
        path = _hook_source_path(hook)
        if path is None:
            return
        try:
            update_hook_yaml(path, updates={"command": cmd})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._focus_pane = "list"
        self._refresh()

    def _do_save_timeout(self) -> None:
        from ...hooks.persist import update_hook_yaml
        hook = self._current_hook()
        if hook is None:
            return
        path = _hook_source_path(hook)
        if path is None:
            return
        try:
            update_hook_yaml(path, updates={"timeout": self._edit_timeout_val})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._focus_pane = "list"
        self._refresh()

    def _do_save_tool(self) -> None:
        from ...hooks.persist import update_hook_yaml
        hook = self._current_hook()
        if hook is None:
            return
        path = _hook_source_path(hook)
        if path is None:
            return
        tools: Optional[list[str]] = self._edit_tools if self._edit_tools else None
        try:
            # Write new `tools` list and remove legacy `tool` key
            update_hook_yaml(path, updates={"tools": tools, "tool": None})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._focus_pane = "list"
        self._refresh()

    def _toggle_tool_at_cursor(self) -> None:
        cat_order = ["filesystem", "shell", "network", "agents", "tasks", "other"]
        by_cat: dict[str, list[str]] = {}
        for t in _NATIVE_TOOLS:
            by_cat.setdefault(_TOOL_CATEGORIES.get(t, "other"), []).append(t)
        flat_idx = 0
        for cat in cat_order:
            for tool in by_cat.get(cat, []):
                if flat_idx == self._edit_tools_cursor:
                    if tool in self._edit_tools:
                        self._edit_tools.remove(tool)
                    else:
                        self._edit_tools.append(tool)
                    self._refresh()
                    return
                flat_idx += 1

    def _toggle_category_at_cursor(self) -> None:
        cat_order = ["filesystem", "shell", "network", "agents", "tasks", "other"]
        by_cat: dict[str, list[str]] = {}
        for t in _NATIVE_TOOLS:
            by_cat.setdefault(_TOOL_CATEGORIES.get(t, "other"), []).append(t)
        flat_idx = 0
        for cat in cat_order:
            tools_in_cat = by_cat.get(cat, [])
            for tool in tools_in_cat:
                if flat_idx == self._edit_tools_cursor:
                    if all(t in self._edit_tools for t in tools_in_cat):
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

    def _do_save_event(self) -> None:
        from ...hooks.persist import update_hook_yaml
        hook = self._current_hook()
        if hook is None:
            return
        path = _hook_source_path(hook)
        if path is None:
            return
        event_name = _EVENTS[self._edit_event_cursor][0]
        try:
            update_hook_yaml(path, updates={"event": event_name})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._focus_pane = "list"
        self._refresh()

    def _do_save_blocking(self) -> None:
        from ...hooks.persist import update_hook_yaml
        hook = self._current_hook()
        if hook is None:
            return
        path = _hook_source_path(hook)
        if path is None:
            return
        blocking = _blocking_from_cursor(self._edit_blocking_cursor)
        try:
            update_hook_yaml(path, updates={"blocking": blocking})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._focus_pane = "list"
        self._refresh()

    def _do_save_description(self) -> None:
        from ...hooks.persist import update_hook_yaml
        hook = self._current_hook()
        if hook is None:
            return
        path = _hook_source_path(hook)
        if path is None:
            return
        desc = self._edit_desc.strip()
        try:
            update_hook_yaml(path, updates={"description": desc or None})
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._mode = "browse"
        self._focus_pane = "list"
        self._refresh()

    def _do_delete(self) -> None:
        hook = self._current_hook()
        if hook is None or self._is_builtin(hook):
            return
        path = _hook_source_path(hook)
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            return
        self._registry_changed = True
        self._reload_registry()
        self._selected = max(0, self._selected - 1)
        self._mode = "browse"
        self._focus_pane = "list"
        self.query_one("#hk-panel", Vertical).focus()
        self._refresh()
