"""Shared display utilities for slot and inspector rendering.

These helpers are used by agents/display.py (console parallel display),
tui/slots.py (TUI slot zone), and tui/inspector.py (subagent inspector).
"""

from __future__ import annotations

_SKIP_KEYS = frozenset({"content", "old_string", "new_string"})


def _trunc(text: str, n: int) -> str:
    """Truncate text to n characters, appending … if shortened."""
    return text if len(text) <= n else text[: n - 1] + "…"


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
