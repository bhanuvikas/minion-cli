"""MinionApp — the prompt_toolkit Application that owns the terminal in TUI mode.

Layout (bottom strip, always visible):
  streaming_zone  — live in-progress assistant response (hidden when idle)
  slots_zone      — live agent status (hidden when no agents running)
  separator
  bottom_zone     — switches between input_bar and permission_panel
  separator
  status_bar      — 1-line model/mode indicator

All completed output (user turns, finished LLM responses, tool results, system
messages) is printed to the real terminal via run_in_terminal(), landing in the
terminal scrollback buffer — giving natural terminal-like scrolling where content
flows up as the conversation grows, exactly like Claude Code.

Non-TTY or MINION_NO_TUI=1: this module is not used; the existing
PromptSession + Rich Live + questionary path remains active.
"""

from __future__ import annotations

import asyncio
import io
import sys
from typing import Awaitable, Callable, Optional

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.lexers import Lexer

from .conversation import ConversationBuffer
from .permission import PermissionPanel
from .slots import SlotsManager
from .status import StatusBar
from .theme import TUI_STYLE


class MinionApp:
    """Full-screen TUI Application for minion.

    Create once per session, pass on_submit and on_quit at run time via
    run_async(). All component state (conversation, slots, permission, status)
    lives here so it can be shared with runner.py and confirmation.py.
    """

    def __init__(self, model_name: str, completer: Optional[Completer] = None) -> None:
        self._model_name = model_name
        self._thinking   = False
        self._terminal_width = 120
        self._completer  = completer

        # Components
        self.conversation = ConversationBuffer()
        self.slots        = SlotsManager(invalidate_fn=self._invalidate)
        self.permission   = PermissionPanel(app_ref=self)
        self.status       = StatusBar(model_name=model_name, width=self._terminal_width)

        # Wire conversation callbacks now (before _build).
        # Use the *internal* write path so ConversationBuffer's own emits do NOT
        # trigger mark_printed() — only external print_renderable() calls do.
        self.conversation.set_callbacks(
            print_ansi_fn=self._write_ansi,
            invalidate_fn=self._invalidate,
        )

        # Callbacks set by run_async()
        self._on_submit: Optional[Callable[[str], Awaitable[None]]] = None
        self._on_quit:   Optional[Callable[[], Awaitable[None]]]    = None

        # Background task tracking
        self._current_task: Optional[asyncio.Task] = None

        # The Application — built lazily so the event loop is available
        self._app: Optional[Application] = None

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> Application:
        kb = KeyBindings()

        # ── Condition filters ─────────────────────────────────────────────────
        is_perm_visible = Condition(lambda: self.permission.is_visible)
        is_thinking     = Condition(lambda: self._thinking)
        is_input_active = ~is_perm_visible & ~is_thinking

        # ── Key bindings ──────────────────────────────────────────────────────

        @kb.add("enter", filter=is_input_active)
        async def _on_enter(event):
            text = event.app.current_buffer.text
            stripped = text.strip()
            if not stripped:
                return
            event.app.current_buffer.append_to_history()
            event.app.current_buffer.reset()
            self.conversation.append_user(stripped)
            self._set_thinking(True)
            if self._on_submit is not None:
                self._current_task = asyncio.create_task(
                    _wrap_submit(self._on_submit, stripped, self)
                )

        @kb.add("escape", "enter", filter=is_input_active)
        def _insert_nl(event):
            event.app.current_buffer.insert_text("\n")

        @kb.add("c-j", filter=is_input_active)
        def _paste_nl(event):
            event.app.current_buffer.insert_text("\n")

        @kb.add("c-c")
        async def _on_ctrl_c(event):
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
            else:
                if self._on_quit is not None:
                    await self._on_quit()
                event.app.exit()

        @kb.add("c-l")
        def _clear_conv(event):
            self.conversation.clear()
            event.app.invalidate()

        # Permission panel keys
        @kb.add("1", filter=is_perm_visible)
        def _perm1(event): self.permission.confirm_by_index(0); event.app.invalidate()

        @kb.add("2", filter=is_perm_visible)
        def _perm2(event): self.permission.confirm_by_index(1); event.app.invalidate()

        @kb.add("3", filter=is_perm_visible)
        def _perm3(event): self.permission.confirm_by_index(2); event.app.invalidate()

        @kb.add("4", filter=is_perm_visible)
        @kb.add("n",       filter=is_perm_visible)
        @kb.add("escape",  filter=is_perm_visible)
        def _perm_no(event): self.permission.deny(); event.app.invalidate()

        @kb.add("up",   filter=is_perm_visible)
        def _perm_up(event):
            self.permission.move_cursor(-1)
            event.app.invalidate()

        @kb.add("down", filter=is_perm_visible)
        def _perm_down(event):
            self.permission.move_cursor(1)
            event.app.invalidate()

        @kb.add("enter", filter=is_perm_visible)
        def _perm_enter(event):
            self.permission.confirm_current()
            event.app.invalidate()

        # ── Input buffer ──────────────────────────────────────────────────────
        history_path = __import__("pathlib").Path.home() / ".minion" / "history"
        history_path.parent.mkdir(exist_ok=True)

        input_buf = Buffer(
            name="input",
            history=FileHistory(str(history_path)),
            multiline=True,
            completer=self._completer,
            accept_handler=None,  # handled by Enter key above
        )

        # ── Layout zones ──────────────────────────────────────────────────────

        # Streaming zone: shows the in-progress assistant response.
        # Visible only while the assistant is actively generating.
        # When the turn finalises, the complete response is committed to the
        # terminal scrollback and this zone clears automatically.
        streaming_window = Window(
            content=FormattedTextControl(
                lambda: self.conversation.get_streaming_formatted_text(),
                focusable=False,
            ),
            wrap_lines=True,
            dont_extend_height=True,
        )
        streaming_zone = ConditionalContainer(
            content=streaming_window,
            filter=Condition(lambda: self.conversation.is_streaming),
        )

        slots_window = Window(
            content=FormattedTextControl(
                lambda: self.slots.get_formatted_text(),
                focusable=False,
            ),
            wrap_lines=True,
            dont_extend_height=True,
        )

        slots_zone = ConditionalContainer(
            content=slots_window,
            filter=Condition(lambda: self.slots.is_visible),
        )

        status_window = Window(
            content=FormattedTextControl(
                lambda: self.status.get_formatted_text(),
                focusable=False,
            ),
            height=1,
        )

        import re as _re
        from ..repl import REPL_COMMANDS as _REPL_COMMANDS
        _token_re = _re.compile(r"@[\w./\-]+|/\S+")

        class _TuiInputLexer(Lexer):
            def lex_document(self, document):
                lines = document.text.split("\n")
                def get_line(lineno):
                    if lineno >= len(lines):
                        return []
                    line = lines[lineno]
                    tokens = []
                    cursor = 0
                    for m in _token_re.finditer(line):
                        if m.start() > cursor:
                            tokens.append(("", line[cursor:m.start()]))
                        text = m.group()
                        if text.startswith("@"):
                            tokens.append(("class:at-mention", text))
                        elif text.lower() in _REPL_COMMANDS:
                            tokens.append(("class:slash-command", text))
                        else:
                            tokens.append(("", text))
                        cursor = m.end()
                    if cursor < len(line):
                        tokens.append(("", line[cursor:]))
                    return tokens
                return get_line

        input_window = Window(
            content=BufferControl(
                buffer=input_buf,
                lexer=_TuiInputLexer(),
                input_processors=[BeforeInput([("class:input-prefix", "you › ")])],
                focusable=True,
            ),
            wrap_lines=True,
            dont_extend_height=True,
        )

        permission_window = Window(
            content=FormattedTextControl(
                lambda: self.permission.get_formatted_text(),
                focusable=False,
            ),
            wrap_lines=True,
            dont_extend_height=True,
        )

        bottom_zone = ConditionalContainer(
            content=HSplit([
                ConditionalContainer(
                    content=input_window,
                    filter=~is_perm_visible,
                ),
                ConditionalContainer(
                    content=permission_window,
                    filter=is_perm_visible,
                ),
            ]),
            filter=~Condition(lambda: False),  # always visible
        )

        layout = Layout(
            FloatContainer(
                content=HSplit([
                    streaming_zone,
                    slots_zone,
                    Window(height=1, char="─", style="class:separator"),
                    bottom_zone,
                    Window(height=1, char="─", style="class:separator"),
                    status_window,
                ]),
                floats=[
                    Float(
                        xcursor=True,
                        ycursor=True,
                        content=CompletionsMenu(max_height=8, scroll_offset=2),
                    ),
                ],
            ),
            focused_element=input_buf,
        )

        return Application(
            layout=layout,
            key_bindings=kb,
            style=TUI_STYLE,
            full_screen=False,
            mouse_support=False,
        )

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run_async(
        self,
        *,
        on_submit: Callable[[str], Awaitable[None]],
        on_quit:   Callable[[], Awaitable[None]],
    ) -> None:
        """Run the Application until the user exits."""
        self._on_submit = on_submit
        self._on_quit   = on_quit
        self._app       = self._build()
        await self._app.run_async()

    # ── Scrollback write paths ────────────────────────────────────────────────

    def _write_ansi(self, ansi: str) -> None:
        """Internal path: write ANSI to the terminal scrollback.

        Used exclusively by ConversationBuffer via the print_ansi_fn callback.
        Does NOT call mark_printed() — ConversationBuffer's own emits must not
        set the external-print flag.
        """
        if not ansi.endswith("\n"):
            ansi = ansi + "\n"
        if self._app is None or not self._app.is_running:
            sys.stdout.write(ansi)
            sys.stdout.flush()
            return
        from prompt_toolkit.application import run_in_terminal
        run_in_terminal(lambda: (sys.stdout.write(ansi), sys.stdout.flush()))

    def _print_ansi_to_scrollback(self, ansi: str) -> None:
        """External path: write ANSI from print_renderable() to the scrollback.

        Calls conversation.mark_printed() so finalize_turn() knows that tool
        calls / hooks / other content appeared since start_assistant_turn().
        """
        self.conversation.mark_printed()
        self._write_ansi(ansi)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _invalidate(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    def scroll_to_bottom(self) -> None:
        """No-op: terminal handles its own scroll."""
        self._invalidate()

    def start_spinner(self) -> None:
        """No-op: streaming zone updates via stream_chunk → invalidate."""

    def stop_spinner_if_idle(self) -> None:
        """No-op: no spinner loop needed."""

    def invalidate(self) -> None:
        self._invalidate()

    def _set_thinking(self, thinking: bool) -> None:
        self._thinking = thinking
        self.status.set_thinking(thinking)
        self._invalidate()

    def set_thinking(self, thinking: bool) -> None:
        self._set_thinking(thinking)

    def update_session(self, **kwargs) -> None:
        """Forward session info (model, provider, project, memory, agents) to StatusBar."""
        self.status.update_session(**kwargs)
        self._invalidate()

    def print_to_terminal(self, rich_text: str) -> None:
        """Print Rich-markup text to the terminal scrollback."""
        self.print_renderable(rich_text)

    def print_renderable(self, renderable) -> None:
        """Print any Rich renderable (markup, Panel, Markdown, etc.) to the scrollback.

        Routes through _print_ansi_to_scrollback (the *external* path) so that
        conversation.mark_printed() is called, enabling finalize_turn() to insert
        a blank line before the assistant response when tool calls preceded it.
        """
        from rich.console import Console as _RC
        from ..theme import MINION_THEME as _THEME
        buf = io.StringIO()
        c   = _RC(file=buf, force_terminal=True, color_system="truecolor",
                  width=self._terminal_width, highlight=False, theme=_THEME)
        c.print(renderable)
        ansi = buf.getvalue()
        self._print_ansi_to_scrollback(ansi)


# ── Internal helper ───────────────────────────────────────────────────────────

async def _wrap_submit(
    on_submit: Callable[[str], Awaitable[None]],
    text: str,
    tui_app: MinionApp,
) -> None:
    """Run on_submit() and ensure thinking state is cleared on completion."""
    try:
        await on_submit(text)
    except asyncio.CancelledError:
        tui_app.conversation.append_system("[#C0C0C0]⚠ Cancelled.[/]")
        tui_app.conversation.finalize_turn()
    except Exception as exc:
        tui_app.conversation.append_system(f"[red]Error: {exc}[/]")
    finally:
        tui_app.set_thinking(False)
        tui_app.scroll_to_bottom()
