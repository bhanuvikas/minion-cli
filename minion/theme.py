import sys
from typing import TYPE_CHECKING, Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from .diff import format_diff_rich
from .llm.base import LLMResponse

if TYPE_CHECKING:
    from .conversation import ContextSnapshot

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


def print_usage(snapshot: "Optional[ContextSnapshot]") -> None:  # type: ignore[name-defined]
    """Footer line shown after every response.

    Format: model · N in / N out · context: X/Y (Z%) · session total: T
    """
    if snapshot is None:
        return
    console.print()
    console.print(
        f"[muted]  ↳ {snapshot.model}  ·  "
        f"{snapshot.input_tokens:,} in / {snapshot.output_tokens:,} out  ·  "
        f"context: {snapshot.current_context_tokens:,}/{snapshot.context_limit:,} "
        f"({snapshot.context_pct:.1f}%)  ·  "
        f"session total: {snapshot.session_total:,}[/]"
    )


def print_context(snapshot: "Optional[ContextSnapshot]") -> None:  # type: ignore[name-defined]
    """Rich context breakdown displayed by the /context slash command."""
    console.print()

    if snapshot is None:
        console.print(f"[muted]  No context data yet — start a conversation first.[/]")
        console.print()
        return

    # ── Bar chart ──────────────────────────────────────────────────────────────
    pct = snapshot.context_pct
    bar_width = 28
    filled = round(bar_width * pct / 100)
    bar = f"[{BLUE}]" + "█" * filled + "[/]" + f"[muted]" + "░" * (bar_width - filled) + "[/]"

    console.print(f"  [bold {YELLOW}]Context — {snapshot.model}[/]")
    console.print(f"  {bar}  {pct:.1f}%")
    console.print(
        f"  [muted]{snapshot.current_context_tokens:,} / {snapshot.context_limit:,} tokens[/]"
    )
    console.print()

    # ── This turn ──────────────────────────────────────────────────────────────
    if snapshot.input_tokens == 0:
        console.print(f"  [muted]History cleared — context is fresh.[/]")
    else:
        console.print(
            f"  This turn:     [{YELLOW}]{snapshot.input_tokens:,}[/] in  /  "
            f"[{YELLOW}]{snapshot.output_tokens:,}[/] out"
        )

    # ── Session ────────────────────────────────────────────────────────────────
    turn_word = "turn" if snapshot.turn_count == 1 else "turns"
    console.print(
        f"  Session total: [{YELLOW}]{snapshot.session_total:,}[/] tokens  "
        f"[muted]({snapshot.turn_count} {turn_word})[/]"
    )

    # ── Breakdown ──────────────────────────────────────────────────────────────
    if snapshot.input_tokens > 0 and snapshot.system_prompt_tokens > 0:
        console.print()
        console.print(f"  [muted]Breakdown (approximate):[/]")
        sys_pct = snapshot.system_prompt_tokens / snapshot.context_limit * 100
        msg_pct = snapshot.message_tokens / snapshot.context_limit * 100
        console.print(
            f"  [muted]  System prompt:  ~{snapshot.system_prompt_tokens:,} tokens  "
            f"({sys_pct:.1f}%)[/]"
        )
        if snapshot.memory_tokens > 0:
            mem_pct = snapshot.memory_tokens / snapshot.context_limit * 100
            console.print(
                f"  [muted]  Memory:         ~{snapshot.memory_tokens:,} tokens  "
                f"({mem_pct:.1f}%)[/]"
            )
        console.print(
            f"  [muted]  Messages:       ~{snapshot.message_tokens:,} tokens  "
            f"({msg_pct:.1f}%)[/]"
        )

    console.print()


def stream_response_to_stdout(chunks) -> None:
    """Write streamed chunks directly to stdout, bypassing Rich's markup scanner."""
    for chunk in chunks:
        sys.stdout.write(chunk)
        sys.stdout.flush()


# ─── Tool Use Display (Phase 3) ───────────────────────────────────────────────

def print_tool_call(name: str, inputs: dict, dry_run: bool = False) -> None:
    """Display a tool call the agent is about to make.

    Scalar and short string values appear inline. Multiline strings (e.g. the
    content argument of write_file) are printed as an indented block below the
    header line so the user can review the full content before confirming.
    """
    label = f"[muted][dry-run][/] " if dry_run else ""

    inline_args = []
    block_args = []  # (key, value) pairs that need a separate block

    for k, v in inputs.items():
        if isinstance(v, str) and "\n" in v:
            block_args.append((k, v))  # shown below, not inline
        elif isinstance(v, str) and len(v) > 60:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]\"{v[:50]}…\"[/]")
        else:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]{v!r}[/]")

    console.print(f"[bold {YELLOW}]⚙[/]  {label}[bold]{name}[/]  {'  '.join(inline_args)}")

    for k, v in block_args:
        lines = v.count("\n") + 1
        console.print(f"  [muted]{k} ({lines} lines):[/]")
        for line in v.splitlines():
            console.print(f"  [muted]│[/] {line}")


def print_tool_result(result: str) -> None:
    """Display a compact summary of the tool result (first line, truncated)."""
    from rich.markup import escape
    first_line = result.split("\n")[0]
    preview = escape(first_line[:100]) + ("…" if len(first_line) > 100 else "")
    extra_lines = result.count("\n")
    suffix = f"  [muted]+{extra_lines} more lines[/]" if extra_lines > 0 else ""
    console.print(f"  [muted]└─[/] {preview}{suffix}")


def print_tool_error(error: str) -> None:
    """Display a tool execution error."""
    console.print(f"  [bold red]└─ Error:[/] {error}")


def print_iteration_limit(max_iter: int) -> None:
    """Displayed when the agent loop hits MAX_ITERATIONS without finishing."""
    print_error(f"Reached iteration limit ({max_iter}) without a final response.")


# ─── Reflection Display (Phase 5) ────────────────────────────────────────────

def print_reflection_header(round_num: int, max_rounds: int) -> None:
    """Show a muted status line when the reflection loop begins."""
    console.print(f"\n[muted]  ↻ Reflecting (round {round_num}/{max_rounds})...[/]")


def print_critique(score: int, response_type: str, critique_text: str) -> None:
    """Display a critique result in a styled panel.

    Score colour:
      green  — score >= SCORE_THRESHOLD (passed)
      yellow — score 5-6 (marginal)
      red    — score < 5 (poor)

    Only called when verbose=True.
    """
    from .reflection import SCORE_THRESHOLD
    from rich.markup import escape

    if score >= SCORE_THRESHOLD:
        score_color = "green"
    elif score >= 5:
        score_color = f"{YELLOW}"
    else:
        score_color = "red"

    header = (
        f"[bold {score_color}]Score: {score}/10[/]  "
        f"[muted]Type: {response_type}[/]"
    )
    body = escape(critique_text)
    console.print(
        Panel(
            f"{header}\n\n{body}",
            title=f"[muted]critique[/]",
            title_align="left",
            border_style=GREY,
            padding=(0, 1),
            expand=False,
        )
    )


def print_diff(original: str, revised: str) -> None:
    """Compute and print a unified diff between original and revised response.

    No-ops when the strings are identical.
    Only called when verbose=True and was_refined=True.
    """
    markup = format_diff_rich(original, revised)
    if not markup:
        return
    console.print(f"\n[muted]  ── diff ──────────────────────────────────────[/]")
    console.print(markup)
    console.print(f"[muted]  ─────────────────────────────────────────────[/]")
