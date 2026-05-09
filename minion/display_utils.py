"""Shared display utilities for slot and inspector rendering.

These helpers are used by agents/display.py (console parallel display),
tui/slots.py (TUI slot zone), and tui/inspector.py (subagent inspector).
"""

from __future__ import annotations

_SKIP_KEYS = frozenset({"content", "old_string", "new_string"})

_SLOT_VALUE_MAX = 50  # chars before switching to double-quote truncation


def _trunc(text: str, n: int) -> str:
    """Truncate text to n characters, appending … if shortened."""
    return text if len(text) <= n else text[: n - 1] + "…"


def tool_slot_header_frags(tool_name: str, inputs: dict) -> list[tuple[str, str]]:
    """Format-neutral (style, text) fragment list for a live tool-slot header.

    Portable to both prompt_toolkit FormattedText and Rich Text — all styles
    are hex colors / bold modifiers, never class names or Rich tags.

    Icon:   bold #FFD700  (yellow — these tools are currently running/live)
    Name:   bold + optional colour from _TOOL_NAME_COLORS
    Key:    #C0C0C0  (silver/muted)
    Value:  #1E90FF  (blue)

    Suppresses write_file 'content' and edit_file 'old_string'/'new_string'
    because the diff preview already shows that information.
    """
    from .theme import BLUE, YELLOW, _TOOL_NAME_COLORS  # lazy to avoid circular at init

    _name_color = _TOOL_NAME_COLORS.get(tool_name, "")
    _name_style = f"bold {_name_color}".strip()

    frags: list[tuple[str, str]] = [
        (f"bold {YELLOW}", "⚙  "),
        (_name_style, tool_name),
    ]
    for k, v in inputs.items():
        if k in _SKIP_KEYS:
            continue
        if isinstance(v, str):
            v_clean = v.replace("\n", "↵").replace("\r", "")
            v_disp = f'"{v_clean[:_SLOT_VALUE_MAX]}…"' if len(v_clean) > _SLOT_VALUE_MAX else f"'{v_clean}'"
        else:
            v_disp = repr(v)[:40]
        frags.append(("#C0C0C0", f"  {k}="))
        frags.append((BLUE, v_disp))

    return frags


def format_tool_args(inputs: dict, *, expanded: bool = False) -> str:
    """Return a formatted key=value snippet from tool inputs.

    Normal mode (expanded=False): up to 3 pairs, 45-char limit per value,
    large content keys skipped (content, old_string, new_string), newlines
    replaced with ↵.

    Expanded mode: all keys included, 200-char limit, up to 3 pairs.
    """
    if not inputs:
        return ""
    skip  = frozenset() if expanded else _SKIP_KEYS
    limit = 200 if expanded else 45
    parts: list[str] = []
    for k, v in inputs.items():
        if k in skip:
            continue
        if isinstance(v, str):
            v_disp = f"'{_trunc(v.replace('\n', '↵'), limit)}'"
        else:
            v_disp = repr(v)[:40]
        parts.append(f"{k}={v_disp}")
        if len(parts) >= 3:
            break
    return "  ".join(parts)
