"""Conversation state manager for the Textual TUI.

Responsibilities:
  • Track the in-progress streaming assistant turn (shown in StreamingZone).
  • Route completed turns to the RichLog via write_ansi_fn callback.
  • Enforce consistent vertical spacing between turn types.

Spacing contract
────────────────
  user turn   blank line BEFORE + blank line AFTER
  assistant   blank line BEFORE when tool calls / external prints preceded it
  tool calls  compact (no blank lines between consecutive tool lines)
  system/ansi no extra spacing — content controls its own margins

Rendering is fully delegated to tui/render.py.
This module contains NO Rich/ANSI rendering logic.
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
    """Routes completed turns to the RichLog; holds streaming turn in-memory.

    Wire up set_callbacks() before first use.  MinionApp calls mark_printed()
    whenever it writes to the RichLog via print_renderable() so that
    finalize_turn() can insert a blank line between tool output and the
    assistant response.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._width: int = 120

        # ── Streaming state (rendered live in StreamingZone) ──────────────────
        self._streaming_text: str = ""
        self._is_streaming: bool = False
        self._is_thinking: bool = False   # true between submit and first token

        # ── Spacing trackers ──────────────────────────────────────────────────
        self._had_external_print: bool = False
        self._last_was_assistant: bool = False
        self._gap_emitted: bool = False

        # ── Pre-finalize hook (set by MinionApp) ──────────────────────────────
        self._pre_finalize_fn: Optional[Callable[[], None]] = None

        # ── Callbacks wired by MinionApp ──────────────────────────────────────
        self._write_ansi_fn:  Optional[Callable[[str], None]] = None
        self._refresh_fn:     Optional[Callable[[], None]]    = None

    def set_callbacks(
        self,
        write_ansi_fn: Callable[[str], None],
        refresh_fn: Callable[[], None],
        # flush_fn kept for API compatibility — no-op in Textual (renders per frame)
        flush_fn: Optional[Callable[[], None]] = None,
        pre_finalize_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self._write_ansi_fn    = write_ansi_fn
        self._refresh_fn       = refresh_fn
        self._pre_finalize_fn  = pre_finalize_fn

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit(self, ansi: str) -> None:
        """Push ANSI to the RichLog. Guarantees exactly one trailing newline."""
        if not ansi:
            return
        if not ansi.endswith("\n"):
            ansi += "\n"
        if self._write_ansi_fn:
            self._write_ansi_fn(ansi)

    def _refresh(self) -> None:
        if self._refresh_fn:
            self._refresh_fn()

    # ── External-print notification ───────────────────────────────────────────

    def mark_printed(self) -> None:
        """Called by MinionApp whenever print_renderable() writes to the RichLog."""
        self._had_external_print = True

    # ── Append API ────────────────────────────────────────────────────────────

    def append_user(self, text: str) -> None:
        """Emit a user turn with blank line before and after."""
        ansi = _r.user_turn(text, self._width)
        self._emit("\n" + ansi + "\n\n")
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
            self._emit("\n")
        self._refresh()

    def stream_chunk(self, chunk: str) -> None:
        """Append a text chunk to the in-progress streaming turn."""
        with self._lock:
            self._streaming_text += chunk
        self._refresh()

    def finalize_turn(self) -> None:
        """Commit the complete assistant response to the RichLog."""
        with self._lock:
            text = self._streaming_text
            self._is_streaming   = False
            self._streaming_text = ""
        if self._pre_finalize_fn:
            self._pre_finalize_fn()
        if text:
            self._emit(_r.assistant_turn(text, self._width))
        with self._lock:
            self._last_was_assistant = True
        self._refresh()

    def append_system(self, rich_markup: str) -> None:
        """Emit a system/status message (rendered from Rich markup)."""
        with self._lock:
            was_assist = self._last_was_assistant
            self._last_was_assistant = False
            self._had_external_print = True
        prefix = "\n" if was_assist else ""
        self._emit(prefix + _r.system_message(rich_markup, self._width))

    def append_ansi(self, ansi: str) -> None:
        """Emit a pre-rendered ANSI string (slash command output, etc.)."""
        with self._lock:
            self._last_was_assistant = False
        self._emit(ansi)

    # ── Tool call API ─────────────────────────────────────────────────────────

    def append_tool_call(self, name: str, key_arg: str = "") -> int:
        with self._lock:
            idx = getattr(self, "_next_tool_idx", 0)
            setattr(self, "_next_tool_idx", idx + 1)
            self._had_external_print = True
        self._emit(_r.tool_call_line(name, key_arg, self._width))
        return idx

    def resolve_tool_call(self, idx: int, success: bool, summary: str = "") -> None:
        self._emit(_r.tool_result_line(success, summary, self._width))

    def has_pending_tools(self) -> bool:
        return False

    def set_thinking(self, thinking: bool) -> None:
        """Show/hide the thinking animation in StreamingZone."""
        with self._lock:
            self._is_thinking = thinking
        self._refresh()

    def emit_spacer(self) -> None:
        """Emit a blank line and mark gap so start_assistant_turn() skips its own."""
        self._emit("\n")
        with self._lock:
            self._gap_emitted = True

    def clear(self) -> None:
        with self._lock:
            self._streaming_text     = ""
            self._is_streaming       = False
            self._is_thinking        = False
            self._had_external_print = False
            self._last_was_assistant = False
            self._gap_emitted        = False

    def scroll_to_bottom(self) -> None:
        pass  # RichLog handles auto-scroll

    def set_width(self, width: int) -> None:
        with self._lock:
            self._width = max(40, width)

    # ── StreamingZone render ──────────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        with self._lock:
            return self._is_streaming or self._is_thinking

    def get_streaming_markup(self) -> str:
        """Return Rich markup string for the live StreamingZone Static widget."""
        with self._lock:
            text         = self._streaming_text
            width        = self._width
            is_thinking  = self._is_thinking
            is_streaming = self._is_streaming

        if not is_thinking and not is_streaming:
            return " "   # single space keeps the zone at height=1

        if is_thinking and not is_streaming:
            t      = _time.monotonic()
            frame  = _THINK_FRAMES[int(t * 4) % len(_THINK_FRAMES)]
            phrase = _THINK_PHRASES[int(t * 0.5) % len(_THINK_PHRASES)]
            return f"[bold #1E90FF]{frame}[/]  [italic #1E90FF]{phrase}[/]"

        # Normal streaming phase
        prefix = "[bold #1E90FF]▌ minion ›[/] "
        if not text:
            return prefix + "[#C0C0C0]…[/]"

        visible = "\n".join(text.split("\n")[-12:])
        try:
            ansi = _r.render_markdown(visible, width).rstrip("\n")
            # Return as plain text (ANSI may not render in Static markup mode)
            # Fall back to stripping ANSI for simplicity
            import re as _re
            plain = _re.sub(r"\x1b\[[0-9;]*m", "", ansi)
            return prefix + plain
        except Exception:
            return prefix + visible

    @property
    def is_empty(self) -> bool:
        return True  # history lives in the RichLog widget
