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

# Single shared console instance used everywhere in the app.
# highlight=False prevents Rich from auto-highlighting numbers/paths in output,
# which can interfere with streamed LLM text.
console = Console(theme=MINION_THEME, highlight=False)


# ─── Static Minion ASCII Art ──────────────────────────────────────────────────
# Generated once, reused on every greeting. Yellow head + blue overalls.
# Intentionally simple so it renders cleanly in any terminal font.

_MINION_HEAD = [
    r"      .-------.      ",
    r"     / O     O \     ",
    r"    |  \_____/  |    ",
    r"    |   \   /   |    ",
    r"    |    ---    |    ",
    r"     \         /     ",
    r"      `-------'      ",
]

_MINION_BODY = [
    r"     .-----------.   ",
    r"     |  .-----.  |   ",
    r"     |  | GRU |  |   ",
    r"     |  '-----'  |   ",
    r"     |___________|   ",
    r"        ||   ||      ",
    r"       _||   ||_     ",
]


def _build_minion_art() -> Text:
    art = Text(justify="center")
    for line in _MINION_HEAD:
        art.append(line + "\n", style=f"bold {YELLOW}")
    for line in _MINION_BODY:
        art.append(line + "\n", style=f"bold {BLUE}")
    return art


# ─── Branded Print Helpers ────────────────────────────────────────────────────

def print_greeting() -> None:
    art = _build_minion_art()
    greeting = Text(justify="center")
    greeting.append("Bello! ", style=f"bold {YELLOW}")
    greeting.append("I'm ", style="white")
    greeting.append("Minion", style=f"bold {BLUE}")
    greeting.append(". What do you want me to do?", style="white")

    content = Text()
    content.append_text(art)
    content.append("\n")
    content.append_text(greeting)
    console.print(Panel(content, border_style=YELLOW, padding=(0, 2)))


def print_error(message: str) -> None:
    console.print(f"[error]Poulet tikka masala![/] {message}")


def print_model_info(provider: str, model: str) -> None:
    console.print(f"[secondary]provider[/] {provider}  [secondary]model[/] {model}")


def print_usage(usage: Optional[LLMResponse]) -> None:
    """Display token usage metadata below a response."""
    if usage is None:
        return
    console.print(
        f"[muted]  ↳ {usage.model}  ·  "
        f"{usage.input_tokens:,} in  /  {usage.output_tokens:,} out[/]"
    )


def stream_response_to_stdout(chunks) -> None:
    """Write streamed text chunks directly to stdout for maximum throughput.

    We bypass Rich's markup processing here because the LLM output is raw text
    that arrives in small chunks — passing each through Rich's renderer would
    add unnecessary overhead and can mangle text that looks like markup tags.
    """
    for chunk in chunks:
        sys.stdout.write(chunk)
        sys.stdout.flush()
