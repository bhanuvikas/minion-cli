"""Stateless Rich renderable factories for TUI conversation output.

All public functions return RenderableType objects — no ANSI strings, no width.
Textual renders them at the widget's actual CSS width automatically.

Colours here MUST stay in sync with tui/theme.py TUI_STYLE:
  #FFD700  you-prefix     (bold gold)
  #1E90FF  minion-prefix  (bold blue)
  #4CAF50  tool-ok        (green)
  #C0C0C0  tool-icon      (silver)
  #666666  tool-detail    (dim grey)
  bold red tool-err
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Group
    from rich.markdown import Markdown
    from rich.text import Text

# ── Colour constants (mirror TUI_STYLE) ───────────────────────────────────────

YOU_STYLE    = "bold #FFD700"
MINION_STYLE = "bold #1E90FF"
SEP_STYLE    = "#888888"      # › separator
RULE_STYLE   = "#333333"      # dim separator rule before user turns
ACCENT_STYLE  = "#FFD700"      # ▌ left accent bar on user message line
ACCENT_BLUE   = "#1E90FF"      # ▌ left accent bar on minion response
ACCENT_TOOL   = "#888888"      # (reserved — not currently used for tools)
TOOL_ICON     = "#C0C0C0"
TOOL_DETAIL  = "#666666"
TOOL_OK      = "#4CAF50"
TOOL_ERR     = "bold red"


# ── Low-level renderers ───────────────────────────────────────────────────────

def render_rich(markup: str) -> "Text":
    """Parse Rich markup into a Text object (no Console, no width).

    Custom theme styles (muted, primary, …) are resolved at render time by
    Textual's console, which has MINION_THEME pushed in MinionApp.on_mount().
    """
    from rich.text import Text
    try:
        return Text.from_markup(markup)
    except Exception:
        return Text(markup)


def render_markdown(text: str) -> "Markdown":
    """Wrap text in a Rich Markdown object (no Console, no width)."""
    from rich.markdown import Markdown
    return Markdown(text)


# ── Turn renderers ────────────────────────────────────────────────────────────

def user_turn(text: str) -> "Group":
    """Render a user turn: dim rule + gold accent bar + message.

    Layout:
        ──────────────────────────────   (dim rule, full width)
        ▌ you › message text here
    Multi-line inputs are indented to align under the first line.
    """
    from rich.console import Group
    from rich.rule import Rule
    from rich.text import Text
    lines = text.strip().split("\n")
    msg = Text()
    msg.append("▌", style=ACCENT_STYLE)
    msg.append(" ")
    msg.append("you", style=YOU_STYLE)
    msg.append(" › ", style=SEP_STYLE)
    first = lines[0]
    # Highlight a valid slash command (first word) in gold.
    if first.startswith("/"):
        word = first.split()[0] if first.split() else first
        try:
            from ..repl.state import REPL_COMMANDS as _CMDS
            if word in _CMDS:
                msg.append(word, style=YOU_STYLE)
                msg.append(first[len(word):])
            else:
                msg.append(first)
        except Exception:
            msg.append(first)
    else:
        msg.append(first)
    for line in lines[1:]:
        msg.append("\n        " + line)   # 8-space indent aligns with first line
    return Group(Rule(style=RULE_STYLE), msg)


def assistant_turn(text: str) -> "Group":
    """Render a complete assistant turn (blue ▌ bar + prefix + rendered markdown)."""
    from rich.console import Group
    from rich.markdown import Markdown
    from rich.text import Text
    # end="" so the prefix shares the first line with the Markdown body
    prefix = Text(end="")
    prefix.append("▌", style=ACCENT_BLUE)
    prefix.append(" ")
    prefix.append("minion", style=MINION_STYLE)
    prefix.append(" › ", style=SEP_STYLE)
    return Group(prefix, Markdown(text))


def tool_call_line(name: str, key_arg: str = "") -> "Text":
    """Render the "⚙  name  arg" pending line."""
    from rich.text import Text
    t = Text()
    t.append("  ")
    t.append("⚙", style=TOOL_ICON)
    t.append("  ")
    t.append(name, style="bold")
    if key_arg:
        t.append("  " + key_arg, style=TOOL_DETAIL)
    return t


def tool_result_line(success: bool, summary: str = "") -> "Text":
    """Render the "   └─ ✓/✗  summary" result line."""
    from rich.text import Text
    icon  = "✓" if success else "✗"
    color = TOOL_OK if success else TOOL_ERR
    t = Text()
    t.append("     └─ ")
    t.append(icon, style=color)
    if summary:
        t.append("  " + summary, style=TOOL_DETAIL)
    return t


def system_message(rich_markup: str) -> "Text":
    """Parse Rich markup into a Text object for system/status messages."""
    return render_rich(rich_markup)


# ── Inspector transcript rendering ────────────────────────────────────────────

def render_message_blocks(
    messages: list[dict],
    label: str,
    *,
    expanded: bool = False,
) -> list[list[tuple[str, str]]]:
    """Render a conversation message list into prompt_toolkit fragment rows.

    Each row is a list of (style, text) tuples; the caller pads and box-wraps.
    Handles three message shapes:
      - role=user   / type=text   → minion prompt prefix + text
      - role=asst   / type=blocks → text blocks + tool_use blocks (⚙ icon)
      - role=user   / type=blocks → tool_result blocks (✓ icon)
    """
    from ..output.display_utils import _trunc, tool_slot_header_frags
    from ..theme import GREEN as _GREEN

    lines: list[list[tuple[str, str]]] = []

    def _line(*frags: tuple[str, str]) -> None:
        lines.append(list(frags))

    for msg in messages:
        role = msg.get("role", "")

        if role == "user" and msg.get("type") == "text":
            text  = msg["text"].replace("\n", " ").strip()
            limit = 400 if expanded else 90
            _line(
                ("class:minion-prefix", " minion ›  "),
                ("class:conv-text",    _trunc(text, limit)),
            )
            _line(("", ""))

        elif role == "assistant" and msg.get("type") == "blocks":
            for blk in msg.get("blocks", []):
                if blk["type"] == "text":
                    txt = blk.get("text", "").replace("\n", " ").strip()
                    if txt:
                        limit = 400 if expanded else 93
                        _line(
                            ("class:inspector-agent", f" {label} ›  "),
                            ("",                      _trunc(txt, limit)),
                        )
                        _line(("", ""))
                elif blk["type"] == "tool_use":
                    name = blk.get("name", "")
                    frags = tool_slot_header_frags(
                        name, blk.get("input", {}),
                        expanded=expanded,
                    )
                    _line(("class:slot-detail", " "), *frags)

        elif role == "user" and msg.get("type") == "blocks":
            for blk in msg.get("blocks", []):
                if blk["type"] == "tool_result":
                    content    = blk.get("content", "")
                    first_line = content.split("\n")[0].strip()
                    limit      = 400 if expanded else 87
                    # Match scrollback structure: ✓ done line + └─ preview line.
                    # Timing is not stored in message blocks, so we omit it.
                    _line((f"bold {_GREEN}", "   ✓  done"))
                    _line(
                        ("class:slot-detail", "   └─  "),
                        ("class:slot-detail", _trunc(first_line, limit)),
                    )
            _line(("", ""))

    return lines
