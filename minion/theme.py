import sys
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from .llm.base import LLMResponse

# ─── Minion Colour Palette ────────────────────────────────────────────────────
YELLOW = "#FFD700"
BLUE = "#1E90FF"
DENIM = "#2F4F8F"
GREY = "#888888"

MINION_THEME = Theme(
    {
        "primary": YELLOW,
        "secondary": BLUE,
        "accent": DENIM,
        "muted": GREY,
        "error": "bold red",
        "success": "bold green",
        "prompt": f"bold {YELLOW}",
    }
)

console = Console(theme=MINION_THEME, highlight=False)


# ─── Figlet Title ─────────────────────────────────────────────────────────────
# "MINION" rendered in figlet ASCII art with alternating yellow/blue per letter.
# Colors mirror the character: yellow body, blue overalls — M I N I O N.
# We render each letter separately so each gets its own Rich color style.

# m=yellow  i=yellow  n=blue  i=yellow  o=yellow  n=blue
_LETTER_COLORS = [YELLOW, YELLOW, BLUE, YELLOW, YELLOW, BLUE]
_FIGLET_FONT = "big"  # upright block letters, no diagonal slant


def _build_title() -> Text:
    try:
        import pyfiglet
    except ImportError:
        # Graceful fallback if pyfiglet isn't installed
        t = Text(justify="center")
        for i, ch in enumerate("minion"):
            t.append(ch, style=_LETTER_COLORS[i])
        t.append("\n")
        return t

    # Render each letter individually so we can apply a per-letter color.
    # pyfiglet renders monospace rows; combining row-by-row places letters
    # side-by-side naturally — no manual width calculation needed.
    letter_lines: list[list[str]] = []
    for letter in "minion":
        raw = pyfiglet.figlet_format(letter, font=_FIGLET_FONT)
        lines = raw.splitlines()
        # Strip trailing empty lines so all letters normalize to the same height
        while lines and not lines[-1].strip():
            lines.pop()
        letter_lines.append(lines)

    max_height = max(len(ls) for ls in letter_lines)

    # Pad any shorter letter to max_height with blank rows of matching width
    for ls in letter_lines:
        width = max((len(l) for l in ls), default=0)
        while len(ls) < max_height:
            ls.append(" " * width)

    title = Text(justify="center")
    for row in range(max_height):
        for ls, color in zip(letter_lines, _LETTER_COLORS):
            title.append(ls[row], style=color)
        title.append("\n")

    return title


# ─── Branded Print Helpers ────────────────────────────────────────────────────

def print_greeting(version: str = "") -> None:
    from . import __version__

    art = _build_title()

    greeting = Text(justify="center")
    greeting.append("Bello! ", style=f"bold {YELLOW}")
    greeting.append("I'm ", style="white")
    greeting.append("Minion", style=f"bold {BLUE}")
    greeting.append(". What do you want me to do?", style="white")

    content = Text()
    content.append_text(art)
    content.append("\n")
    content.append_text(greeting)

    panel_title = (
        f"[bold {YELLOW}]minion-cli[/] "
        f"[{GREY}]v{version or __version__}[/]"
    )
    console.print(
        Panel(
            content,
            title=panel_title,
            title_align="left",
            border_style=YELLOW,
            padding=(0, 2),
            expand=False,   # size to content, don't stretch to terminal width
        )
    )


def print_error(message: str) -> None:
    console.print(f"[error]Poulet tikka masala![/] {message}")


def print_model_info(provider: str, model: str) -> None:
    console.print(f"[secondary]provider[/] {provider}  [secondary]model[/] {model}")


def print_usage(usage: Optional[LLMResponse], session_total: Optional[int] = None) -> None:
    """Display token usage on its own line with a leading blank line for breathing room.

    session_total: cumulative (input + output) tokens across all turns this session.
                   Only shown in REPL mode where a Conversation is active.
    """
    if usage is None:
        return
    console.print()
    line = (
        f"[muted]  ↳ {usage.model}  ·  "
        f"{usage.input_tokens:,} in  /  {usage.output_tokens:,} out"
    )
    if session_total is not None:
        line += f"  ·  session: {session_total:,}"
    line += "[/]"
    console.print(line)


def stream_response_to_stdout(chunks) -> None:
    """Write streamed chunks directly to stdout, bypassing Rich's markup scanner."""
    for chunk in chunks:
        sys.stdout.write(chunk)
        sys.stdout.flush()
