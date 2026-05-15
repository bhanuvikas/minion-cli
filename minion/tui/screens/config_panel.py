"""ConfigPanelScreen — interactive /config settings modal.

Flat list of settings with inline compact editors.
All changes are saved to the project config file immediately and
reflected in the running session where applicable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from ...config.file import MinionConfig
from ..theme import DIM, GOLD, SILVER

# ── Color constants ────────────────────────────────────────────────────────────

_GREEN  = "#6fd080"
_MUTED  = "#5a5a5a"
_BRIGHT = "#e6e6e6"
_DIM_BG = "#1a1a1a"

# ── SettingDef catalog ─────────────────────────────────────────────────────────


@dataclass
class SettingDef:
    full_key: str
    section: str
    key: str
    desc: str
    kind: str           # "bool" | "enum" | "int" | "float"
    options: list[str] = field(default_factory=list)
    min_val: float = 0.0
    max_val: float = 100.0
    step: float = 1.0
    session_attr: str = ""   # ReplState attr to update on dismiss ("" = file-only)
    option_colors: dict[str, str] = field(default_factory=dict)  # enum value → color


ALL_SETTINGS: list[SettingDef] = [
    # Agent
    SettingDef("agent.reflect_depth",      "agent", "reflect_depth",      "self-refine iterations (0 = off)",            "int",  min_val=0,   max_val=5,   step=1,    session_attr="reflect_depth"),
    SettingDef("agent.verbose",            "agent", "verbose",            "verbose critique output during reflection",    "bool",                                         session_attr="verbose"),
    SettingDef("agent.debug",              "agent", "debug",              "print system prompt and debug info per turn",  "bool",                                         session_attr="debug"),
    SettingDef("agent.agents_enabled",     "agent", "agents_enabled",     "allow the model to spawn sub-agents",          "bool",                                         session_attr="agents_enabled"),
    SettingDef("agent.max_subagent_depth", "agent", "max_subagent_depth", "max nesting depth for spawned agents",         "int",  min_val=1,   max_val=5,   step=1),
    SettingDef("agent.approval_mode",      "agent", "approval_mode",      "tool confirmation policy",                     "enum", options=["off", "edits", "yolo"],        session_attr="approval_mode", option_colors={"off": _BRIGHT, "edits": GOLD, "yolo": "#e26d55"}),
    SettingDef("agent.markdown_enabled",   "agent", "markdown_enabled",   "render LLM responses as Rich markdown",        "bool",                                         session_attr="markdown_enabled"),
    # Memory
    SettingDef("memory.enabled",                 "memory", "enabled",                 "persist and retrieve memories across sessions",  "bool",                                    session_attr="memory_enabled"),
    SettingDef("memory.top_k",                   "memory", "top_k",                   "max memories injected per turn",                 "int",  min_val=1,  max_val=20,  step=1),
    SettingDef("memory.similarity_threshold",    "memory", "similarity_threshold",    "min cosine similarity to include a memory",      "float",min_val=0.0,max_val=1.0, step=0.05),
    SettingDef("memory.consolidation_threshold", "memory", "consolidation_threshold", "similarity above which two memories are merged", "float",min_val=0.0,max_val=1.0, step=0.05),
    SettingDef("memory.extraction_trigger",      "memory", "extraction_trigger",      "when to extract memories from conversation",     "enum", options=["substantial", "every_5", "manual", "always"]),
    SettingDef("memory.extraction_min_words",    "memory", "extraction_min_words",    "min word count before attempting extraction",    "int",  min_val=0,  max_val=200, step=10),
    # Context
    SettingDef("context.auto_compact",    "context", "auto_compact",      "auto-compact context on token rate limit",   "bool"),
    # Tracing
    SettingDef("tracing.enabled",         "tracing", "enabled",           "write session trace events to JSONL",        "bool"),
    # Hooks
    SettingDef("hooks.enabled",           "hooks",   "enabled",           "master toggle for all hook handlers",        "bool"),
    SettingDef("hooks.builtin_minion_md", "hooks",   "builtin_minion_md", "suggest MINION.md update after file edits",  "bool"),
]

# ── Value resolution ───────────────────────────────────────────────────────────

_SECTION_ATTR: dict[str, str] = {
    "agent":   "agent",
    "memory":  "memory",
    "context": "context",
    "tracing": "tracing",
    "hooks":   "hooks_config",
}


def _get_value(setting: SettingDef, cfg: MinionConfig) -> Any:
    obj = getattr(cfg, _SECTION_ATTR.get(setting.section, setting.section), None)
    return getattr(obj, setting.key, None) if obj is not None else None


# ── Column widths (computed from catalog at import time) ───────────────────────

def _val_col_width(s: SettingDef) -> int:
    if s.kind == "bool":
        return 7   # "[ off ]"
    if s.kind == "enum":
        return max(len(o) + 4 for o in s.options)   # "[ substantial ]" = 15
    if s.kind == "int":
        return len(f"[-] {int(s.max_val)} [+]")
    if s.kind == "float":
        return len(f"[-] {s.max_val:.2f} [+]")
    return 10


_KEY_W  = max(len(s.full_key) for s in ALL_SETTINGS)    # 30
_VAL_W  = max(_val_col_width(s) for s in ALL_SETTINGS)  # 15
_DESC_W = max(len(s.desc)      for s in ALL_SETTINGS)

# dot(2) + key + key-margin(2) + desc + gap(2) + val + row_pad(4) + border(2) + scrollbar(1)
_PANEL_W = 2 + _KEY_W + 2 + 2 + _DESC_W + 2 + _VAL_W + 4 + 2 + 1


# ── Panel CSS ──────────────────────────────────────────────────────────────────

_PANEL_CSS = f"""
ConfigPanelScreen {{
    align: center middle;
    background: #000000 40%;
}}
#config-panel {{
    width: {_PANEL_W};
    height: 90%;
    background: #0d0d0d;
    border: round #3a3a3a;
}}
#config-title {{
    height: auto;
    padding: 0 2;
    background: #0d0d0d;
    border-bottom: solid #2e2e2e;
}}
#config-search-wrap {{
    height: auto;
    padding: 0 2 0 2;
    border-bottom: solid #2e2e2e;
    background: #0d0d0d;
}}
#config-search {{
    margin: 0;
    background: {_DIM_BG};
    border: solid #3a3a3a;
    color: {_BRIGHT};
    padding: 0 1;
    height: 3;
}}
#config-search:focus {{
    border: solid {GOLD};
}}
#settings-list {{
    height: 1fr;
    padding: 0;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {DIM};
}}
SettingRow {{
    height: 3;
    border: solid #1c1c1c;
    padding: 0 1;
    layout: horizontal;
    align: left middle;
}}
SettingRow:focus {{
    border: solid {GOLD};
    background: #1a1200;
}}
.row-dot {{
    width: 2;
    height: 1;
}}
.row-key {{
    width: {_KEY_W};
    height: 1;
    margin-right: 2;
}}
.row-desc {{
    width: 1fr;
    height: 1;
    color: {_MUTED};
}}
.row-editor {{
    width: {_VAL_W};
    height: 1;
    text-align: right;
}}
#config-foot {{
    height: 2;
    padding: 0 2;
    background: #0d0d0d;
    color: {DIM};
    border-top: solid #2e2e2e;
}}
"""

# ── Compact editor markup ──────────────────────────────────────────────────────


def _compact_markup(setting: SettingDef, val: Any) -> str:
    kind = setting.kind
    if kind == "bool":
        return f"[bold {_GREEN}]\\[ on ][/]" if val else f"[{_MUTED}]\\[ off ][/]"
    if kind == "enum":
        color = setting.option_colors.get(str(val), _BRIGHT)
        return f"[bold {color}]\\[ {val} ][/]"
    if kind == "int":
        return f"[{_MUTED}]\\[-][/] [{_BRIGHT}]{int(val)}[/] [{_MUTED}]\\[+][/]"
    if kind == "float":
        return f"[{_MUTED}]\\[-][/] [{_BRIGHT}]{float(val):.2f}[/] [{_MUTED}]\\[+][/]"
    return f"[{_BRIGHT}]{val}[/]"


# ── SettingRow widget ──────────────────────────────────────────────────────────


class SettingRow(Widget):
    """One focusable row in the settings flat list."""

    can_focus = True

    class RowFocused(Message):
        def __init__(self, full_key: str) -> None:
            super().__init__()
            self.full_key = full_key

    def __init__(self, setting: SettingDef, value: Any, changed: bool = False) -> None:
        super().__init__(id=f"row-{setting.full_key.replace('.', '-')}")
        self._setting = setting
        self._value   = value
        self._changed = changed

    def compose(self) -> ComposeResult:
        ns, _, name = self._setting.full_key.partition(".")
        dot_markup = f"[bold {GOLD}]●[/]" if self._changed else " "
        key_markup = f"[{_MUTED}]{ns}.[/][{_BRIGHT}]{name}[/]"
        yield Static(dot_markup,                                  classes="row-dot")
        yield Static(key_markup,                                  classes="row-key")
        yield Static(self._setting.desc,                          classes="row-desc")
        yield Static(_compact_markup(self._setting, self._value), classes="row-editor")

    def on_focus(self) -> None:
        self.post_message(self.RowFocused(self._setting.full_key))

    def update_value(self, value: Any, changed: bool) -> None:
        self._value   = value
        self._changed = changed
        try:
            self.query_one(".row-dot",    Static).update(f"[bold {GOLD}]●[/]" if changed else " ")
            self.query_one(".row-editor", Static).update(_compact_markup(self._setting, value))
        except Exception:
            pass


# ── Footer markup ──────────────────────────────────────────────────────────────

_FooterState = Literal["browsing", "searching"]

_FOOTER_HINTS: dict[str, list[tuple[list[str], str]]] = {
    "browsing": [
        (["↑", "↓"], "navigate"),
        (["←", "→"], "adjust"),
        (["Space"], "toggle"),
        (["/"], "search"),
        (["Esc"], "close"),
    ],
    "searching": [
        (["type"], "filter"),
        (["↑", "↓"], "navigate"),
        (["Esc"], "clear"),
    ],
}


def _build_footer(state: _FooterState) -> str:
    parts: list[str] = []
    for keys, label in _FOOTER_HINTS[state]:
        key_spans = " ".join(f"[bold {SILVER}]{k}[/]" for k in keys)
        parts.append(f"{key_spans} [{DIM}]{label}[/]")
    return " " + f"  [{DIM}]·[/]  ".join(parts)


# ── ConfigPanelScreen ──────────────────────────────────────────────────────────


class ConfigPanelScreen(ModalScreen):  # type: ignore[type-arg]
    """Interactive /config settings panel — flat list with inline editors."""

    CSS = _PANEL_CSS

    BINDINGS = [
        Binding("escape", "dismiss_or_clear", show=False, priority=True),
        Binding("up",     "nav_up",           show=False, priority=True),
        Binding("down",   "nav_down",         show=False, priority=True),
        Binding("left",   "nav_left",         show=False),
        Binding("right",  "nav_right",        show=False),
        Binding("enter",  "confirm",          show=False),
        Binding("space",  "toggle_bool",      show=False),
    ]

    def __init__(self, *, cfg: MinionConfig, cwd: Path) -> None:
        super().__init__()
        self._cfg  = cfg
        self._cwd  = cwd

        self._values: dict[str, Any] = {
            s.full_key: _get_value(s, cfg) for s in ALL_SETTINGS
        }
        self._changed_keys:  set[str]       = set()
        self._state_changes: dict[str, Any] = {}
        self._focused_key:   str            = ALL_SETTINGS[0].full_key if ALL_SETTINGS else ""
        self._searching:     bool           = False

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        project_path = self._cwd / ".minion" / "config.toml"
        path_label = (
            str(project_path) if project_path.exists()
            else "project config will be created on first change"
        )
        title = f"[{DIM}]┌─[/] [bold]/config[/] [{DIM}]— settings[/]   [{_MUTED}]{path_label}[/]"
        with Vertical(id="config-panel"):
            yield Static(title, id="config-title")
            with Vertical(id="config-search-wrap"):
                yield Input(placeholder="search…", id="config-search")
            with VerticalScroll(id="settings-list"):
                for s in ALL_SETTINGS:
                    yield SettingRow(s, self._values[s.full_key])
            yield Static(_build_footer("browsing"), id="config-foot")

    async def on_mount(self) -> None:
        self.query_one("#settings-list", VerticalScroll).can_focus = False
        self._enter_search()

    # ── Focus tracking ────────────────────────────────────────────────────────

    def on_setting_row_row_focused(self, event: SettingRow.RowFocused) -> None:
        self._focused_key = event.full_key

    def _focus_key(self, key: str) -> None:
        self._focused_key = key
        self._searching = False
        try:
            self.query_one(f"#row-{key.replace('.', '-')}", SettingRow).focus(scroll_visible=True)
        except Exception:
            pass
        self._refresh_footer()

    def _focused_setting(self) -> SettingDef | None:
        return next((s for s in ALL_SETTINGS if s.full_key == self._focused_key), None)

    def _visible_keys(self) -> list[str]:
        result: list[str] = []
        for s in ALL_SETTINGS:
            try:
                row = self.query_one(f"#row-{s.full_key.replace('.', '-')}", SettingRow)
                if row.display:
                    result.append(s.full_key)
            except Exception:
                pass
        return result

    # ── Navigation ────────────────────────────────────────────────────────────

    def action_nav_up(self) -> None:
        keys = self._visible_keys()
        if not keys:
            return
        if self._searching:
            self._focus_key(keys[-1])
            return
        idx = keys.index(self._focused_key) if self._focused_key in keys else 0
        self._focus_key(keys[(idx - 1) % len(keys)])

    def action_nav_down(self) -> None:
        keys = self._visible_keys()
        if not keys:
            return
        if self._searching:
            self._focus_key(keys[0])
            return
        idx = keys.index(self._focused_key) if self._focused_key in keys else 0
        self._focus_key(keys[(idx + 1) % len(keys)])

    def action_nav_left(self) -> None:
        setting = self._focused_setting()
        if setting:
            self._adjust(setting, -1)

    def action_nav_right(self) -> None:
        setting = self._focused_setting()
        if setting:
            self._adjust(setting, +1)

    def action_toggle_bool(self) -> None:
        setting = self._focused_setting()
        if setting and setting.kind == "bool":
            self._adjust(setting, 0)

    def action_confirm(self) -> None:
        if self._searching:
            self._exit_search()

    def _adjust(self, setting: SettingDef, direction: int) -> None:
        kind    = setting.kind
        current = self._values[setting.full_key]
        if kind == "bool":
            new_val: Any = not bool(current)
        elif kind == "enum":
            opts = setting.options
            idx  = opts.index(current) if current in opts else 0
            new_val = opts[(idx + (1 if direction >= 0 else -1)) % len(opts)]
        elif kind == "int":
            delta   = int(setting.step) * direction
            new_val = max(int(setting.min_val), min(int(setting.max_val), int(current) + delta))
        elif kind == "float":
            raw     = float(current) + setting.step * direction
            new_val = round(max(setting.min_val, min(setting.max_val, raw)), 4)
        else:
            return
        self._apply_change(setting, new_val)

    # ── Apply a change ────────────────────────────────────────────────────────

    def _apply_change(self, setting: SettingDef, new_val: Any) -> None:
        if new_val == self._values[setting.full_key]:
            return
        self._values[setting.full_key] = new_val
        self._changed_keys.add(setting.full_key)

        # Persist to project config immediately
        try:
            from ...config.file import set_project_config_value
            set_project_config_value(self._cwd, setting.section, setting.key, new_val)
        except Exception:
            pass

        # Record for immediate ReplState update on dismiss
        if setting.session_attr:
            self._state_changes[setting.session_attr] = new_val

        # Refresh the row widget
        try:
            self.query_one(
                f"#row-{setting.full_key.replace('.', '-')}", SettingRow
            ).update_value(new_val, changed=True)
        except Exception:
            pass

    # ── Dismiss ───────────────────────────────────────────────────────────────

    async def action_dismiss_or_clear(self) -> None:
        if self._searching:
            self._exit_search()
            return
        self.dismiss({"session_changes": self._state_changes})

    # ── Search ────────────────────────────────────────────────────────────────

    async def on_key(self, event) -> None:  # type: ignore[override]
        if event.key == "slash" and not self._searching:
            event.prevent_default()
            event.stop()
            self._enter_search()

    def _enter_search(self) -> None:
        self._searching = True
        try:
            self.query_one("#config-search", Input).focus()
        except Exception:
            pass
        self._refresh_footer()

    def _exit_search(self) -> None:
        self._searching = False
        try:
            self.query_one("#config-search", Input).value = ""
        except Exception:
            pass
        for s in ALL_SETTINGS:
            try:
                self.query_one(f"#row-{s.full_key.replace('.', '-')}", SettingRow).display = True
            except Exception:
                pass
        self._refresh_footer()
        self._focus_key(self._focused_key)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "config-search":
            self._exit_search()
            event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "config-search":
            return
        q = event.value.lower().strip()
        for s in ALL_SETTINGS:
            matches = not q or q in s.full_key.lower() or q in s.desc.lower()
            try:
                self.query_one(f"#row-{s.full_key.replace('.', '-')}", SettingRow).display = matches
            except Exception:
                pass
        visible = self._visible_keys()
        if visible and self._focused_key not in visible:
            self._focused_key = visible[0]

    def _refresh_footer(self) -> None:
        state: _FooterState = "searching" if self._searching else "browsing"
        try:
            self.query_one("#config-foot", Static).update(_build_footer(state))
        except Exception:
            pass
