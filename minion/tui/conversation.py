"""Conversation state manager for the TUI.

Responsibilities:
  • Track the in-progress streaming assistant turn (shown in the streaming zone).
  • Route completed turns to the terminal scrollback via the print_ansi_fn
    callback wired by MinionApp.set_callbacks().
  • Enforce consistent vertical spacing between turn types.

Spacing contract
────────────────
  user turn   blank line BEFORE + blank line AFTER
               ("you" is always visually separated from its neighbours)
  assistant   blank line BEFORE when tool calls / external prints preceded it;
               no extra blank when the assistant responds directly to the user
  tool calls  compact (no blank lines between consecutive tool lines)
  system/ansi no extra spacing — content controls its own margins

Rendering is fully delegated to tui/render.py.
This module contains NO Rich/ANSI rendering logic.
"""

from __future__ import annotations

import threading
import time as _time
from typing import Callable, Optional

# Crystallising-thought animation: seed → diamond → star → open → contract
_THINK_FRAMES = ["·", "◇", "◆", "✦", "✧", "✦", "◆", "◇"]

from prompt_toolkit.formatted_text import FormattedText

from . import render as _r


class ConversationBuffer:
    """Routes completed turns to the terminal scrollback; holds streaming turn in-memory.

    Wire up set_callbacks() before first use.  MinionApp calls mark_printed()
    whenever it writes to the scrollback via print_renderable() so that
    finalize_turn() can insert a blank line between tool output and the
    assistant response.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._width: int = 120

        # ── Streaming state (rendered live in the streaming zone) ─────────────
        self._streaming_text: str = ""
        self._is_streaming: bool = False
        self._is_thinking: bool = False   # true between submit and first token

        # ── Spacing trackers ──────────────────────────────────────────────────
        # _had_external_print: set by mark_printed() when MinionApp.print_renderable()
        # writes content between start_assistant_turn() and finalize_turn().
        # finalize_turn() uses this to decide if a blank line is needed before
        # the assistant response.
        self._had_external_print: bool = False

        # _last_was_assistant: set after finalize_turn() so that the next
        # append_system() call (e.g. hook tips) gets a leading blank line,
        # visually separating hook messages from the assistant response.
        self._last_was_assistant: bool = False

        # ── Callbacks wired by MinionApp ──────────────────────────────────────
        self._print_ansi_fn:  Optional[Callable[[str], None]] = None
        self._invalidate_fn:  Optional[Callable[[], None]]    = None

    def set_callbacks(
        self,
        print_ansi_fn: Callable[[str], None],
        invalidate_fn: Callable[[], None],
    ) -> None:
        self._print_ansi_fn = print_ansi_fn
        self._invalidate_fn = invalidate_fn

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, ansi: str) -> None:
        """Push ANSI to the scrollback. Guarantees exactly one trailing newline."""
        if not ansi:
            return
        if not ansi.endswith("\n"):
            ansi += "\n"
        if self._print_ansi_fn:
            self._print_ansi_fn(ansi)

    def _invalidate(self) -> None:
        if self._invalidate_fn:
            self._invalidate_fn()

    # ── External-print notification ───────────────────────────────────────────

    def mark_printed(self) -> None:
        """Called by MinionApp whenever print_renderable() writes to the scrollback.

        Tells finalize_turn() that content appeared between start_assistant_turn()
        and now, so a blank line should precede the assistant response.
        """
        self._had_external_print = True

    # ── Append API ────────────────────────────────────────────────────────────

    def append_user(self, text: str) -> None:
        """Emit a user turn with blank line before and after.

        Resets _had_external_print — each new user message is a fresh slate.
        Tool calls that run AFTER this will re-set the flag before finalize_turn().
        """
        ansi = _r.user_turn(text, self._width)
        # \n prefix  = blank line separating from previous output
        # \n\n suffix = blank line separating from what follows (tools / response)
        self._emit("\n" + ansi + "\n\n")
        with self._lock:
            self._last_was_assistant = False
            self._had_external_print = False   # reset for the new turn

    def start_assistant_turn(self) -> None:
        """Begin a new streaming assistant turn.

        Emits a blank line immediately when tool calls/system messages preceded
        this turn so the streaming zone is visually separated from the tool
        output right away — not only after finalize_turn() commits.

        Does NOT reset _had_external_print — it is read here and also in
        finalize_turn() so the finalized text omits a redundant second prefix.
        It is reset only in append_user() when a new user turn begins.
        """
        with self._lock:
            had_print = self._had_external_print
            self._streaming_text = ""
            self._is_streaming = True
            self._last_was_assistant = False
        if had_print:
            self._emit("\n")  # blank line now so streaming zone starts separated
        self._invalidate()

    def stream_chunk(self, chunk: str) -> None:
        """Append a text chunk to the in-progress streaming turn."""
        with self._lock:
            self._streaming_text += chunk
        self._invalidate()

    def finalize_turn(self) -> None:
        """Commit the complete assistant response to the scrollback.

        The blank line before the response (when tools preceded it) was already
        emitted by start_assistant_turn(), so no prefix is added here.
        """
        with self._lock:
            text = self._streaming_text
            self._is_streaming = False
            self._streaming_text = ""
        if text:
            self._emit(_r.assistant_turn(text, self._width))
        with self._lock:
            self._last_was_assistant = True
        self._invalidate()

    def append_system(self, rich_markup: str) -> None:
        """Emit a system/status message (rendered from Rich markup).

        Also sets _had_external_print so finalize_turn() knows to insert a blank
        line before the assistant response.  This covers tool-call display via
        execute_async() → _tui_conv() → append_system(), which never goes through
        print_renderable() / mark_printed().

        Adds a blank line before the first system message after an assistant
        turn so hook tips are visually separated from the response.
        """
        with self._lock:
            was_assist = self._last_was_assistant
            self._last_was_assistant = False
            self._had_external_print = True   # tool calls / hooks / system msgs all count
        prefix = "\n" if was_assist else ""
        self._emit(prefix + _r.system_message(rich_markup, self._width))

    def append_ansi(self, ansi: str) -> None:
        """Emit a pre-rendered ANSI string (slash command output, etc.)."""
        with self._lock:
            self._last_was_assistant = False
        self._emit(ansi)

    # ── Tool call API (kept for API compatibility; wired up by executor.py) ──

    def append_tool_call(self, name: str, key_arg: str = "") -> int:
        """Emit a pending tool-call line and return a tracking index."""
        # Use a simple monotonic counter — no lock needed for the index bump
        # because this is always called from the async iteration loop.
        with self._lock:
            idx = getattr(self, "_next_tool_idx", 0)
            setattr(self, "_next_tool_idx", idx + 1)
            self._had_external_print = True
        self._emit(_r.tool_call_line(name, key_arg, self._width))
        return idx

    def resolve_tool_call(self, idx: int, success: bool, summary: str = "") -> None:
        """Emit the tool result line for a previously appended tool call."""
        self._emit(_r.tool_result_line(success, summary, self._width))

    def has_pending_tools(self) -> bool:
        return False  # pending tracking removed; spinner driven by streaming state

    def set_thinking(self, thinking: bool) -> None:
        """Show/hide the streaming zone during the pre-token thinking phase."""
        with self._lock:
            self._is_thinking = thinking
        self._invalidate()

    def clear(self) -> None:
        with self._lock:
            self._streaming_text = ""
            self._is_streaming = False
            self._is_thinking = False
            self._had_external_print = False
            self._last_was_assistant = False

    def scroll_to_bottom(self) -> None:
        pass  # terminal handles its own scroll

    def set_width(self, width: int) -> None:
        with self._lock:
            self._width = max(40, width)

    # ── Streaming zone render (called by app.py layout) ──────────────────────

    @property
    def is_streaming(self) -> bool:
        with self._lock:
            return self._is_streaming or self._is_thinking

    def get_streaming_formatted_text(self) -> FormattedText:
        """Return prompt_toolkit fragments for the live streaming zone.

        Uses class: tokens (resolved by TUI_STYLE) for the prefix so colours
        are correct without embedding hex codes here.  Rich markdown is used
        for the content and converted to ANSI fragments.
        """
        from prompt_toolkit.formatted_text import ANSI

        with self._lock:
            text         = self._streaming_text
            width        = self._width
            is_thinking  = self._is_thinking
            is_streaming = self._is_streaming

        # Pre-token thinking phase: crystallising-thought animation
        if is_thinking and not is_streaming:
            frame = _THINK_FRAMES[int(_time.monotonic() * 8) % len(_THINK_FRAMES)]
            return FormattedText([
                ("class:thinking-icon", frame),
                ("class:slot-running",  "  thinking"),
            ])

        # Normal streaming phase
        frags: list[tuple[str, str]] = [
            ("class:minion-prefix", "minion"),
            ("", " › "),
        ]

        if not text:
            frags.append(("class:slot-running", "…"))
            return FormattedText(frags)

        # Clip to last 12 lines to keep the streaming zone compact
        visible = "\n".join(text.split("\n")[-12:])
        try:
            frags.extend(ANSI(_r.render_markdown(visible, width)).__pt_formatted_text__())
        except Exception:
            frags.append(("", visible))

        return FormattedText(frags)

    @property
    def is_empty(self) -> bool:
        return True  # history lives in terminal scrollback
