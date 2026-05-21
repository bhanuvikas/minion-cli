"""TuiRenderer — routes output to the Textual MinionApp."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Optional

from .base import OutputRenderer

if TYPE_CHECKING:
    from ..tui.app import MinionApp


class TuiRenderer(OutputRenderer):
    """Routes all output to the TUI conversation buffer and slots zone.

    Wraps MinionApp and delegates to conversation.*, slots.*, and
    print_renderable() as appropriate.
    """

    def __init__(self, app: "MinionApp") -> None:
        self._app = app
        self._printed_prefix: bool = False
        self._silent_render: bool = False
        self._display_name: str = "minion"

    # ── Streaming assistant turn ──────────────────────────────────────────────

    def on_assistant_start(
        self,
        *,
        display_name: str = "minion",
        stream_markdown: bool = False,
        silent: bool = False,
    ) -> None:
        # Stream always starts so the user sees live output; panel replaces it on end.
        self._silent_render = silent
        self._display_name  = display_name
        self._app.conversation.start_assistant_turn(display_name=display_name)
        self._printed_prefix = True

    def on_assistant_chunk(self, text: str) -> None:
        self._app.conversation.stream_chunk(text)
        self._app.invalidate()

    def on_tool_accumulation_start(self, tool_name: str) -> None:
        pass  # TUI streaming zone continues — no spinner or newline needed

    def on_tool_use_block_received(self) -> None:
        pass  # no-op — TUI has no tool-accumulation spinner to stop

    def on_narration_flush(self, text: str, *, display_name: str = "minion") -> None:
        # LLM emitted narration before a tool call — commit it so the user sees it.
        # on_assistant_end() will then find an empty streaming zone and abandon it.
        if self._silent_render:
            self._app.conversation.finalize_turn()
            self._silent_render = False

    def on_assistant_end(self) -> None:
        # Guard prevents double-finalize if on_cancellation() already closed the turn
        if self._printed_prefix:
            if self._silent_render:
                self._app.conversation.abandon_streaming_turn()
            else:
                self._app.conversation.finalize_turn()
            self._printed_prefix = False
            self._silent_render = False

    # ── Tool call display ─────────────────────────────────────────────────────

    def on_tool_call(
        self,
        name: str,
        inputs: dict[str, Any],
        *,
        dry_run: bool = False,
        agent_label: Optional[str] = None,
        mode_badge: Optional[str] = None,
    ) -> None:
        from .formatter import format_tool_call
        self._app.conversation.append_system(
            format_tool_call(name, inputs, dry_run=dry_run, agent_label=agent_label, mode_badge=mode_badge)
        )
        self._app.invalidate()

    def on_tool_result(self, result: str, latency_ms: int = 0) -> None:
        from .formatter import format_tool_result
        from ..theme import GREEN as _GREEN
        from rich.text import Text
        is_success = (
            latency_ms > 0
            and not result.startswith("Error:")
            and result != "User declined tool execution."
        )
        if is_success:
            done = Text()
            done.append(f"   ✓  done ({latency_ms / 1000:.1f}s)", style=f"bold {_GREEN}")
            self._app.conversation.append_block(done)
        self._app.conversation.append_system(format_tool_result(result))
        self._app.invalidate()

    def on_tool_error(self, error: str) -> None:
        from .formatter import format_tool_error
        self._app.conversation.append_system(format_tool_error(error))
        self._app.invalidate()

    def on_diff_preview(self, detail: str, *, tool_name: str = "") -> None:
        # Indent to align with tool result lines (which start with 3 spaces)
        indented = "   " + detail.rstrip("\n").replace("\n", "\n   ")
        self._app.conversation.append_system(indented)
        self._app.invalidate()

    def on_todo_list(self, *, show_if_all_done: bool = False) -> None:
        from .formatter import format_todo_list
        markup = format_todo_list(show_if_all_done=show_if_all_done)
        if markup:
            self._app.conversation.append_system(markup)
            self._app.invalidate()

    # ── System messages ───────────────────────────────────────────────────────

    def on_info(self, message: str) -> None:
        if not message:
            self._app.conversation.emit_spacer()
        else:
            self._app.conversation.append_system(message)
        self._app.invalidate()

    def on_error(self, message: str) -> None:
        self._app.conversation.append_system(f"[red]{message}[/]")
        self._app.invalidate()

    def on_cancellation(self) -> None:
        self._app.conversation.append_system("[#C0C0C0]⚠ Cancelled.[/]")
        self._app.conversation.finalize_turn()
        self._app.invalidate()

    def on_stop_reason(self, reason: str) -> None:
        self._app.conversation.append_system(f"[muted]  ↳ stopped: {reason}[/]")
        self._app.invalidate()

    # ── Progress ──────────────────────────────────────────────────────────────

    def spinner(self, label: str) -> contextlib.AbstractContextManager:
        # TUI's own thinking indicator makes a Rich console spinner redundant and disruptive
        return contextlib.nullcontext()  # streaming zone serves this purpose

    # ── Rich output ───────────────────────────────────────────────────────────

    def on_markdown_panel(self, text: str, title: Optional[str] = None) -> None:
        from rich.markdown import Markdown
        from rich.panel import Panel
        from rich.text import Text
        from ..theme import YELLOW
        prefix = Text()
        prefix.append(f"▌ {self._display_name} ›", style="bold #1E90FF")
        self._app.conversation.append_block(prefix)
        panel = Panel(
            Markdown(text),
            title=f"[bold {YELLOW}]{title or 'Response'}[/]",
            expand=False,
            border_style="dim",
        )
        self._app.print_renderable(panel)

    # ── Session / metadata ────────────────────────────────────────────────────

    def on_iteration_limit(self, limit: int) -> None:
        from ..theme import YELLOW
        self._app.conversation.append_system(
            f"[{YELLOW}]⚠ Maximum iterations ({limit}) reached.[/]"
        )
        self._app.invalidate()

    def on_session_summary(
        self,
        snapshot: Any,
        *,
        approval_mode: Optional[str] = None,
    ) -> None:
        pass  # TUI shows live stats in the status bar; no end-of-turn wall of text

    def on_subagent_tokens(self, count: int, total: int) -> None:
        self._app.conversation.append_system(
            f"  [muted]subagents: {count} agent{'s' if count > 1 else ''}, "
            f"{total:,} tokens total[/]"
        )
        self._app.invalidate()

    # ── Parallel display ──────────────────────────────────────────────────────

    @property
    def parallel_display(self) -> Any:
        # Reuse the shared SlotsManager instead of creating a new Rich Live display;
        # needs_scrollback_flush=True so caller commits slot results to the conversation buffer
        return self._app.slots  # SlotsManager — no Rich Live needed
