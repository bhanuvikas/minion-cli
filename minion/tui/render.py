"""Stateless ANSI rendering functions for TUI conversation output.

All functions return ANSI strings.  No trailing newlines are added — callers
in conversation.py decide vertical spacing.

Colours here MUST stay in sync with tui/theme.py TUI_STYLE:
  #FFD700  you-prefix     (bold gold)
  #1E90FF  minion-prefix  (bold blue)
  #4CAF50  tool-ok        (green)
  #C0C0C0  tool-icon      (silver)
  #666666  tool-detail    (dim grey)
  bold red tool-err
"""

from __future__ import annotations

import io

# ── Colour constants (mirror TUI_STYLE) ───────────────────────────────────────
YOU_STYLE    = "bold #FFD700"
MINION_STYLE = "bold #1E90FF"
SEP_STYLE    = "#888888"      # › separator
TOOL_ICON    = "#C0C0C0"
TOOL_DETAIL  = "#666666"
TOOL_OK      = "#4CAF50"
TOOL_ERR     = "bold red"


# ── Low-level renderers ───────────────────────────────────────────────────────

def render_rich(markup: str, width: int = 120) -> str:
    """Render Rich markup to an ANSI string (no trailing newline)."""
    from rich.console import Console
    from ..theme import MINION_THEME as _THEME
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor",
        width=width, highlight=False, markup=True, theme=_THEME,
    ).print(markup, end="")
    return buf.getvalue()


def render_markdown(text: str, width: int = 120) -> str:
    """Render Markdown to ANSI via Rich (Rich adds its own trailing newlines)."""
    from rich.console import Console
    from rich.markdown import Markdown
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor",
        width=width, highlight=False,
    ).print(Markdown(text), end="")
    return buf.getvalue()


# ── Turn renderers ────────────────────────────────────────────────────────────

def user_turn(text: str, width: int = 120) -> str:
    """Render a user turn (no trailing newline).

    Multi-line inputs are indented to align under the first line.
    """
    lines = text.strip().split("\n")
    markup = f"[{YOU_STYLE}]you[/] [{SEP_STYLE}]›[/] " + lines[0]
    for line in lines[1:]:
        markup += "\n      " + line
    try:
        return render_rich(markup, width)
    except Exception:
        return f"you › {text}"


def assistant_turn(text: str, width: int = 120) -> str:
    """Render a complete assistant turn (prefix + rendered markdown).

    Returns the combined string; trailing newlines come from Rich's markdown
    renderer and are preserved as-is.
    """
    try:
        prefix = render_rich(f"[{MINION_STYLE}]minion[/] [{SEP_STYLE}]›[/]", width)
        md     = render_markdown(text, width)
        # Strip prefix's own trailing newline (end="" doesn't always prevent it)
        # and markdown's leading newline, then join with a space.
        return prefix.rstrip("\n") + " " + md.lstrip("\n")
    except Exception:
        return render_rich(f"[{MINION_STYLE}]minion[/] [{SEP_STYLE}]›[/] {text}", width)


def tool_call_line(name: str, key_arg: str = "", width: int = 120) -> str:
    """Render the "⚙  name  arg" pending line (no trailing newline)."""
    detail = f"  {key_arg}" if key_arg else ""
    markup = f"  [{TOOL_ICON}]⚙[/]  [bold]{name}[/][{TOOL_DETAIL}]{detail}[/]"
    try:
        return render_rich(markup, width)
    except Exception:
        return f"  ⚙  {name}{detail}"


def tool_result_line(success: bool, summary: str = "", width: int = 120) -> str:
    """Render the "   └─ ✓/✗  summary" result line (no trailing newline)."""
    icon   = "✓" if success else "✗"
    color  = TOOL_OK if success else TOOL_ERR
    markup = f"     └─ [{color}]{icon}[/]"
    if summary:
        markup += f"  [{TOOL_DETAIL}]{summary}[/]"
    try:
        return render_rich(markup, width)
    except Exception:
        return f"     └─ {icon} {summary}"


def system_message(rich_markup: str, width: int = 120) -> str:
    """Render a system/status message from Rich markup (no trailing newline)."""
    try:
        return render_rich(rich_markup, width)
    except Exception:
        return rich_markup
