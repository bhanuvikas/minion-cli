"""minion.theme — public re-export package.

All symbols re-exported so every existing `from minion.theme import X`
statement continues to work without modification.
"""

# ── Palette ───────────────────────────────────────────────────────────────────
from .palette import (
    YELLOW,
    BLUE,
    GREEN,
    DENIM,
    GREY,
    SILVER,
    MINION_THEME,
    _TOOL_NAME_COLORS,
)

# ── Console ───────────────────────────────────────────────────────────────────
from .console import (
    console,
    startup_warnings,
    _active_status,
    set_active_status,
    pause_spinner,
    resume_spinner,
)

# ── Banner ────────────────────────────────────────────────────────────────────
from .banner import (
    BANNER_COMMANDS,
    print_greeting,
    print_startup_warnings,
)

# ── Printers ──────────────────────────────────────────────────────────────────
from .printers import (
    print_error,
    print_model_info,
    print_todo_list,
    print_usage,
    print_context,
    stream_response_to_stdout,
    MarkdownStreamer,
    print_mode_toggle,
    print_tool_call,
    print_trust_saved,
    print_tool_result,
    print_tool_error,
    print_iteration_limit,
    print_reflection_header,
    print_critique,
    print_diff,
)

__all__ = [
    # palette
    "YELLOW", "BLUE", "GREEN", "DENIM", "GREY", "SILVER",
    "MINION_THEME", "_TOOL_NAME_COLORS",
    # console
    "console", "startup_warnings", "_active_status",
    "set_active_status", "pause_spinner", "resume_spinner",
    # banner
    "BANNER_COMMANDS", "print_greeting", "print_startup_warnings",
    # printers
    "print_error", "print_model_info", "print_todo_list",
    "print_usage", "print_context", "stream_response_to_stdout",
    "MarkdownStreamer", "print_mode_toggle", "print_tool_call",
    "print_trust_saved", "print_tool_result", "print_tool_error",
    "print_iteration_limit", "print_reflection_header",
    "print_critique", "print_diff",
]
