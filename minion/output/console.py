"""ConsoleRenderer — Rich + questionary output for non-TUI mode."""

from __future__ import annotations

import contextlib
import sys
from typing import Any, Optional

from .base import OutputRenderer


class ConsoleRenderer(OutputRenderer):
    """Routes all output to the Rich console and stdout.

    Preserves exactly the same display behaviour as the pre-renderer code
    in runner.py and executor.py for the non-TUI path.
    """

    def __init__(self) -> None:
        # Streaming state — reset at on_assistant_start / on_assistant_end
        self._silent: bool = False
        self._stream_markdown: bool = False
        self._display_name: str = "minion"
        self._printed_prefix: bool = False
        self._had_tool_newline: bool = False
        self._tool_spinner = None
        self._md_streamer = None

    # ── Streaming assistant turn ──────────────────────────────────────────────

    def on_assistant_start(
        self,
        *,
        display_name: str = "minion",
        stream_markdown: bool = False,
        silent: bool = False,
    ) -> None:
        from ..theme import BLUE, console, MarkdownStreamer
        self._silent = silent
        self._stream_markdown = stream_markdown
        self._display_name = display_name
        self._printed_prefix = True
        self._had_tool_newline = False
        if silent:
            return
        if stream_markdown:
            self._md_streamer = MarkdownStreamer(display_name=display_name)
            self._md_streamer.__enter__()
        else:
            console.print(f"[bold {BLUE}]{display_name}[/] › ", end="")

    def on_assistant_chunk(self, text: str) -> None:
        if self._silent:
            return
        if self._stream_markdown and self._md_streamer is not None:
            self._md_streamer.write(text)
        else:
            sys.stdout.write(text)
            sys.stdout.flush()

    def on_tool_accumulation_start(self, tool_name: str) -> None:
        from ..theme import console
        from ..tools.executor import TOOL_SPINNER_LABELS
        if self._stream_markdown and self._md_streamer is not None:
            self._md_streamer.close()
            self._md_streamer = None
        else:
            print()  # end the current text line
        self._had_tool_newline = True
        self._tool_spinner = console.status(
            TOOL_SPINNER_LABELS.get(tool_name, "[muted]thinking...[/]"),
            spinner="dots",
        )
        self._tool_spinner.start()

    def on_tool_use_block_received(self) -> None:
        if self._tool_spinner is not None:
            self._tool_spinner.stop()
            self._tool_spinner = None

    def on_narration_flush(self, text: str, *, display_name: str = "minion") -> None:
        from ..theme import BLUE, console
        console.print(f"[bold {BLUE}]{display_name}[/] › ", end="")
        sys.stdout.write(text)
        print()

    def on_assistant_end(self) -> None:
        # Stop any lingering spinner (StreamComplete without prior ToolUseBlock)
        if self._tool_spinner is not None:
            self._tool_spinner.stop()
            self._tool_spinner = None
        if self._stream_markdown and self._md_streamer is not None:
            self._md_streamer.close()
            self._md_streamer = None
        elif self._printed_prefix and not self._had_tool_newline and not self._silent:
            print()
        # Reset state for next turn
        self._printed_prefix = False
        self._had_tool_newline = False
        self._silent = False
        self._stream_markdown = False

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
        from ..theme import print_tool_call
        print_tool_call(name, inputs, dry_run=dry_run, agent_label=agent_label, mode_badge=mode_badge)

    def on_tool_result(self, result: str) -> None:
        from ..theme import print_tool_result
        print_tool_result(result)

    def on_tool_error(self, error: str) -> None:
        from ..theme import print_tool_error
        print_tool_error(error)

    def on_diff_preview(self, detail: str, *, tool_name: str = "") -> None:
        from ..theme import console
        if tool_name in ("write_file", "edit_file"):
            console.print(detail)
        else:
            console.print(f"[muted]{detail}[/]")

    def on_todo_list(self, *, show_if_all_done: bool = False) -> None:
        from ..theme import print_todo_list
        print_todo_list(show_if_all_done=show_if_all_done)

    # ── System messages ───────────────────────────────────────────────────────

    def on_info(self, message: str) -> None:
        from ..theme import console
        if message:
            console.print(message)
        else:
            print()  # blank line separator

    def on_error(self, message: str) -> None:
        from ..theme import print_error
        print_error(message)

    def on_cancellation(self) -> None:
        from ..theme import YELLOW, console
        console.print(f"\n[{YELLOW}]⚠ Cancelled.[/]\n")

    def on_stop_reason(self, reason: str) -> None:
        from ..theme import console
        console.print(f"\n[muted]  ↳ stopped: {reason}[/]")

    # ── Progress ──────────────────────────────────────────────────────────────

    def spinner(self, label: str) -> contextlib.AbstractContextManager:
        from ..theme import console
        return console.status(label, spinner="dots")

    # ── Rich output ───────────────────────────────────────────────────────────

    def on_markdown_panel(self, text: str, title: Optional[str] = None) -> None:
        from rich.markdown import Markdown
        from rich.panel import Panel
        from ..theme import YELLOW, console
        panel = Panel(
            Markdown(text),
            title=f"[bold {YELLOW}]{title or 'Response'}[/]",
            expand=False,
            border_style="dim",
        )
        console.print(panel)

    # ── Session / metadata ────────────────────────────────────────────────────

    def on_iteration_limit(self, limit: int) -> None:
        from ..theme import print_iteration_limit
        print_iteration_limit(limit)

    def on_session_summary(
        self,
        snapshot: Any,
        *,
        approval_mode: Optional[str] = None,
    ) -> None:
        from ..theme import print_todo_list, print_usage
        print_todo_list()
        print_usage(snapshot, active_mode=approval_mode if approval_mode != "off" else None)

    def on_subagent_tokens(self, count: int, total: int) -> None:
        from ..theme import console
        console.print(
            f"  [muted]subagents: {count} agent{'s' if count > 1 else ''}, "
            f"{total:,} tokens total[/]"
        )

    # ── Parallel display ──────────────────────────────────────────────────────

    @property
    def parallel_display(self) -> Any:
        return None  # Caller creates a fresh AgentLiveDisplay each time
