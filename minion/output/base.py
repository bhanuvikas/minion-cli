"""OutputRenderer — abstract base class for all display output.

Core logic (runner.py, executor.py) calls these methods.
ConsoleRenderer and TuiRenderer implement them.
Neither the caller nor the interface knows which mode is active.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from typing import Any, Optional


class OutputRenderer(ABC):

    # ── Streaming assistant turn ──────────────────────────────────────────────

    @abstractmethod
    def on_assistant_start(
        self,
        *,
        display_name: str = "minion",
        stream_markdown: bool = False,
        silent: bool = False,
    ) -> None:
        """First text chunk arrived — set up prefix/streaming zone."""

    @abstractmethod
    def on_assistant_chunk(self, text: str) -> None:
        """A text chunk arrived from the LLM stream."""

    @abstractmethod
    def on_tool_accumulation_start(self, tool_name: str) -> None:
        """Model stopped generating text; tool JSON is streaming.

        ConsoleRenderer: closes any markdown streamer, prints newline, starts spinner.
        TuiRenderer: no-op (streaming zone continues showing in-progress text).
        """

    @abstractmethod
    def on_tool_use_block_received(self) -> None:
        """A ToolUseBlock was fully parsed — stop any pending tool-accumulation spinner."""

    @abstractmethod
    def on_narration_flush(self, text: str, *, display_name: str = "minion") -> None:
        """Flush narration text in silent mode when stop_reason==tool_use.

        ConsoleRenderer: print prefix + text.
        TuiRenderer: no-op (text is already in the conversation buffer).
        """

    @abstractmethod
    def on_assistant_end(self) -> None:
        """Streaming is done — commit the turn.

        ConsoleRenderer: close MarkdownStreamer or print trailing newline.
        TuiRenderer: call conversation.finalize_turn().
        """

    # ── Tool call display ─────────────────────────────────────────────────────

    @abstractmethod
    def on_tool_call(
        self,
        name: str,
        inputs: dict[str, Any],
        *,
        dry_run: bool = False,
        agent_label: Optional[str] = None,
        mode_badge: Optional[str] = None,
    ) -> None:
        """A tool call is about to be executed — show the call."""

    @abstractmethod
    def on_tool_result(self, result: str) -> None:
        """A tool call completed successfully — show the result."""

    @abstractmethod
    def on_tool_error(self, error: str) -> None:
        """A tool call failed — show the error."""

    @abstractmethod
    def on_diff_preview(self, detail: str, *, tool_name: str = "") -> None:
        """Show a file diff before a write/edit tool executes."""

    @abstractmethod
    def on_todo_list(self, *, show_if_all_done: bool = False) -> None:
        """Show the current todo list (called after todo_write)."""

    # ── System messages ───────────────────────────────────────────────────────

    @abstractmethod
    def on_info(self, message: str) -> None:
        """Informational Rich-markup message (rate-limit notice, compact status, etc.)."""

    @abstractmethod
    def on_error(self, message: str) -> None:
        """Error message from the system (LLM error, rate limit, etc.)."""

    @abstractmethod
    def on_cancellation(self) -> None:
        """User cancelled the current operation."""

    @abstractmethod
    def on_stop_reason(self, reason: str) -> None:
        """LLM stopped for a non-standard reason (max_tokens, etc.)."""

    # ── Progress ──────────────────────────────────────────────────────────────

    @abstractmethod
    def spinner(self, label: str) -> contextlib.AbstractContextManager:
        """Return a context manager that shows a spinner while body executes.

        ConsoleRenderer: console.status(label, spinner="dots").
        TuiRenderer: contextlib.nullcontext() — streaming zone serves this purpose.
        """

    # ── Rich output ───────────────────────────────────────────────────────────

    @abstractmethod
    def on_markdown_panel(self, text: str, title: Optional[str] = None) -> None:
        """Render a completed markdown response as a panel/block."""

    # ── Session / metadata ────────────────────────────────────────────────────

    @abstractmethod
    def on_iteration_limit(self, limit: int) -> None:
        """Maximum iterations reached."""

    @abstractmethod
    def on_session_summary(
        self,
        snapshot: Any,
        *,
        approval_mode: Optional[str] = None,
    ) -> None:
        """End-of-turn: token usage, todo list, etc."""

    @abstractmethod
    def on_subagent_tokens(self, count: int, total: int) -> None:
        """Subagent token summary after a turn that spawned agents."""

    # ── Parallel display ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def parallel_display(self) -> Any:
        """Return the parallel-agent display object, or None to create a fresh one.

        TuiRenderer: returns app.slots (SlotsManager).
        ConsoleRenderer: returns None (caller creates AgentLiveDisplay each time).
        """
