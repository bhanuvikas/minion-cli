import sys

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

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


# ─── Branded Print Helpers ────────────────────────────────────────────────────

def print_greeting() -> None:
    text = Text()
    text.append("Bello! ", style=f"bold {YELLOW}")
    text.append("I'm ", style="white")
    text.append("Minion", style=f"bold {BLUE}")
    text.append(". What do you want me to do?", style="white")
    console.print(Panel(text, border_style=YELLOW, padding=(0, 2)))


def print_error(message: str) -> None:
    console.print(f"[error]Poulet tikka masala![/] {message}")


def print_model_info(provider: str, model: str) -> None:
    console.print(f"[secondary]provider[/] {provider}  [secondary]model[/] {model}")


def stream_response_to_stdout(chunks) -> None:
    """Write streamed text chunks directly to stdout for maximum throughput.

    We bypass Rich's markup processing here because the LLM output is raw text
    that arrives in small chunks — passing each through Rich's renderer would
    add unnecessary overhead and can mangle text that looks like markup tags.
    """
    for chunk in chunks:
        sys.stdout.write(chunk)
        sys.stdout.flush()
