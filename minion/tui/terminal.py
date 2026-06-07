"""Terminal environment detection for TUI-specific hints.

Checks environment variables only — no subprocess, no filesystem I/O.
All public API is safe to call at import time.
"""

from __future__ import annotations

import os
from typing import Optional


def detect_terminal() -> str:
    """Return a canonical terminal identifier from environment variables.

    Returns one of: iterm2, terminal_app, kitty, wezterm, vscode, hyper,
    tmux, ssh, or unknown.
    """
    # Kitty sets a unique env var regardless of TERM_PROGRAM
    if os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"

    # WezTerm sets WEZTERM_PANE (and WEZTERM_UNIX_SOCKET on some platforms)
    if os.environ.get("WEZTERM_PANE") or os.environ.get("WEZTERM_UNIX_SOCKET"):
        return "wezterm"

    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program == "iterm.app":
        return "iterm2"
    if term_program == "apple_terminal":
        return "terminal_app"
    if term_program == "vscode":
        return "vscode"
    if term_program == "hyper":
        return "hyper"
    if term_program == "ghostty":
        return "ghostty"

    # TERM can also identify kitty when TERM_PROGRAM isn't set
    if "kitty" in os.environ.get("TERM", ""):
        return "kitty"

    # Multiplexers / remote sessions — check after terminal-specific vars
    if os.environ.get("TMUX"):
        return "tmux"
    if os.environ.get("STY"):      # GNU Screen
        return "screen"
    if os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return "ssh"

    return "unknown"


# Gesture description per terminal (used in Rich markup).
# {modifier} is the modifier key; the full tip is built in get_selection_tip().
_DRAG_TIPS: dict[str, str] = {
    "iterm2":       "hold [bold]Option[/bold] while clicking and dragging",
    "terminal_app": "hold [bold]Option[/bold] while clicking and dragging",
    "kitty":        "hold [bold]Shift[/bold] while clicking and dragging",
    "wezterm":      "hold [bold]Alt[/bold] while clicking and dragging",
    "ghostty":      "hold [bold]Shift[/bold] while clicking and dragging",
    "vscode":       "hold [bold]Alt[/bold] while clicking and dragging",
    "hyper":        "hold [bold]Shift[/bold] while clicking and dragging",
    "tmux":         "hold [bold]Shift[/bold] while clicking and dragging",
    "screen":       "hold [bold]Shift[/bold] while clicking and dragging",
}

_CTRL_Y = "press [bold]ctrl+y[/bold] to copy the last response"


def get_selection_tip() -> str:
    """Return a terminal-specific selection tip message (Rich markup, no label prefix).

    The caller adds the display label (e.g. a gold "Tip" prefix). Always
    includes the ctrl+y fallback; for known terminals also includes the native
    drag-select modifier so users don't have to discover it themselves.
    """
    terminal = detect_terminal()
    drag = _DRAG_TIPS.get(terminal)

    if terminal == "ssh":
        return _CTRL_Y

    if drag:
        return f"to select text — {drag} · or {_CTRL_Y}"

    # Unknown terminal: ctrl+y + generic modifier hint
    return (
        f"{_CTRL_Y} · "
        "or hold [bold]Shift[/bold]/[bold]Option/Alt[/bold] and drag "
        "to select text in most terminals"
    )
