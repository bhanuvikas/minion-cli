"""MinionApp — the Textual Application that owns the terminal in TUI mode.

Layout (vertical stack):
  ConversationArea  — ConversationLog for all turns including live streaming
  SlotsZone         — live parallel agent/tool status (hidden when idle)
  InspectorZone     — subagent transcript viewer (hidden by default)
  InputSection      — switches between InputRow (normal) and PermissionContent
    PermissionContent — inline permission panel (hidden when idle)
    InputRow          — "you › " label + TextArea for user input
  CompletionList    — slash-command completion overlay (hidden when idle)
  StatusLine        — 1-line docked status bar (below InputSection)

Non-TTY or MINION_NO_TUI=1: this module is not used; the console path
(PromptSession + Rich Live + questionary) remains active.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.geometry import Size
from textual.binding import Binding
from textual.containers import Horizontal
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .agent_registry import get_registry
from .conversation import ConversationBuffer
from .inspector import InspectorPanel
from .messages import InspectorUpdated, SlotsUpdated
from .permission import PermissionPanel
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


# ── Custom widget classes ─────────────────────────────────────────────────────

class ConversationLog(RichLog):
    """RichLog with checkpoint-based line removal for thinking/streaming animation."""

    def checkpoint(self) -> int:
        """Return the current line count as a revert point."""
        return len(self.lines)

    def pop_to(self, n: int) -> None:
        """Remove all lines after index n."""
        if n >= len(self.lines):
            return
        self.lines = self.lines[:n]
        self._line_cache.clear()   # stale cache entries must be evicted or old strips re-appear
        self.virtual_size = Size(self.virtual_size.width, len(self.lines))
        self.refresh()


class ConversationArea(Widget):
    """Hosts the ConversationLog — completed turns and live streaming/thinking."""

    def compose(self) -> ComposeResult:
        yield ConversationLog(markup=True, highlight=False, auto_scroll=True, id="rich-log")

    def write_ansi(self, ansi: str) -> None:
        rl = self.query_one(ConversationLog)
        # Strip the one trailing \n that _emit guarantees on every call so it
        # doesn't produce a spurious blank line.  Intentional blank lines
        # (extra \n in the middle or from append_user's trailing "\n\n") are
        # preserved because we split and write each line separately — that way
        # blank lines become explicit rl.write("") calls which reliably
        # produce Strip.blank entries in the RichLog.
        if ansi.endswith("\n"):
            ansi = ansi[:-1]
        for line in ansi.split("\n"):
            rl.write(Text.from_ansi(line), expand=True)

    def write_markup(self, markup: str) -> None:
        self.query_one(ConversationLog).write(markup)

    def clear_log(self) -> None:
        self.query_one(ConversationLog).clear()


class StreamingZone(Static):
    """Live in-progress assistant response / thinking indicator."""


class SlotsZone(Static):
    """Live parallel agent/tool status."""


class InspectorZone(Static):
    """Subagent transcript viewer."""


class PermissionContent(Static):
    """Inline permission panel — shown inside InputSection when confirmation is needed."""


class InputArea(TextArea):
    """Multiline input box with submit, newline-insert, and history navigation."""

    BINDINGS = [
        Binding("enter",  "submit_input",         "Submit",    priority=True, show=False),
        Binding("ctrl+j", "insert_newline",        "New line",  show=False),
        Binding("up",     "navigate_history_up",   "Hist ↑",    show=False),
        Binding("down",   "navigate_history_down", "Hist ↓",    show=False),
    ]

    def action_submit_input(self) -> None:
        # Apply completion if overlay is visible
        try:
            cl = self.app.query_one(CompletionList)
            if cl.display:
                highlighted = cl.highlighted
                if highlighted is not None:
                    opt = cl.get_option_at_index(highlighted)
                    cmd = str(getattr(opt, "id", "") or "")
                    if cmd:
                        self.clear()
                        self.insert(cmd)
                        cl.display = False
                        return
        except Exception:
            pass
        text = self.text.strip()
        if text:
            self.post_message(TuiSubmit(text))

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def action_navigate_history_up(self) -> None:
        self.app.post_message(TuiHistoryNav(direction=-1))

    def action_navigate_history_down(self) -> None:
        self.app.post_message(TuiHistoryNav(direction=1))

    def on_key(self, event: Key) -> None:
        try:
            cl = self.app.query_one(CompletionList)
        except Exception:
            return
        if not cl.display:
            return
        if event.key == "escape":
            cl.display = False
            event.prevent_default()
        elif event.key == "tab":
            highlighted = cl.highlighted
            if highlighted is not None:
                opt = cl.get_option_at_index(highlighted)
                cmd = str(getattr(opt, "id", "") or "")
                if cmd:
                    self.clear()
                    self.insert(cmd)
            cl.display = False
            event.prevent_default()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        try:
            cl = self.app.query_one(CompletionList)
        except Exception:
            return
        if text.startswith("/") and "\n" not in text:
            self.app.post_message(TuiUpdateCompletion(text))
        else:
            cl.display = False


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


class CompletionList(OptionList):
    """Slash-command completion overlay."""


class StatusLine(Static):
    """1-line docked status bar."""


# ── Main Application ──────────────────────────────────────────────────────────

class MinionApp(App):
    """Full-screen Textual TUI for minion."""

    CSS = MINION_TCSS

    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit",     "Cancel/Quit", show=False),
        Binding("ctrl+l", "clear_conversation",  "Clear",       show=False),
        Binding("ctrl+o", "toggle_inspector",    "Inspector",   show=False),
        Binding("ctrl+e", "expand_inspector",    "Expand",      show=False),
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

        # Streaming-to-RichLog state
        self._pre_thinking_cp:             int  = 0
        self._streaming_lines_cp:          int  = 0
        self._streaming_started:           bool = False
        self._streaming_synced_text:       str  = ""
        self._thinking_written_to_richlog: bool = False

        self._on_submit: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_quit:   Optional[Callable[[], Awaitable[None]]]    = None
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
        self.status       = StatusBar(model_name=model_name, width=self._terminal_width)

        _reg = get_registry()
        _reg.set_post_message(self.post_message)
        self.inspector = InspectorPanel(registry=_reg)
        self.inspector.set_app(self)
        self.slots = SlotsManager(post_message_fn=self.post_message)

        # Wire conversation callbacks
        self.conversation.set_callbacks(
            write_ansi_fn=self._write_ansi,
            refresh_fn=self._refresh_streaming,
            pre_finalize_fn=self._pre_finalize_streaming,
        )

        # Widget references populated in on_mount()
        self._conv_area:          Optional[ConversationArea]  = None
        self._slots_zone:         Optional[SlotsZone]         = None
        self._inspector_zone:     Optional[InspectorZone]     = None
        self._input_section:      Optional[InputSection]      = None
        self._permission_content: Optional[PermissionContent] = None
        self._input_row:          Optional[InputRow]          = None
        self._completion_list:    Optional[CompletionList]    = None
        self._status_line:        Optional[StatusLine]        = None
        self._input_area:         Optional[InputArea]         = None

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
        yield SlotsZone(" ", id="slots-zone")
        yield InspectorZone(" ", id="inspector-zone")
        yield InputSection(id="input-section")
        yield CompletionList(id="completion-list")
        yield StatusLine("", id="status-line")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._conv_area          = self.query_one("#conv-area",          ConversationArea)
        self._slots_zone         = self.query_one("#slots-zone",         SlotsZone)
        self._inspector_zone     = self.query_one("#inspector-zone",     InspectorZone)
        self._input_section      = self.query_one("#input-section",      InputSection)
        self._permission_content = self.query_one("#permission-content", PermissionContent)
        self._input_row          = self.query_one("#input-row",          InputRow)
        self._completion_list    = self.query_one("#completion-list",    CompletionList)
        self._status_line        = self.query_one("#status-line",        StatusLine)
        self._input_area         = self.query_one("#input-area",         InputArea)

        import shutil as _shutil
        cols = _shutil.get_terminal_size().columns or 120
        self._terminal_width = cols
        self.conversation.set_width(cols)
        self.status.set_width(cols)

        # Initially hidden zones (PermissionContent starts hidden via CSS)
        self._slots_zone.display      = False
        self._inspector_zone.display  = False
        self._completion_list.display = False

        # Thinking animation timer (paused until first prompt)
        self._think_timer = self.set_interval(0.25, self._tick_thinking, pause=True)

        self._refresh_status()
        self._write_banner()
        self.set_focus(self._input_area)

    def on_resize(self, event) -> None:
        self._terminal_width = event.size.width
        self.conversation.set_width(event.size.width)
        self.status.set_width(event.size.width)
        self._refresh_status()

    # ── run_async override ────────────────────────────────────────────────────

    async def run_async(  # type: ignore[override]
        self,
        *,
        on_submit: Optional[Callable[[str], Awaitable[None]]] = None,
        on_quit:   Optional[Callable[[], Awaitable[None]]]    = None,
        **kwargs,
    ) -> None:
        if on_submit is not None:
            self._on_submit = on_submit
        if on_quit is not None:
            self._on_quit = on_quit
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
        if self._conv_area is not None:
            self._conv_area.clear_log()

    def action_toggle_inspector(self) -> None:
        if self.permission.is_visible:
            return
        self.inspector.toggle()
        self.status.set_inspector_hint(
            self.inspector.hint() if self.inspector.is_visible else ""
        )
        self._refresh_inspector()
        self._refresh_status()

    def action_expand_inspector(self) -> None:
        if not self.inspector.is_visible:
            return
        self.inspector.toggle_expanded()
        self.status.set_inspector_hint(self.inspector.hint())
        self._refresh_inspector()
        self._refresh_status()

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

        if self.inspector.is_visible:
            handled = True
            k = event.key
            if k == "left":
                self.inspector.move_agent(-1)
                self.status.set_inspector_hint(self.inspector.hint())
                self._refresh_inspector()
                self._refresh_status()
            elif k == "right":
                self.inspector.move_agent(1)
                self.status.set_inspector_hint(self.inspector.hint())
                self._refresh_inspector()
                self._refresh_status()
            elif k == "up":
                self.inspector.scroll(-1)
                self._refresh_inspector()
            elif k == "down":
                self.inspector.scroll(1)
                self._refresh_inspector()
            elif k == "escape":
                self.inspector.close()
                self.status.set_inspector_hint("")
                self._refresh_inspector()
                self._refresh_status()
            else:
                handled = False
            if handled:
                event.prevent_default()

    # ── Message handlers ──────────────────────────────────────────────────────

    def on_slots_updated(self, _: SlotsUpdated) -> None:
        if self._slots_zone is None:
            return
        self._slots_zone.update(self.slots.get_rich_text())
        self._slots_zone.display = self.slots.is_visible

    def on_inspector_updated(self, _: InspectorUpdated) -> None:
        self._refresh_inspector()

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
        self._input_area.clear()
        if new_idx == -1:
            self._input_area.insert(self._hist_saved)
        else:
            self._input_area.insert(self._history[-(new_idx + 1)])

    def on_tui_update_completion(self, message: TuiUpdateCompletion) -> None:
        if self._completion_list is None:
            return
        prefix = message.prefix.lower()
        from ..repl import REPL_COMMANDS as _CMDS
        matches: list[str] = [cmd for cmd in _CMDS if cmd.startswith(prefix)][:10]
        self._completion_list.clear_options()
        if matches:
            for cmd in matches:
                desc = _CMDS.get(cmd, "")
                display = f"{cmd}  [dim]{desc}[/dim]" if desc else cmd
                self._completion_list.add_option(Option(display, id=cmd))
            self._completion_list.display = True
        else:
            self._completion_list.display = False

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
                self.set_focus(self._input_area)

    # ── Thinking animation ────────────────────────────────────────────────────

    def _tick_thinking(self) -> None:
        """Animate the thinking indicator in ConversationLog (runs on event loop)."""
        if self._streaming_started:
            return  # streaming phase owns the RichLog from here on
        if not self.conversation._is_thinking:
            return
        if self._conv_area is None:
            return
        try:
            rl = self._conv_area.query_one(ConversationLog)
            markup = self.conversation.get_streaming_markup()
            rl.pop_to(self._pre_thinking_cp)
            rl.write(Text.from_markup(markup))
        except Exception:
            pass

    def _set_thinking(self, thinking: bool) -> None:
        self._thinking = thinking
        self.conversation.set_thinking(thinking)
        self.status.set_thinking(thinking)
        if thinking:
            if self._conv_area is not None:
                try:
                    rl = self._conv_area.query_one(ConversationLog)
                    self._pre_thinking_cp             = rl.checkpoint()
                    self._streaming_started           = False
                    self._streaming_synced_text       = ""
                    self._streaming_lines_cp          = 0
                    self._thinking_written_to_richlog = True
                    rl.write(Text.from_markup(self.conversation.get_streaming_markup()))
                except Exception:
                    pass
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

    # ── Write paths ───────────────────────────────────────────────────────────

    def _write_ansi(self, ansi: str) -> None:
        """Write ANSI content to the RichLog. Thread-safe."""
        if self._conv_area is None:
            sys.stdout.write(ansi)
            sys.stdout.flush()
            return
        try:
            asyncio.get_running_loop()
            # On the event loop — direct call
            self._conv_area.write_ansi(ansi)
        except RuntimeError:
            # Worker thread
            self.call_from_thread(self._conv_area.write_ansi, ansi)

    def _print_ansi_to_scrollback(self, ansi: str) -> None:
        """External path: marks conversation.mark_printed() then writes."""
        self.conversation.mark_printed()
        self._write_ansi(ansi)

    def _write_markup(self, markup: str) -> None:
        """Render Rich markup to ANSI and write directly to RichLog (no mark_printed)."""
        from rich.console import Console as _RC
        buf = io.StringIO()
        c = _RC(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            width=self._terminal_width,
            highlight=False,
        )
        c.print(markup)
        self._write_ansi(buf.getvalue())

    # ── Refresh helpers ───────────────────────────────────────────────────────

    def _refresh_streaming(self) -> None:
        """Sync in-progress streaming text into ConversationLog (thinking+streaming)."""
        if not self.conversation._is_streaming:
            return
        if self._conv_area is None:
            return

        def _do_sync() -> None:
            try:
                rl = self._conv_area.query_one(ConversationLog)  # type: ignore[union-attr]
            except Exception:
                return

            if not self._streaming_started:
                # First chunk: pop thinking indicator and record start point
                if self._thinking_written_to_richlog:
                    rl.pop_to(self._pre_thinking_cp)
                self._streaming_started     = True
                self._streaming_synced_text = ""
                self._streaming_lines_cp    = rl.checkpoint()

            with self.conversation._lock:
                full_text = self.conversation._streaming_text

            new_text = full_text[len(self._streaming_synced_text):]

            if "\n" in new_text:
                new_lines = new_text.split("\n")
                # Pop previous partial line
                rl.pop_to(self._streaming_lines_cp)
                # Write all newly completed lines
                for line in new_lines[:-1]:
                    rl.write(Text(line), expand=True)
                self._streaming_synced_text = full_text[: len(full_text) - len(new_lines[-1])]
                self._streaming_lines_cp    = rl.checkpoint()
                partial = new_lines[-1]
            else:
                # No new complete lines — just refresh the partial line
                rl.pop_to(self._streaming_lines_cp)
                partial = full_text[len(self._streaming_synced_text):]

            if partial:
                rl.write(Text("▌ minion › " + partial, style="bold #1E90FF"), expand=True)

        try:
            asyncio.get_running_loop()
            _do_sync()
        except RuntimeError:
            self.call_from_thread(_do_sync)

    def _pre_finalize_streaming(self) -> None:
        """Pop all thinking/streaming lines from ConversationLog before final render."""
        if not self._thinking_written_to_richlog:
            return

        def _do_pop() -> None:
            if self._conv_area is None:
                return
            try:
                rl = self._conv_area.query_one(ConversationLog)
                rl.pop_to(self._pre_thinking_cp)
            except Exception:
                pass
            self._thinking_written_to_richlog = False
            self._streaming_started           = False
            self._streaming_synced_text       = ""
            self._streaming_lines_cp          = 0

        try:
            asyncio.get_running_loop()
            _do_pop()
        except RuntimeError:
            self.call_from_thread(_do_pop)

    def _refresh_status(self) -> None:
        if self._status_line is None:
            return
        self._status_line.update(self.status.get_rich_markup())

    def _refresh_inspector(self) -> None:
        if self._inspector_zone is None:
            return
        if self.inspector.is_visible:
            self._inspector_zone.update(self.inspector.get_rich_text())
            self._inspector_zone.display = True
        else:
            self._inspector_zone.display = False

    def _refresh_permission(self) -> None:
        if self._permission_content is not None:
            self._permission_content.update(self.permission.get_rich_markup())

    # ── Permission show/hide ──────────────────────────────────────────────────

    def show_permission(self) -> None:
        markup = self.permission.get_rich_markup()
        if self._permission_content is not None:
            self._permission_content.update(markup)
            self._permission_content.display = True
        if self._input_row is not None:
            self._input_row.display = False
        if self._input_section is not None:
            self._input_section.add_class("permission-active")

    def hide_permission(self) -> None:
        from .permission import _DIFF_TOOLS as _DT
        # Write the diff to the conversation log so the user can scroll through it.
        # The tool call header (⊙ write_file) is already rendered by TuiRenderer;
        # we only add the diff content — no redundant header here.
        if (self.permission._last_result
                and self.permission._last_diff
                and self.permission._last_name in _DT):
            self._write_markup(self.permission._last_diff)

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
        pass  # RichLog auto-scrolls

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
        """Render any Rich renderable and write it to the RichLog."""
        from rich.console import Console as _RC
        try:
            from ..theme import MINION_THEME as _THEME
            theme_kwarg: dict = {"theme": _THEME}
        except Exception:
            theme_kwarg = {}
        buf = io.StringIO()
        c = _RC(
            file=buf,
            force_terminal=True,
            color_system="truecolor",
            width=self._terminal_width,
            highlight=False,
            **theme_kwarg,
        )
        c.print(renderable)
        self._print_ansi_to_scrollback(buf.getvalue())

    # ── Banner ────────────────────────────────────────────────────────────────

    def _write_banner(self) -> None:
        """Write the greeting banner to the RichLog on startup."""
        try:
            from rich.rule import Rule as _Rule
            from rich.text import Text as _Text
            from ..theme.banner import get_greeting_renderables as _get_renderables
            from ..theme.palette import SILVER as _SILVER

            if self._conv_area is None:
                return
            rl = self._conv_area.query_one(ConversationLog)

            # expand=True is essential: without it, Table.grid renders at minimum
            # measured width instead of filling the content area. expand=True is
            # stored in the DeferredRender queue if _size_known is still False
            # (writes in on_mount are deferred until the RichLog's first Resize),
            # so the correct width is used whenever the size becomes available.
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
                rl.write(r, expand=True)

            # Startup warnings (MCP connection errors, MINION.md tip, etc.)
            if self._startup_warnings:
                for w in self._startup_warnings:
                    rl.write(w, expand=True)
                rl.write("", expand=True)
                rl.write(_Rule(style=_SILVER), expand=True)
                rl.write("", expand=True)
        except Exception:
            pass
