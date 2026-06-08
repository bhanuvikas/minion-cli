"""All print_* helpers, MarkdownStreamer, and stream_response_to_stdout."""

import sys
from typing import TYPE_CHECKING, Optional

from rich.panel import Panel
from rich.text import Text

from .palette import BLUE, GREEN, GREY, SILVER, YELLOW
from .console import console
from ..output.diff import format_diff_rich

if TYPE_CHECKING:
    from ..llm.conversation import ContextSnapshot


def print_error(message: str) -> None:
    console.print(f"[error]Poulet tikka masala![/] {message}")


def print_model_info(provider: str, model: str) -> None:
    console.print(f"[secondary]provider[/] {provider}  [secondary]model[/] {model}")


def print_todo_list(show_if_all_done: bool = False) -> None:
    """Print compact task checklist."""
    from ..output.formatter import format_todo_list
    markup = format_todo_list(show_if_all_done=show_if_all_done)
    if markup:
        console.print(markup)


def print_usage(snapshot: "Optional[ContextSnapshot]", active_mode: "Optional[str]" = None) -> None:  # type: ignore[name-defined]
    """Footer line shown after every response.

    Format: model · N in / N out · context: X/Y (Z%) · session total: T
    Cache hits are shown as a suffix on 'in' when present.
    active_mode: None | "edits" | "yolo" — appends a badge when a mode is active.
    """
    if snapshot is None:
        return
    console.print()
    cache_suffix = ""
    if snapshot.cache_read_tokens > 0:
        cache_suffix = f" [dim]({snapshot.cache_read_tokens:,} cached)[/dim]"
    mode_badge = ""
    if active_mode == "edits":
        mode_badge = f"  [{YELLOW}]» edits[/]"
    elif active_mode == "yolo":
        mode_badge = f"  [red]⚡ yolo[/]"
    console.print(
        f"[muted]  ↳ {snapshot.model}  ·  "
        f"{snapshot.input_tokens:,} in{cache_suffix} / {snapshot.output_tokens:,} out  ·  "
        f"context: {snapshot.current_context_tokens:,}/{snapshot.context_limit:,} "
        f"({snapshot.context_pct:.1f}%)  ·  "
        f"billed: {snapshot.session_total:,}[/]{mode_badge}"
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
        if snapshot.cache_read_tokens > 0 or snapshot.cache_creation_tokens > 0:
            uncached = snapshot.input_tokens - snapshot.cache_read_tokens - snapshot.cache_creation_tokens
            console.print(
                f"  [muted]  └─ {uncached:,} uncached · "
                f"{snapshot.cache_read_tokens:,} cache read · "
                f"{snapshot.cache_creation_tokens:,} cache write[/]"
            )

    # ── Session ────────────────────────────────────────────────────────────────
    turn_word = "turn" if snapshot.turn_count == 1 else "turns"
    console.print(
        f"  Billed (cumulative): [{YELLOW}]{snapshot.session_total:,}[/] tokens  "
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


class MarkdownStreamer:
    """Context manager that streams LLM text as plain text, then commits as markdown on close.

    During streaming a transient Live area shows plain text (stable, linear height growth).
    On close the transient area is cleared and the full accumulated content is printed as
    rendered Markdown in one committed console.print() call.

    This avoids the Rich Live cursor-arithmetic bug where non-linear height changes from
    partially-parsed markdown tables cause prior content to be overwritten mid-stream.
    """

    def __init__(self, display_name: str = "minion") -> None:
        self._buffer: list[str] = []
        self._display_name = display_name
        self._live: Optional["Live"] = None  # type: ignore[name-defined]
        self._entered = False

    def __enter__(self) -> "MarkdownStreamer":
        from rich.live import Live
        from rich.text import Text
        console.print(f"\n[bold {BLUE}]{self._display_name}[/] ›")
        self._live = Live(
            Text(""),
            console=console,
            refresh_per_second=12,
            vertical_overflow="visible",
            transient=True,  # cleared on exit; final markdown committed via console.print
        )
        self._live.__enter__()  # type: ignore[union-attr]
        self._entered = True
        return self

    def write(self, text: str) -> None:
        if not self._entered:
            return
        self._buffer.append(text)
        from rich.text import Text
        # Plain Text height grows linearly (one line per \n) — no cursor-math surprises
        self._live.update(Text("".join(self._buffer)))  # type: ignore[union-attr]

    def close(self) -> None:
        """Exit the transient live area, then commit the final markdown render."""
        if not self._entered:
            return
        self._live.__exit__(None, None, None)  # type: ignore[union-attr]  # clears transient area
        self._entered = False
        if self._buffer:
            from rich.markdown import Markdown
            console.print(Markdown("".join(self._buffer)))

    def __exit__(self, *args: object) -> None:
        self.close()


def print_mode_toggle(mode: str, enabled: bool) -> None:
    """Print a one-line status message when /edits or /yolo is toggled."""
    if mode == "edits":
        if enabled:
            console.print(f"  [{YELLOW}]»  edits mode on[/] [muted]— write_file and edit_file auto-approved[/]")
        else:
            console.print(f"  [muted]»  edits mode off[/]")
    elif mode == "yolo":
        if enabled:
            console.print(f"  [red]⚡  yolo mode on[/] [muted]— all tools auto-approved, stay sharp[/]")
        else:
            console.print(f"  [muted]⚡  yolo mode off[/]")


def print_tool_call(name: str, inputs: dict, dry_run: bool = False, agent_label: str | None = None, mode_badge: str | None = None) -> None:
    """Display a tool call the agent is about to make."""
    from ..output.formatter import format_tool_call
    console.print(format_tool_call(name, inputs, dry_run=dry_run, agent_label=agent_label, mode_badge=mode_badge))


def print_trust_saved(tool: str, patterns: list[str], scope: str) -> None:
    """Print a confirmation line after saving a trust rule."""
    scope_labels = {
        "session": "session",
        "project": "project (.minion/permissions.toml)",
        "global": f"global (~/.minion/permissions.toml)",
    }
    scope_label = scope_labels.get(scope, scope)
    for p in patterns:
        console.print(
            f"   [green]~[/] [muted]saved:[/] [{YELLOW}]{tool}[/]"
            f" [muted]{p!r} ({scope_label})[/]"
        )


def print_tool_result(result: str) -> None:
    """Display a compact summary of the tool result (first line, truncated)."""
    from ..output.formatter import format_tool_result
    console.print(format_tool_result(result))


def print_tool_error(error: str) -> None:
    """Display a tool execution error."""
    from ..output.formatter import format_tool_error
    console.print(format_tool_error(error))


def print_iteration_limit(max_iter: int) -> None:
    """Displayed when the agent loop hits MAX_ITERATIONS without finishing."""
    print_error(f"Reached iteration limit ({max_iter}) without a final response.")


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
    from ..llm.reflection import SCORE_THRESHOLD
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
