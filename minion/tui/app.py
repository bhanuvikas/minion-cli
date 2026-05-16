"""MinionApp — the Textual Application that owns the terminal in TUI mode.

Layout (vertical stack):
  ConversationArea   — VerticalScroll; each content block is a child Static widget
  Slots              — live parallel agent/tool status (inline at end of ConversationArea)
  InspectorZone      — subagent transcript viewer (hidden by default)
  InputSection       — switches between InputRow (normal) and PermissionContent
    PermissionContent  — inline permission panel (hidden when idle)
    InputRow           — "you › " label + TextArea for user input
  SlashPreviewWidget — inline-preview slash-command completion overlay (hidden when idle)
  StatusLine         — 1-line docked status bar (below InputSection)

Non-TTY or MINION_NO_TUI=1: this module is not used; the console path
(PromptSession + Rich Live + questionary) remains active.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, cast

from rich.console import RenderableType
from rich.text import Text
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Center, Horizontal, VerticalScroll
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TextArea

from .agent_registry import get_registry
from .conversation import ConversationBuffer
from .inspector import InspectorScreen
from .messages import InspectorUpdated, SlotsUpdated
from .permission import PermissionPanel
from .setup_checklist import SetupChecklistPanel
from .slots import SlotsManager
from .status import StatusBar
from .theme import MINION_TCSS


# ── Internal messages (app-private event bus) ─────────────────────────────────

class TuiSubmit(Message):
    """User has submitted text from the InputArea."""
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class TuiHistoryNav(Message):
    """User pressed ↑/↓ to navigate history."""
    def __init__(self, direction: int) -> None:
        self.direction = direction   # -1 = older, +1 = newer
        super().__init__()


class TuiUpdateCompletion(Message):
    """Text has changed; update the completion overlay."""
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        super().__init__()


class TuiOpenHelp(Message):
    """User pressed ? in the completion dropdown — open /help modal."""
    def __init__(self, initial_cmd: str | None = None) -> None:
        self.initial_cmd = initial_cmd
        super().__init__()


# ── SlashPreviewWidget color tokens ───────────────────────────────────────────

_SP_BRIGHT   = "#f5d76e"  # highlighted cmd name + pointer
_SP_CYAN     = "#1E90FF"  # command names, related links (minion blue)
_SP_GREEN    = "#6ed084"  # usage patterns
_SP_DIM_YEL  = "#b8a030"  # labels: usage, related
_SP_DIM      = "#6a6a6a"  # descriptions, footer
_SP_TINT_BG  = "#1a1400"  # faint yellow tint for selected row + expansion
_SP_KEYCAP   = "#2a2a2a"  # keycap pill background

# Category pill — single gold accent matching the minion brand
_CAT_PILL_FG = "#e6c34a"   # gold text
_CAT_PILL_BG = "#1f1a08"   # very dark gold background

# ── Custom widget classes ─────────────────────────────────────────────────────

class ConversationArea(VerticalScroll):
    """Scrollable conversation — each content block is a child Static widget.

    append_block() mounts a new Static and scrolls to bottom.
    Thinking / streaming widgets are mounted and removed in-place so they
    always appear immediately after the last committed message.

    The live slots widget (_slots_widget) is also a child, always kept as the
    last node. append_block() inserts before it when slots are active so new
    blocks stay above the live status display.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._slots_widget: Optional[Static] = None

    def append_block(self, renderable: RenderableType) -> Static:
        """Mount a new Static block and scroll to the bottom."""
        widget = Static(renderable)
        # Keep slots widget as the last child by inserting before it.
        if self._slots_widget is not None and self._slots_widget.is_attached:
            self.mount(widget, before=self._slots_widget)
        else:
            self.mount(widget)
        self.scroll_end(animate=False, x_axis=False)
        return widget

    def update_slots(self, text: RenderableType, visible: bool) -> None:
        """Show, update, or hide the inline live slots widget."""
        if visible:
            if self._slots_widget is None:
                self._slots_widget = Static(text)
                self.mount(self._slots_widget)
            else:
                self._slots_widget.update(text)
        elif self._slots_widget is not None:
            # Summary lines were already inserted before this widget via
            # append_block(); remove it now so they become the new tail.
            self._slots_widget.remove()
            self._slots_widget = None
        self.scroll_end(animate=False, x_axis=False)

    def clear_log(self) -> None:
        """Remove all child widgets and reset scroll position."""
        self.remove_children()
        self._slots_widget = None
        self.scroll_home(animate=False)


class SlotsZone(Static):
    """Live parallel agent/tool status (kept for CSS selector; not composed)."""


class InspectorZone(Static):
    """Subagent transcript viewer."""


class SetupChecklistZone(Widget):
    """Hosts the first-run setup checklist between the conversation and input."""

    def compose(self) -> ComposeResult:
        yield Static("", id="cl-header")
        yield Static("", classes="cl-row", id="cl-row-0")
        yield Static("", classes="cl-row", id="cl-row-1")
        yield Static("", classes="cl-row", id="cl-row-2")
        yield Static("", id="cl-footer")


class PermissionContent(Static):
    """Inline permission panel — shown inside InputSection when confirmation is needed."""

    # Must be focusable so that Enter/Esc are NOT intercepted by InputArea's
    # priority=True binding (which fires before App.on_key when InputArea is
    # focused). With PermissionContent focused, InputArea is not in the focus
    # chain and its bindings are inactive — keys bubble cleanly to App.on_key.
    can_focus = True


class InputArea(TextArea):
    """Multiline input box with submit, newline-insert, and history navigation."""

    cursor_type = "line"

    BINDINGS = [
        Binding("enter",         "submit_input",         "Submit",      priority=True, show=False),
        Binding("shift+enter",   "insert_newline",        "New line",    show=False),
        Binding("ctrl+j",        "insert_newline",        "New line",    show=False),
        Binding("up",            "navigate_history_up",   "Hist ↑",      show=False),
        Binding("down",          "navigate_history_down", "Hist ↓",      show=False),
        # Word deletion: alt+backspace = Option+Delete on macOS; ctrl+w = Unix
        Binding("alt+backspace", "delete_word_left",      "Del word",    show=False),
        Binding("ctrl+w",        "delete_word_left",      "Del word",    show=False),
    ]

    def on_mount(self) -> None:
        from rich.style import Style
        from textual.widgets.text_area import TextAreaTheme
        # Register a minimal theme that maps our custom capture name to gold.
        # All other theme fields left None so CSS controls cursor/selection/etc.
        self.register_theme(TextAreaTheme(
            name="minion-input",
            syntax_styles={"slash.cmd": Style(color="#FFD700", bold=True)},
        ))
        self.theme = "minion-input"

    def _build_highlight_map(self) -> None:
        # super() clears _highlights and returns early (no language/tree-sitter).
        super()._build_highlight_map()
        text = self.text
        if not text.startswith("/") or "\n" in text:
            return
        word = text.split()[0] if text.split() else text.rstrip()
        from ..repl import REPL_COMMANDS as _CMDS
        if word in _CMDS:
            self._highlights[0].append((0, len(word), "slash.cmd"))

    # Set before load_text() so the resulting Changed event doesn't re-open the dropdown.
    _suppress_next_completion: bool = False

    def _apply_completion(self, dropdown: "SlashPreviewWidget") -> str:
        """Fill the input with the highlighted command; return the cmd string."""
        cmd = dropdown.highlighted_cmd or ""
        if cmd:
            # load_text fires exactly one Changed event; suppress it so the
            # dropdown doesn't reopen after we close it.
            self._suppress_next_completion = True
            self.load_text(cmd)
            self.move_cursor((0, len(cmd)))
        dropdown.display = False
        return cmd

    def action_submit_input(self) -> None:
        try:
            dropdown = self.app.query_one(SlashPreviewWidget)
            if dropdown.display:
                # Enter fills the input with the selected command; the user
                # can review / append arguments before pressing Enter again.
                self._apply_completion(dropdown)
                return
        except Exception:
            pass
        text = self.text.strip()
        if text:
            self.post_message(TuiSubmit(text))
        elif hasattr(self.app, "_is_checklist_visible"):
            # Empty Enter + visible checklist → activate the focused row
            _mapp = cast("MinionApp", self.app)
            if _mapp._is_checklist_visible():
                _mapp.checklist.activate_current()

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def action_navigate_history_up(self) -> None:
        try:
            dropdown = self.app.query_one(SlashPreviewWidget)
            if dropdown.display:
                dropdown.move_up()
                return
        except Exception:
            pass
        if hasattr(self.app, "_is_checklist_visible"):
            _mapp = cast("MinionApp", self.app)
            if _mapp._is_checklist_visible():
                _mapp.checklist.move_cursor(-1)
                _mapp._refresh_checklist()
                return
        self.app.post_message(TuiHistoryNav(direction=-1))

    def action_navigate_history_down(self) -> None:
        try:
            dropdown = self.app.query_one(SlashPreviewWidget)
            if dropdown.display:
                dropdown.move_down()
                return
        except Exception:
            pass
        if hasattr(self.app, "_is_checklist_visible"):
            _mapp = cast("MinionApp", self.app)
            if _mapp._is_checklist_visible():
                _mapp.checklist.move_cursor(1)
                _mapp._refresh_checklist()
                return
        self.app.post_message(TuiHistoryNav(direction=1))

    def on_key(self, event: Key) -> None:
        # x dismisses the setup checklist when it's visible and the input is empty
        if event.key == "x" and not self.text.strip():
            if hasattr(self.app, "_is_checklist_visible"):
                _mapp = cast("MinionApp", self.app)
                if _mapp._is_checklist_visible():
                    _mapp.hide_setup_checklist()
                    event.prevent_default()
                    return

        try:
            dropdown = self.app.query_one(SlashPreviewWidget)
        except Exception:
            return
        if not dropdown.display:
            return
        if event.key == "escape":
            dropdown.display = False
            event.prevent_default()
        elif event.key == "question_mark":
            initial_cmd = dropdown.highlighted_cmd
            dropdown.display = False
            self.post_message(TuiOpenHelp(initial_cmd))
            event.prevent_default()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._suppress_next_completion:
            self._suppress_next_completion = False
            return
        text = event.text_area.text
        try:
            dropdown = self.app.query_one(SlashPreviewWidget)
        except Exception:
            return
        if text.startswith("/") and "\n" not in text:
            self.app.post_message(TuiUpdateCompletion(text))
        else:
            dropdown.display = False


class InputRow(Horizontal):
    """Input row: "you › " prefix label + InputArea."""

    def compose(self) -> ComposeResult:
        yield Static("[bold #FFD700]you ›[/] ", classes="input-prefix", id="you-prefix")
        yield InputArea(language=None, id="input-area")


class InputSection(Widget):
    """Container that switches between InputRow (normal) and PermissionContent."""

    def compose(self) -> ComposeResult:
        yield PermissionContent("", id="permission-content")
        yield InputRow(id="input-row")


class SlashPreviewWidget(Static):
    """Inline-preview slash-command autocomplete dropdown."""

    def __init__(self, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._matches: list[str] = []
        self._selected: int = 0
        self._prefix: str = ""
        self._total: int = 0

    def update_matches(self, prefix: str, matches: list[str], total: int) -> None:
        self._prefix = prefix
        self._matches = matches
        self._selected = 0
        self._total = total
        self._refresh_content()

    def move_up(self) -> None:
        if self._matches:
            self._selected = (self._selected - 1) % len(self._matches)
            self._refresh_content()

    def move_down(self) -> None:
        if self._matches:
            self._selected = (self._selected + 1) % len(self._matches)
            self._refresh_content()

    @property
    def highlighted_cmd(self) -> str | None:
        return self._matches[self._selected] if self._matches else None

    def _refresh_content(self) -> None:
        self.update(self._build_dropdown())

    def _build_dropdown(self) -> RenderableType:
        from ..repl import REPL_COMMANDS as _CMDS
        from rich.table import Table
        try:
            from .screens.help_screen import COMMAND_DETAIL
        except Exception:
            COMMAND_DETAIL = {}  # type: ignore[assignment]

        # Table.grid(expand=True) makes Rich extend every row's background to
        # the full terminal width — the only reliable way to span the highlight.
        tbl = Table.grid(expand=True, padding=0)
        tbl.add_column("content", no_wrap=True, overflow="ellipsis")

        prefix_len = len(self._prefix)
        # Layout columns (all units = terminal characters):
        #   gutter (5) + name (11) + gap (2) = description starts at col 18
        #   indent (18) + label (9)           = values start at col 27
        NAME_W  = 11
        DESC_W  = 62   # wide enough for longest short_desc (57 chars) + margin
        GAP     = 2
        LABEL_W = 9    # "usage    " / "related  " — 9 chars each
        INDENT  = " " * (5 + NAME_W + GAP)  # 18 spaces for expansion rows
        TINT_S  = f"on {_SP_TINT_BG}"

        # Skill registry for dynamic commands not in COMMAND_DETAIL
        try:
            from .screens.help_screen import CmdInfo as _CmdInfo
        except Exception:
            _CmdInfo = None  # type: ignore[assignment]
        _skill_registry = getattr(self.app, "_skill_registry", None)

        def _cat_pill(t: Text, category: str) -> None:
            t.append(f" {category} ", style=f"{_CAT_PILL_FG} on {_CAT_PILL_BG}")

        def _trow(text: Text, highlighted: bool = False) -> None:
            tbl.add_row(text, style=TINT_S if highlighted else "")

        def _get_info(cmd: str):  # type: ignore[return]
            """Return CmdInfo for static commands, or a minimal equivalent for skills."""
            static = COMMAND_DETAIL.get(cmd)
            if static is not None:
                return static
            if _skill_registry is not None and _CmdInfo is not None:
                skill_name = cmd.lstrip("/")
                skill = _skill_registry.get(skill_name)
                if skill is not None:
                    return _CmdInfo(
                        name=cmd,
                        short_desc=skill.description,
                        long_desc=skill.description,
                        usage=[cmd],
                        related=["/skills"],
                        category="skills",
                    )
            return None

        for i, cmd in enumerate(self._matches):
            is_sel = i == self._selected
            info = _get_info(cmd)
            short_desc = info.short_desc if info else _CMDS.get(cmd, "")
            category   = info.category   if info else ""

            if is_sel:
                # ── Highlighted main row ──────────────────────────────────────
                row = Text(no_wrap=True, overflow="ellipsis")
                row.append("  ▸  ", style=f"bold {_SP_BRIGHT}")
                if prefix_len and cmd.startswith(self._prefix):
                    row.append(cmd[:prefix_len], style=f"bold {_SP_BRIGHT}")
                    remainder = cmd[prefix_len:]
                else:
                    remainder = cmd
                row.append(remainder.ljust(max(0, NAME_W - prefix_len)), style=_SP_CYAN)
                row.append("  ")
                # Pad description to DESC_W so pill lands at consistent column
                row.append(short_desc[:DESC_W].ljust(DESC_W))
                row.append("  ")
                if category:
                    _cat_pill(row, category)
                _trow(row, highlighted=True)

                # ── Expansion block: usage, one line per invocation ───────────
                if info and info.usage:
                    for j, usage_item in enumerate(info.usage):
                        urow = Text(no_wrap=True, overflow="ellipsis")
                        urow.append(INDENT)
                        urow.append("usage".ljust(LABEL_W) if j == 0 else " " * LABEL_W,
                                    style=_SP_DIM_YEL if j == 0 else "")
                        urow.append(usage_item, style=_SP_GREEN)
                        _trow(urow, highlighted=True)

                # ── Expansion block: related, one line per command ────────────
                if info and info.related:
                    for j, rel in enumerate(info.related[:4]):
                        rrow = Text(no_wrap=True, overflow="ellipsis")
                        rrow.append(INDENT)
                        rrow.append("related".ljust(LABEL_W) if j == 0 else " " * LABEL_W,
                                    style=_SP_DIM_YEL if j == 0 else "")
                        rrow.append(rel, style=_SP_CYAN)
                        _trow(rrow, highlighted=True)
            else:
                # ── Compact row ───────────────────────────────────────────────
                row = Text(no_wrap=True, overflow="ellipsis")
                row.append("     ")
                if prefix_len and cmd.startswith(self._prefix):
                    row.append(cmd[:prefix_len], style=f"bold {_SP_BRIGHT}")
                    row.append(cmd[prefix_len:].ljust(max(0, NAME_W - prefix_len)), style=_SP_CYAN)
                else:
                    row.append(cmd.ljust(NAME_W), style=_SP_CYAN)
                row.append("  ")
                row.append(short_desc[:DESC_W].ljust(DESC_W), style=_SP_DIM)
                row.append("  ")
                if category:
                    _cat_pill(row, category)
                _trow(row)

        # ── Footer ────────────────────────────────────────────────────────────
        tbl.add_row(Text(""))

        foot = Text(no_wrap=True)

        def _kpill(label: str) -> None:
            foot.append(f" {label} ", style=f"bold white on {_SP_KEYCAP}")

        foot.append("     ")
        _kpill("↑↓")
        foot.append(" pick  ·  ", style=_SP_DIM)
        _kpill("↵")
        foot.append(" fill  ·  ", style=_SP_DIM)
        _kpill("?")
        foot.append(" full help  ·  ", style=_SP_DIM)
        _kpill("esc")
        foot.append(" dismiss", style=_SP_DIM)
        foot.append(f"  {len(self._matches)} of {self._total}", style=_SP_DIM)
        tbl.add_row(foot)

        return tbl


class StatusLine(Static):
    """1-line docked status bar."""


# ── Main Application ──────────────────────────────────────────────────────────

class MinionApp(App):
    """Full-screen Textual TUI for minion."""

    CSS = MINION_TCSS

    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit",       "Cancel/Quit", show=False),
        Binding("ctrl+l", "clear_conversation",    "Clear",       show=False),
        Binding("ctrl+o", "toggle_inspector",      "Inspector",   show=False),
        Binding("ctrl+y", "copy_last_response",    "Copy",        show=False),
    ]

    def __init__(
        self,
        model_name: str,
        *,
        agent_registry=None,
        skill_registry=None,
        a2a_manager=None,
        # Legacy parameters — ignored; completion uses registries directly
        completer=None,
        completer_data=None,
    ) -> None:
        super().__init__()
        self._model_name     = model_name
        self._agent_registry = agent_registry
        self._skill_registry = skill_registry
        self._a2a_manager    = a2a_manager
        self._terminal_width = 120
        self._thinking       = False
        self._think_timer    = None
        self._current_task: Optional[asyncio.Task] = None

        # Live animation widgets — mounted as children of ConversationArea
        self._thinking_widget:  Optional[Static] = None
        self._streaming_widget: Optional[Static] = None

        self._on_submit:    Optional[Callable[[str], Awaitable[None]]] = None
        self._on_quit:      Optional[Callable[[], Awaitable[None]]]   = None
        self._on_first_run: Optional[Callable[[], None]]              = None
        self._startup_warnings: list[str] = []

        # In-memory + persistent history
        self._history:    list[str] = []
        self._hist_idx:   int = -1
        self._hist_saved: str = ""
        self._history_path = Path.home() / ".minion" / "history"
        self._load_history()

        # Component state machines (not Textual widgets)
        self.conversation = ConversationBuffer()
        self.permission   = PermissionPanel(app_ref=self)
        self.checklist    = SetupChecklistPanel()
        self.status       = StatusBar(model_name=model_name, width=self._terminal_width)

        _reg = get_registry()
        _reg.set_post_message(self.post_message)
        self.slots = SlotsManager(post_message_fn=self.post_message)

        # Modal screen reference — set while the inspector is pushed, None otherwise.
        self._inspector_screen: Optional[InspectorScreen] = None

        # Wire conversation callbacks
        self.conversation.set_callbacks(
            write_block_fn=self._write_block,
            refresh_fn=self._refresh_streaming,
            pre_finalize_fn=self._pre_finalize_streaming,
        )

        # Widget references populated in on_mount()
        self._conv_area:          Optional[ConversationArea]    = None
        self._input_section:      Optional[InputSection]        = None
        self._permission_content: Optional[PermissionContent]   = None
        self._input_row:          Optional[InputRow]            = None
        self._completion_list:    Optional[SlashPreviewWidget]   = None
        self._status_line:        Optional[StatusLine]          = None
        self._input_area:         Optional[InputArea]           = None
        self._setup_zone:         Optional[Widget]               = None   # the Center wrapper
        self._setup_cl_header:    Optional[Static]              = None
        self._setup_cl_rows:      list[Static]                  = []
        self._setup_cl_footer:    Optional[Static]              = None

    # ── History helpers ───────────────────────────────────────────────────────

    def _load_history(self) -> None:
        try:
            if self._history_path.exists():
                self._history = [
                    l.strip() for l in self._history_path.read_text().splitlines()
                    if l.strip() and not l.startswith("#")
                ]
        except Exception:
            self._history = []

    def _save_history_entry(self, text: str) -> None:
        try:
            self._history_path.parent.mkdir(exist_ok=True)
            with self._history_path.open("a", encoding="utf-8") as f:
                f.write(text.replace("\n", " ") + "\n")
        except Exception:
            pass

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield ConversationArea(id="conv-area")
        with Center(id="setup-zone-center"):
            yield SetupChecklistZone(id="setup-zone")
        yield InputSection(id="input-section")
        yield SlashPreviewWidget(id="completion-list")
        yield StatusLine("", id="status-line")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._conv_area          = self.query_one("#conv-area",              ConversationArea)
        self._setup_zone         = self.query_one("#setup-zone-center", Center)
        self._setup_cl_header    = self.query_one("#cl-header",   Static)
        self._setup_cl_rows      = [self.query_one(f"#cl-row-{i}", Static) for i in range(3)]
        self._setup_cl_footer    = self.query_one("#cl-footer",   Static)
        self._input_section      = self.query_one("#input-section",          InputSection)
        self._permission_content = self.query_one("#permission-content",     PermissionContent)
        self._input_row          = self.query_one("#input-row",              InputRow)
        self._completion_list    = self.query_one("#completion-list",        SlashPreviewWidget)
        self._status_line        = self.query_one("#status-line",            StatusLine)
        self._input_area         = self.query_one("#input-area",             InputArea)

        # Push MINION_THEME onto Textual's shared console so that custom style
        # names (muted, primary, …) resolve correctly when Static widgets render
        # Text objects that contain those markup tags.
        from ..theme.palette import MINION_THEME as _MT
        self.console.push_theme(_MT)

        import shutil as _shutil
        cols = _shutil.get_terminal_size().columns or 120
        self._terminal_width = cols
        self.status.set_width(cols)

        # Initially hidden zones (PermissionContent starts hidden via CSS)
        self._completion_list.display = False

        # Thinking animation timer (paused until first prompt)
        self._think_timer = self.set_interval(0.25, self._tick_thinking, pause=True)

        self._refresh_status()
        self._write_banner()
        if self._on_first_run is not None:
            self.call_after_refresh(self._on_first_run)
        self.set_focus(self._input_area)

    def on_resize(self, event) -> None:
        self._terminal_width = event.size.width
        self.status.set_width(event.size.width)
        self._refresh_status()
        if self._completion_list is not None and self._completion_list.display:
            self.call_after_refresh(self._position_completion_list)

    # ── run_async override ────────────────────────────────────────────────────

    async def run_async(  # type: ignore[override]
        self,
        *,
        on_submit:    Optional[Callable[[str], Awaitable[None]]] = None,
        on_quit:      Optional[Callable[[], Awaitable[None]]]    = None,
        on_first_run: Optional[Callable[[], None]]               = None,
        **kwargs,
    ) -> None:
        if on_submit is not None:
            self._on_submit = on_submit
        if on_quit is not None:
            self._on_quit = on_quit
        if on_first_run is not None:
            self._on_first_run = on_first_run
        await super().run_async(**kwargs)
        # Post-exit: print a resume hint to the restored terminal
        rule = "\033[38;2;192;192;192m" + "─" * self._terminal_width + "\033[0m\n"
        sys.stdout.write(rule)
        sys.stdout.write("  minion session ended — run `minion` to resume\n")
        sys.stdout.flush()

    # ── Actions ───────────────────────────────────────────────────────────────

    async def action_cancel_or_quit(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        else:
            if self._on_quit is not None:
                await self._on_quit()
            self.exit()

    def action_clear_conversation(self) -> None:
        self.conversation.clear()
        self._thinking_widget  = None
        self._streaming_widget = None
        if self._conv_area is not None:
            self._conv_area.clear_log()

    def action_toggle_inspector(self) -> None:
        if self.permission.is_visible:
            return
        if self._inspector_screen is not None:
            try:
                self._inspector_screen.dismiss()
            except Exception:
                self._inspector_screen = None
            return
        states = get_registry().get_all()
        if not states:
            return
        screen = InspectorScreen(registry=get_registry())
        self._inspector_screen = screen
        def _on_dismiss(_result) -> None:
            self._inspector_screen = None
        self.push_screen(screen, _on_dismiss)

    def action_copy_last_response(self) -> None:
        """Copy the last assistant response to the clipboard (OSC 52 / terminal clipboard)."""
        text = self.conversation.last_assistant_text
        if not text:
            self.notify("No response to copy yet.", severity="warning", timeout=2)
            return
        try:
            self.copy_to_clipboard(text)
            self.notify("Last response copied to clipboard.", timeout=2)
        except Exception:
            self.notify("Copy not supported by your terminal.", severity="warning", timeout=3)

    # ── Key handler for permission + inspector navigation ─────────────────────

    def on_key(self, event: Key) -> None:
        if self.permission.is_visible:
            handled = True
            k = event.key
            if k == "1":
                self.permission.confirm_by_index(0)
            elif k == "2":
                self.permission.confirm_by_index(1)
            elif k == "3":
                self.permission.confirm_by_index(2)
            elif k in ("4", "n", "escape"):
                self.permission.deny()
            elif k == "enter":
                self.permission.confirm_current()
            elif k == "up":
                self.permission.move_cursor(-1)
                self._refresh_permission()
                handled = False   # don't swallow but still refresh
            elif k == "down":
                self.permission.move_cursor(1)
                self._refresh_permission()
                handled = False
            else:
                handled = False
            if handled:
                event.prevent_default()
            return

        if self._is_checklist_visible() and event.key == "x":
            self.hide_setup_checklist()
            event.prevent_default()
            return

    # ── Message handlers ──────────────────────────────────────────────────────

    def on_slots_updated(self, _: SlotsUpdated) -> None:
        if self._conv_area is None:
            return
        self._conv_area.update_slots(self.slots.get_rich_text(), self.slots.is_visible)

    def on_inspector_updated(self, _: InspectorUpdated) -> None:
        if self._inspector_screen is not None:
            self._inspector_screen.refresh_from_registry()

    def on_tui_submit(self, message: TuiSubmit) -> None:
        """User submitted text from InputArea."""
        text = message.text
        if not text:
            return
        if self._input_area is not None:
            self._input_area.clear()
        self._history.append(text)
        self._hist_idx  = -1
        self._hist_saved = ""
        self._save_history_entry(text)
        self.conversation.append_user(text)
        self._set_thinking(True)
        if self._on_submit is not None:
            self._current_task = asyncio.ensure_future(self._run_submit(text))

    def on_tui_history_nav(self, message: TuiHistoryNav) -> None:
        if self._input_area is None or not self._history:
            return
        direction = message.direction   # -1 = older (up), +1 = newer (down)
        if self._hist_idx == -1:
            self._hist_saved = self._input_area.text
        new_idx = self._hist_idx - direction
        if new_idx < -1:
            new_idx = -1
        if new_idx >= len(self._history):
            new_idx = len(self._history) - 1
        self._hist_idx = new_idx
        text = self._hist_saved if new_idx == -1 else self._history[-(new_idx + 1)]
        # Suppress completion: load_text fires one Changed event; the flag
        # prevents that event from reopening the dropdown for slash commands.
        self._input_area._suppress_next_completion = True
        if self._completion_list is not None:
            self._completion_list.display = False
        self._input_area.load_text(text)
        self._input_area.move_cursor((0, len(text)))

    def on_tui_update_completion(self, message: TuiUpdateCompletion) -> None:
        if self._completion_list is None:
            return
        prefix = message.prefix.lower()
        from ..repl import REPL_COMMANDS as _CMDS
        matches = [cmd for cmd in _CMDS if cmd.startswith(prefix)]
        if matches:
            self._completion_list.update_matches(prefix, matches, len(_CMDS))
            self._completion_list.display = True
            self.call_after_refresh(self._position_completion_list)
        else:
            self._completion_list.display = False

    def on_tui_open_help(self, message: TuiOpenHelp) -> None:
        from .screens import HelpScreen

        async def _on_done(result: str | None) -> None:
            if result:
                self.prefill_input(result)

        self.push_screen(
            HelpScreen(skill_registry=self._skill_registry, initial_cmd=message.initial_cmd),
            _on_done,
        )

    def _position_completion_list(self) -> None:
        """Pin the completion dropdown's bottom edge flush to InputSection's top.

        dock: bottom anchors the widget to the screen bottom on the overlay layer.
        margin-bottom = screen_height - input_section.top pushes its bottom edge
        up to exactly flush with the input box, so the gap is always zero
        regardless of how many options are shown.
        """
        if self._completion_list is None or self._input_section is None:
            return
        try:
            input_top  = self._input_section.region.y
            screen_h   = self.size.height
            bottom_gap = screen_h - input_top
            # top=0, right=2, bottom=bottom_gap, left=2
            self._completion_list.styles.margin = (0, 2, bottom_gap, 2)
        except Exception:
            pass

    # ── Submit runner ─────────────────────────────────────────────────────────

    async def _run_submit(self, text: str) -> None:
        if self._on_submit is None:
            return
        try:
            await self._on_submit(text)
        except asyncio.CancelledError:
            self.conversation.append_system("[#C0C0C0]⚠ Cancelled.[/]")
            self.conversation.finalize_turn()
        except Exception as exc:
            self.conversation.append_system(f"[red]Error: {exc}[/]")
        finally:
            self._set_thinking(False)
            if self._input_area is not None:
                try:
                    self.set_focus(self._input_area)
                except ScreenStackError:
                    pass  # app shutting down — screen stack already cleared

    # ── Thinking animation ────────────────────────────────────────────────────

    def _tick_thinking(self) -> None:
        """Animate the thinking widget (runs on event loop at 0.25s intervals)."""
        if not self.conversation._is_thinking:
            return
        if self.conversation._is_streaming:
            return  # streaming phase owns its own widget
        if self._thinking_widget is None:
            return
        self._thinking_widget.update(self.conversation.get_streaming_renderable())

    def _set_thinking(self, thinking: bool) -> None:
        self._thinking = thinking
        self.conversation.set_thinking(thinking)
        self.status.set_thinking(thinking)
        self._refresh_status()
        if thinking:
            if self._conv_area is not None:
                self._thinking_widget = self._conv_area.append_block(
                    self.conversation.get_streaming_renderable()
                )
            if self._think_timer is not None:
                try:
                    self._think_timer.resume()
                except Exception:
                    pass
        else:
            if self._think_timer is not None:
                try:
                    self._think_timer.pause()
                except Exception:
                    pass
            self._dismiss_thinking()

    # ── Transient widget helpers ──────────────────────────────────────────────

    def _dismiss_thinking(self) -> None:
        """Remove the thinking indicator widget if present."""
        if self._thinking_widget is not None:
            try:
                self._thinking_widget.remove()
            except Exception:
                pass
            self._thinking_widget = None

    def _dismiss_streaming(self) -> None:
        """Remove the streaming preview widget if present."""
        if self._streaming_widget is not None:
            try:
                self._streaming_widget.remove()
            except Exception:
                pass
            self._streaming_widget = None

    # ── Write paths ───────────────────────────────────────────────────────────

    def _on_tui_loop(self) -> bool:
        """True when the caller is running on the TUI's own event loop.

        asyncio.get_running_loop() also succeeds on worker threads that run a
        subagent loop (L_sub).  Using it alone to decide whether to call _do()
        directly would mount Textual widgets on L_sub — their asyncio.Task would
        be bound to L_sub instead of the TUI loop, crashing gather() at shutdown.
        We must check the identity of the running loop, not just its existence.
        """
        tui_loop = self._loop
        if tui_loop is None:
            return False
        try:
            return asyncio.get_running_loop() is tui_loop
        except RuntimeError:
            return False

    def _write_block(self, block) -> None:
        """Append a Rich renderable block as a new Static widget. Thread-safe.

        Called by ConversationBuffer._emit() from both the event loop (streaming)
        and worker threads (tool calls, system messages).
        """
        if self._conv_area is None:
            from rich.console import Console
            Console().print(block)
            return

        def _do() -> None:
            self._dismiss_thinking()
            if self._conv_area is not None:
                self._conv_area.append_block(block)

        if self._on_tui_loop():
            _do()
        else:
            self.call_from_thread(_do)

    def _print_ansi_to_scrollback(self, ansi: str) -> None:
        """External path: marks conversation.mark_printed() then writes."""
        from rich.text import Text
        self.conversation.mark_printed()
        block = Text.from_ansi(ansi.rstrip("\n")) if ansi.strip() else Text(" ")
        self._write_block(block)

    def _write_markup(self, markup: str) -> None:
        """Parse Rich markup and write to the conversation."""
        from .render import render_rich as _render_rich
        self._write_block(_render_rich(markup))

    # ── Refresh helpers ───────────────────────────────────────────────────────

    def _refresh_streaming(self) -> None:
        """Create or update the streaming preview widget. Thread-safe."""
        if not self.conversation._is_streaming:
            return

        renderable = self.conversation.get_streaming_renderable()

        def _do() -> None:
            if self._conv_area is None:
                return
            self._dismiss_thinking()
            if self._streaming_widget is None:
                self._streaming_widget = self._conv_area.append_block(renderable)
            else:
                self._streaming_widget.update(renderable)
                self._conv_area.scroll_end(animate=False, x_axis=False)

        if self._on_tui_loop():
            _do()
        else:
            self.call_from_thread(_do)

    def _pre_finalize_streaming(self) -> None:
        """Remove the streaming preview widget before the final render is written."""
        def _do() -> None:
            self._dismiss_streaming()

        if self._on_tui_loop():
            _do()
        else:
            self.call_from_thread(_do)

    def _refresh_status(self) -> None:
        if self._status_line is None:
            return
        self._status_line.update(self.status.get_rich_markup())

    def _refresh_permission(self) -> None:
        if self._permission_content is not None:
            self._permission_content.update(self.permission.get_rich_markup())

    # ── Checklist show/hide/refresh ───────────────────────────────────────────

    def _is_checklist_visible(self) -> bool:
        return self._setup_zone is not None and self._setup_zone.display

    def _refresh_checklist(self) -> None:
        if self._setup_cl_header is None:
            return
        cl = self.checklist
        if cl.is_complete:
            self._setup_cl_header.update(cl.get_done_banner())
            for s in self._setup_cl_rows:
                s.display = False
            if self._setup_cl_footer:
                self._setup_cl_footer.display = False
        else:
            self._setup_cl_header.update(cl.get_header_markup())
            for i, s in enumerate(self._setup_cl_rows):
                s.display = True
                s.update(cl.get_row_markup(i))
                is_focused = (i == cl._cursor and cl._state[i] != "done")
                s.set_class(is_focused, "cl-row-focused")
            if self._setup_cl_footer:
                self._setup_cl_footer.display = True
                self._setup_cl_footer.update(cl.get_footer_markup())

    def prefill_input(self, text: str) -> None:
        """Insert text into the prompt buffer (e.g. command inserted from /help modal)."""
        try:
            ia = self.query_one(InputArea)
            ia._suppress_next_completion = True
            ia.load_text(text)
            ia.move_cursor((0, len(text)))
            ia.focus()
        except Exception:
            pass

    def show_setup_checklist(self) -> None:
        """Show the setup checklist zone (first-run or /setup re-trigger)."""
        self.checklist.reset()
        self._refresh_checklist()
        if self._setup_zone is not None:
            self._setup_zone.display = True
        if self._conv_area is not None:
            self.call_after_refresh(
                self._conv_area.scroll_end, animate=False, x_axis=False
            )

    def hide_setup_checklist(self) -> None:
        """Hide the checklist zone and write a summary to the conversation."""
        if self._setup_zone is not None:
            self._setup_zone.display = False
        done = self.checklist.done_count()
        if done > 0:
            self.conversation.append_system(
                f"[#4CAF50]setup · {done} of 3 steps completed[/]"
            )
        else:
            self.conversation.append_system("[#666666]setup dismissed · type /setup to run anytime[/]")
        self.conversation.finalize_turn()
        if self._input_area is not None:
            self.set_focus(self._input_area)

        # Fire dismiss callback if set
        if self.checklist.on_dismiss:
            self.checklist.on_dismiss()

    # ── Permission show/hide ──────────────────────────────────────────────────

    def show_permission(self) -> None:
        markup = self.permission.get_rich_markup()
        if self._permission_content is not None:
            self._permission_content.update(markup)
            self._permission_content.display = True
            # Move focus here so InputArea's priority Enter binding is inactive.
            # Keys bubble through PermissionContent → App.on_key which owns
            # all permission key handling (Enter, Esc, 1-4, ↑↓).
            self.set_focus(self._permission_content)
        if self._input_row is not None:
            self._input_row.display = False
        if self._input_section is not None:
            self._input_section.add_class("permission-active")
        # PermissionContent expands InputSection, shrinking ConversationArea (1fr).
        # Scroll to bottom after the layout reflows so recent messages stay visible.
        if self._conv_area is not None:
            self.call_after_refresh(
                self._conv_area.scroll_end, animate=False, x_axis=False
            )

    def hide_permission(self) -> None:
        # Diff is now written by the executor via _deferred_r.on_diff_preview()
        # immediately after confirm_async() returns, which guarantees correct
        # ordering in both single-tool and parallel-tool paths.
        if self._permission_content is not None:
            self._permission_content.display = False
        if self._input_row is not None:
            self._input_row.display = True
        if self._input_section is not None:
            self._input_section.remove_class("permission-active")
        if self._input_area is not None:
            self.set_focus(self._input_area)

    # ── Public API (called by session.py, TuiRenderer, confirmation.py) ───────

    def invalidate(self) -> None:
        self.refresh()

    def scroll_to_bottom(self) -> None:
        if self._conv_area is not None:
            self._conv_area.scroll_end(animate=False, x_axis=False)

    async def flush_writes_async(self) -> None:
        pass  # no-op — Textual renders per frame

    async def flush_and_exit(self) -> None:
        self.exit()

    def set_thinking(self, thinking: bool) -> None:
        self._set_thinking(thinking)

    def set_startup_warnings(self, warnings: list[str]) -> None:
        self._startup_warnings = list(warnings)

    def update_session(self, **kwargs) -> None:
        self.status.update_session(**kwargs)
        self._refresh_status()

    def print_to_terminal(self, rich_text: str) -> None:
        self.print_renderable(rich_text)

    def print_renderable(self, renderable) -> None:
        """Write any Rich renderable directly to the conversation."""
        self.conversation.mark_printed()
        self._write_block(renderable)

    # ── Banner ────────────────────────────────────────────────────────────────

    def _write_banner(self) -> None:
        """Mount greeting banner blocks into ConversationArea on startup."""
        try:
            from ..theme.banner import get_greeting_renderables as _get_renderables

            if self._conv_area is None:
                return

            for r in _get_renderables(
                model=self.status._model,
                provider=self.status._provider,
                project_name=self.status._project,
                cwd=self.status._cwd,
                agent_count=self.status._agents,
                memory_enabled=self.status._memory,
                mcp_count=self.status._mcp_count,
                a2a_count=self.status._a2a_count,
                version=self.status._version,
            ):
                self._conv_area.append_block(r)

            # Build warnings list: existing startup warnings + selection tip.
            # All items use the same "  [bold YELLOW]Tip[/]  message" markup
            # so they render consistently inside get_startup_warning_renderables().
            from .terminal import get_selection_tip as _get_tip
            from ..theme.palette import YELLOW as _YELLOW
            _tip_markup = f"  [bold {_YELLOW}]Tip[/]      [#888888]{_get_tip()}[/]"
            _all_warnings = list(self._startup_warnings) + [_tip_markup]

            from ..theme.banner import get_startup_warning_renderables as _get_warn
            for r in _get_warn(_all_warnings):
                self._conv_area.append_block(r)
        except Exception:
            pass
