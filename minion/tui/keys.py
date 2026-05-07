"""Key binding documentation registry.

This module lists all TUI key bindings for display in /help.
It is not the executable binding registry — that lives in app.py.
"""

BINDINGS: list[dict] = [
    {"key": "Enter",         "action": "Submit prompt to agent"},
    {"key": "Shift+Enter",   "action": "Insert newline (multi-line input)"},
    {"key": "Ctrl+C",        "action": "Cancel running agent; exit when idle"},
    {"key": "Ctrl+L",        "action": "Clear visible conversation (history preserved)"},
    {"key": "Ctrl+O",        "action": "Open subagent inspector panel"},
    {"key": "Shift+Tab",     "action": "Cycle mode: CHAT → PLAN → EDITS → YOLO"},
    {"key": "PgUp",          "action": "Scroll conversation up"},
    {"key": "PgDn",          "action": "Scroll conversation down"},
    {"key": "Up / Down",     "action": "Navigate history (input) / move cursor (permission)"},
    {"key": "1 / 2 / 3 / 4","action": "Select permission scope (when permission panel shown)"},
    {"key": "Enter",         "action": "Confirm selected permission scope"},
    {"key": "Esc",           "action": "Deny permission / close inspector panel"},
]
