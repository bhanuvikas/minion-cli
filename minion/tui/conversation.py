"""Conversation state manager for the Textual TUI.

Responsibilities:
  • Track the in-progress streaming assistant turn (shown in StreamingZone).
  • Route completed turns to ConversationArea via write_block_fn callback.
  • Enforce consistent vertical spacing between turn types.

Spacing contract
────────────────
  user turn   blank line BEFORE + blank line AFTER
  assistant   blank line BEFORE when tool calls / external prints preceded it
  tool calls  compact (no blank lines between consecutive tool lines)
  system/ansi no extra spacing — content controls its own margins

Rendering is fully delegated to tui/render.py.
This module contains NO ANSI rendering logic.
"""

from __future__ import annotations

import threading
import time as _time
from typing import Callable, Optional

# ── Thinking animation frames ─────────────────────────────────────────────────

_THINK_FRAMES = ["◡ ◡", "○ ○", "◠ ○", "○ ○", "○ ◠", "○ ○"]

_THINK_PHRASES = [
    "thinking",           # English
    "pensando",           # Spanish
    "bee do bee do",      # Minionese
    "réfléchissant",      # French
    "考え中",              # Japanese
    "bello!",             # Minionese
    "nachdenken",         # German
    "సోచిస్తున్నా",        # Telugu
    "думаю",              # Russian
    "tulaliloo",          # Minionese
    "सोच रहा हूँ",        # Hindi
    "σκέφτομαι",          # Greek
    "gelato!",            # Minionese
    " யோசிக்கிறேன்",      # Tamil
    "يفكر",               # Arabic
    "생각 중",             # Korean
    "para tú",            # Minionese
    "ਸੋਚ ਰਿਹਾ ਹਾਂ",      # Punjabi
    "思考中",              # Mandarin
    "ভাবছি",              # Bengali
    "suy nghĩ",           # Vietnamese
    "düşünüyorum",        # Turkish
    "ninafikiri",         # Swahili
    "sto pensando",       # Italian
    "ajattelen",          # Finnish
    "mă gândesc",         # Romanian
]

from . import render as _r


class ConversationBuffer:
    """Routes completed turns to ConversationArea; holds streaming turn in-memory.

    Wire up set_callbacks() before first use.  MinionApp calls mark_printed()
    whenever it writes to the conversation via print_renderable() so that
    finalize_turn() can insert a blank line between tool output and the
    assistant response.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # ── Streaming state (rendered live in StreamingZone) ──────────────────
        self._streaming_text: str = ""
        self._is_streaming: bool = False
        self._is_thinking: bool = False   # true between submit and first token

        # ── Last committed assistant text (for clipboard copy) ────────────────
        self._last_assistant_text: str = ""

        # ── Spacing trackers ──────────────────────────────────────────────────
        self._had_external_print: bool = False
        self._last_was_assistant: bool = False
        self._gap_emitted: bool = False

        # ── Pre-finalize hook (set by MinionApp) ──────────────────────────────
        self._pre_finalize_fn: Optional[Callable[[], None]] = None

        # ── Callbacks wired by MinionApp ──────────────────────────────────────
        self._write_block_fn: Optional[Callable] = None
        self._refresh_fn:     Optional[Callable[[], None]] = None

    def set_callbacks(
        self,
        write_block_fn: Callable,
        refresh_fn: Callable[[], None],
        # flush_fn kept for API compatibility — no-op in Textual (renders per frame)
        flush_fn: Optional[Callable[[], None]] = None,
        pre_finalize_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self._write_block_fn   = write_block_fn
        self._refresh_fn       = refresh_fn
        self._pre_finalize_fn  = pre_finalize_fn

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, block) -> None:
        """Route a Rich renderable block to the write callback."""
        if self._write_block_fn:
            self._write_block_fn(block)

    def _blank(self):
        """Return a single blank-row renderable."""
        from rich.text import Text
        return Text(" ")

    def _refresh(self) -> None:
        if self._refresh_fn:
            self._refresh_fn()

    # ── External-print notification ───────────────────────────────────────────

    def mark_printed(self) -> None:
        """Called by MinionApp whenever print_renderable() writes to the conversation."""
        self._had_external_print = True

    # ── Append API ────────────────────────────────────────────────────────────

    def append_user(self, text: str) -> None:
        """Emit a user turn with blank line before and after."""
        self._emit(self._blank())
        self._emit(_r.user_turn(text))
        self._emit(self._blank())
        with self._lock:
            self._last_was_assistant = False
            self._had_external_print = False
            self._gap_emitted = False

    def start_assistant_turn(self) -> None:
        """Begin a new streaming assistant turn."""
        with self._lock:
            had_print   = self._had_external_print
            gap_already = self._gap_emitted
            self._streaming_text = ""
            self._is_streaming   = True
            self._last_was_assistant = False
            self._gap_emitted    = False
        if had_print and not gap_already:
            self._emit(self._blank())
        self._refresh()

    def stream_chunk(self, chunk: str) -> None:
        """Append a text chunk to the in-progress streaming turn."""
        with self._lock:
            self._streaming_text += chunk
        self._refresh()

    def finalize_turn(self) -> None:
        """Commit the complete assistant response to the conversation."""
        with self._lock:
            text = self._streaming_text
            self._is_streaming   = False
            self._streaming_text = ""
            if text:
                self._last_assistant_text = text
        if self._pre_finalize_fn:
            self._pre_finalize_fn()
        if text:
            self._emit(_r.assistant_turn(text))
        with self._lock:
            self._last_was_assistant = True
        self._refresh()

    def append_system(self, rich_markup: str) -> None:
        """Emit a system/status message (rendered from Rich markup)."""
        with self._lock:
            was_assist = self._last_was_assistant
            self._last_was_assistant = False
            self._had_external_print = True
        if was_assist:
            self._emit(self._blank())
        self._emit(_r.system_message(rich_markup))

    def append_ansi(self, ansi: str) -> None:
        """Emit a pre-rendered ANSI string (slash command output, etc.)."""
        from rich.text import Text
        with self._lock:
            self._last_was_assistant = False
        block = Text.from_ansi(ansi.rstrip("\n")) if ansi.strip() else self._blank()
        self._emit(block)

    def append_block(self, block) -> None:
        """Emit any Rich renderable directly. Used by TuiRenderer for custom blocks."""
        self._emit(block)

    # ── Tool call API ─────────────────────────────────────────────────────────

    def append_tool_call(self, name: str, key_arg: str = "") -> int:
        with self._lock:
            idx = getattr(self, "_next_tool_idx", 0)
            setattr(self, "_next_tool_idx", idx + 1)
            self._had_external_print = True
        self._emit(_r.tool_call_line(name, key_arg))
        return idx

    def resolve_tool_call(self, idx: int, success: bool, summary: str = "") -> None:
        self._emit(_r.tool_result_line(success, summary))

    def has_pending_tools(self) -> bool:
        return False

    def set_thinking(self, thinking: bool) -> None:
        """Show/hide the thinking animation in StreamingZone."""
        with self._lock:
            self._is_thinking = thinking
        self._refresh()

    def emit_spacer(self) -> None:
        """Emit a blank line and mark gap so start_assistant_turn() skips its own."""
        self._emit(self._blank())
        with self._lock:
            self._gap_emitted = True

    @property
    def last_assistant_text(self) -> str:
        """Last committed assistant response text (for clipboard copy)."""
        with self._lock:
            return self._last_assistant_text

    def clear(self) -> None:
        with self._lock:
            self._streaming_text     = ""
            self._is_streaming       = False
            self._is_thinking        = False
            self._had_external_print = False
            self._last_was_assistant = False
            self._gap_emitted        = False

    def scroll_to_bottom(self) -> None:
        pass  # ConversationArea handles auto-scroll

    def set_width(self, width: int) -> None:
        pass  # vestigial — width is now determined by Textual CSS layout

    # ── StreamingZone render ──────────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        with self._lock:
            return self._is_streaming or self._is_thinking

    def get_streaming_renderable(self):
        """Return a Rich renderable for the live StreamingZone Static widget."""
        from rich.text import Text
        with self._lock:
            text         = self._streaming_text
            is_thinking  = self._is_thinking
            is_streaming = self._is_streaming

        if not is_thinking and not is_streaming:
            return Text(" ")   # single space keeps the zone at height=1

        if is_thinking and not is_streaming:
            t      = _time.monotonic()
            frame  = _THINK_FRAMES[int(t * 4) % len(_THINK_FRAMES)]
            phrase = _THINK_PHRASES[int(t * 0.5) % len(_THINK_PHRASES)]
            result = Text()
            result.append(frame, style="bold #1E90FF")
            result.append("  ")
            result.append(phrase, style="italic #1E90FF")
            return result

        # Normal streaming phase — end="" so prefix shares first line with Markdown
        prefix = Text(end="")
        prefix.append("▌ minion ›", style="bold #1E90FF")
        prefix.append(" ")
        if not text:
            prefix.append("…", style="#C0C0C0")
            return prefix

        from rich.console import Group
        from rich.markdown import Markdown
        return Group(prefix, Markdown("\n".join(text.split("\n")[-12:])))

    @property
    def is_empty(self) -> bool:
        return True  # history lives in the ConversationArea widget
