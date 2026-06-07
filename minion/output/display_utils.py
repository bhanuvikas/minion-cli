"""Shared display utilities for slot and inspector rendering.

These helpers are used by agents/display.py (console parallel display),
tui/slots.py (TUI slot zone), and tui/inspector.py (subagent inspector).
"""

from __future__ import annotations

_SKIP_KEYS = frozenset({"content", "old_string", "new_string"})

_SLOT_VALUE_MAX = 50  # chars before switching to double-quote truncation


def apply_slot_event(state: dict, event: str, **data) -> None:
    """Apply a parallel-slot display event to the slot state dict in-place.

    Pure function — no I/O, no side effects beyond mutating *state*.
    Used by both ParallelDisplay.make_callback() and SlotsManager.make_callback()
    so all event-handling logic lives in one place (Single Responsibility).

    Recognised events:
      running             — mark slot active
      complete            — record latency_ms + preview
      error               — record error message
      diff                — store Rich markup diff for inline display
      tool_call           — update last_activity from tool name + inputs
      text                — rolling text buffer → last_activity snippet
      parallel_sub_start  — populate sub_activities list
      parallel_sub_done   — mark one sub_activity complete
      parallel_sub_clear  — clear sub_activities
    """
    if event == "running":
        state["status"] = "running"
    elif event == "complete":
        state.update({
            "status":     "complete",
            "latency_ms": data.get("latency_ms", 0),
            "preview":    data.get("preview", ""),
        })
    elif event == "error":
        state.update({
            "status": "error",
            "error":  data.get("error", ""),
        })
    elif event == "diff":
        state["diff_markup"] = data.get("markup", "")
    elif event == "tool_call":
        name   = data.get("name", "")
        inputs = data.get("inputs", {})
        state["last_activity"] = f"↳ {name}  {format_tool_args(inputs)}"
    elif event == "text":
        buf = state.get("_text_buf", "") + data.get("text", "")
        state["_text_buf"] = buf[-200:]
        flat = " ".join(state["_text_buf"].split())
        if flat:
            state["last_activity"] = f"· {flat[-80:]}"
    elif event == "parallel_sub_start":
        state["sub_activities"] = [
            {
                "key":  t["key"],
                "text": f"↳ {t['name']}  {format_tool_args(t['inputs'])}",
                "done": False,
            }
            for t in data.get("tools", [])
        ]
    elif event == "parallel_sub_done":
        done_key = data.get("key")
        for sa in state.get("sub_activities", []):
            if sa["key"] == done_key:
                sa["done"] = True
    elif event == "parallel_sub_clear":
        state["sub_activities"] = []


def _trunc(text: str, n: int) -> str:
    """Truncate text to n characters, appending … if shortened."""
    return text if len(text) <= n else text[: n - 1] + "…"


def tool_name_style(tool_name: str) -> str:
    """Return the bold + optional-colour style string for a tool name.

    Works in both prompt_toolkit FormattedText and Rich markup — hex colors
    are accepted by both rendering systems. Returns plain "bold" when the
    tool has no special colour in _TOOL_NAME_COLORS.

    Used by render_message_blocks (inspector), tool_slot_header_frags (slots),
    and format_tool_call (formatter) so the name style is consistent.
    """
    from ..theme import _TOOL_NAME_COLORS
    color = _TOOL_NAME_COLORS.get(tool_name, "")
    return f"bold {color}".strip()


def tool_slot_header_frags(
    tool_name: str,
    inputs: dict,
    *,
    icon_style: str | None = None,
    expanded: bool = False,
) -> list[tuple[str, str]]:
    """Format-neutral (style, text) fragment list for a live tool-slot header.

    Portable to both prompt_toolkit FormattedText and Rich Text — all styles
    are hex colors / bold modifiers, never class names or Rich tags.

    icon_style: override the icon's style. Defaults to #C0C0C0 (silver) across
                all contexts — live slots, scrollback, and inspector.
    expanded:   when True, includes keys that are normally skipped (content,
                old_string, new_string) with a larger value length limit.

    Icon:   #C0C0C0  (silver — consistent across live and historical contexts)
    Name:   bold + optional colour from _TOOL_NAME_COLORS
    Key:    #C0C0C0  (silver/muted)
    Value:  #1E90FF  (blue)
    """
    from ..theme import BLUE, YELLOW  # lazy to avoid circular at init

    _icon_style = icon_style if icon_style is not None else "#C0C0C0"
    _skip       = frozenset() if expanded else _SKIP_KEYS
    _max        = 200 if expanded else _SLOT_VALUE_MAX

    frags: list[tuple[str, str]] = [
        (_icon_style, "⚙  "),
        (tool_name_style(tool_name), tool_name),
    ]
    for k, v in inputs.items():
        if k in _skip:
            continue
        if isinstance(v, str):
            v_clean = v.replace("\n", "↵").replace("\r", "")
            v_disp = f'"{v_clean[:_max]}…"' if len(v_clean) > _max else f"'{v_clean}'"
        else:
            v_disp = repr(v)[:40]
        frags.append(("#C0C0C0", f"  {k}="))
        frags.append((BLUE, v_disp))

    return frags


def frags_to_rich_markup(frags: list[tuple[str, str]]) -> str:
    """Adapter: convert (style, text) fragment list → Rich markup string.

    Styles must be hex colors / bold modifiers (no class: names).
    This bridges tool_slot_header_frags() (the canonical semantic layer)
    to Rich-based renderers such as format_tool_call().
    Text is escaped so Rich special characters in values don't break markup.
    """
    from rich.markup import escape as _e
    return "".join(
        f"[{style}]{_e(text)}[/]" if style else _e(text)
        for style, text in frags
    )


def tool_diff_markup(tool_name: str, inp: dict) -> str:
    """Return a Rich markup diff string for write_file / edit_file tool inputs.

    edit_file  → character-level diff between old_string and new_string.
    write_file → all-additions diff (diff from empty string).
    All other tools → empty string (caller should skip rendering).
    """
    from ..output.diff import format_diff_rich
    if tool_name == "edit_file":
        old = inp.get("old_string", "")
        new = inp.get("new_string", "")
        if old or new:
            return format_diff_rich(old, new)
    elif tool_name == "write_file":
        content = inp.get("content", "")
        if content:
            return format_diff_rich("", content)
    return ""


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
