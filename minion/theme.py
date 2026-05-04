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

def print_greeting(
    version: str = "",
    model: str = "",
    provider: str = "",
    project_name: str = "",
    cwd: str = "",
    agent_count: int = 0,
    memory_enabled: bool = True,
    mcp_count: int = 0,
    minion_md: bool = False,
    a2a_count: int = 0,
) -> None:
    from . import __version__
    from pathlib import Path
    from rich.align import Align
    from rich.rule import Rule
    from rich.table import Table

    term_w = console.size.width
    SEP_W  = 3  # │ column fixed width

    # Column widths that match the outer grid (ratio=50 / ratio=50).
    # Use integer division so dots/truncation never exceed the cell width.
    left_w  = max(30, (term_w - SEP_W) // 2)
    right_w = max(20, term_w - SEP_W - left_w)

    # ── Logo (Align centers the block as a unit) ───────────────────────────────
    art = _build_title()
    art.justify = None
    console.print()
    console.print(Align(art, align="center"))

    # ── Greeting ──────────────────────────────────────────────────────────────
    greeting = Text()
    greeting.append("Bello! ", style=f"bold {YELLOW}")
    greeting.append("I'm ", style="white")
    greeting.append("Minion", style=f"bold {BLUE}")
    greeting.append(". What do you want me to do?", style="white")
    console.print(Align(greeting, align="center"))
    console.print()

    # ── Top rule ──────────────────────────────────────────────────────────────
    console.print(Rule(style=GREY))

    # ── Dot-line helpers (stay 1 char short of column width for safety) ───────
    dots_cmd  = (". " * (left_w  // 2))[:left_w  - 1]
    dots_sess = (". " * (right_w // 2))[:right_w - 3]  # -2 indent, -1 safety

    # ── Commands column ───────────────────────────────────────────────────────
    _BANNER_COMMANDS = [
        ("/help",    "show all commands"),
        ("/plan",    "create a step-by-step plan"),
        ("/compact", "summarise conversation"),
        ("/yolo",    "auto-approve all tools"),
        ("/model",   "switch provider or model"),
        ("/context", "show context window usage"),
        ("/clear",   "wipe conversation history"),
        ("/reflect", "enable self-critique mode"),
        ("/save",    "save current session"),
        ("/quit",    "exit Minion"),
    ]
    _CMD_KEY_W = 10  # /compact (8) + 2 trailing spaces
    _max_desc  = max(10, left_w - _CMD_KEY_W - 1)

    cmd_text = Text()
    cmd_text.append(f"{'command':<{_CMD_KEY_W}}", style=f"bold {YELLOW}")
    cmd_text.append("description\n", style=GREY)
    cmd_text.append(dots_cmd + "\n", style=f"dim {GREY}")
    for i, (cmd, desc) in enumerate(_BANNER_COMMANDS):
        cmd_text.append(f"{cmd:<{_CMD_KEY_W}}", style=f"bold {YELLOW}")
        desc_out = desc if len(desc) <= _max_desc else desc[:_max_desc - 1] + "…"
        suffix = "\n" if i < len(_BANNER_COMMANDS) - 1 else ""
        cmd_text.append(desc_out + suffix, style="white")

    # ── Session column ────────────────────────────────────────────────────────
    _max_val = max(8, right_w - 11 - 1)  # 11 = "  " + 9-char key; -1 safety

    def _sv(s: str) -> str:
        return s if len(s) <= _max_val else s[:_max_val - 1] + "…"

    sess_rows: list[tuple[str, str, str]] = []  # (key, value, value_style)
    ver = version or __version__
    sess_rows.append(("version", _sv(f"v{ver}"), "white"))
    if model:
        sess_rows.append(("model", _sv(model), BLUE))
    if provider:
        sess_rows.append(("provider", _sv(provider), "white"))
    if project_name:
        sess_rows.append(("project", _sv(project_name), "white"))
    if minion_md:
        sess_rows.append(("config", "MINION.md", f"dim {GREY}"))
    if a2a_count > 0:
        lbl = "1 remote agent" if a2a_count == 1 else f"{a2a_count} remote agents"
        sess_rows.append(("a2a", _sv(lbl), "white"))
    if cwd:
        cwd_display = cwd
        home = str(Path.home())
        if cwd_display.startswith(home):
            cwd_display = "~" + cwd_display[len(home):]
        if len(cwd_display) > _max_val:
            cwd_display = "…" + cwd_display[-(_max_val - 1):]
        sess_rows.append(("cwd", cwd_display, "white"))
    if agent_count > 0:
        lbl = "1 role loaded" if agent_count == 1 else f"{agent_count} roles loaded"
        sess_rows.append(("agents", _sv(lbl), "white"))
    mem_val   = "enabled"  if memory_enabled else "disabled"
    mem_style = "green"    if memory_enabled else f"dim {GREY}"
    sess_rows.append(("memory", mem_val, mem_style))
    if mcp_count > 0:
        lbl = "1 server active" if mcp_count == 1 else f"{mcp_count} servers active"
        sess_rows.append(("mcp", _sv(lbl), "white"))

    sess_text = Text()
    sess_text.append("  session\n", style=f"bold {YELLOW}")
    sess_text.append(f"  {dots_sess}\n", style=f"dim {GREY}")
    for i, (key, val, val_style) in enumerate(sess_rows):
        sess_text.append(f"  {key:<9}", style=GREY)
        suffix = "\n" if i < len(sess_rows) - 1 else ""
        sess_text.append(val + suffix, style=val_style)

    # ── Separator ─────────────────────────────────────────────────────────────
    # cmd_text: header + dots + N commands (no blank line after dots)
    n_sep = max(2 + len(_BANNER_COMMANDS), 2 + len(sess_rows))
    sep_text = Text("\n".join(["│"] * n_sep), style=f"dim {GREY}", justify="center")

    # ── Outer layout ──────────────────────────────────────────────────────────
    outer = Table.grid(expand=True)
    outer.add_column(ratio=50)
    outer.add_column(width=SEP_W, justify="center")
    outer.add_column(ratio=50)
    outer.add_row(cmd_text, sep_text, sess_text)

    console.print(outer)
    console.print()
    console.print(Rule(style=GREY))
    console.print()


def print_error(message: str) -> None:
    console.print(f"[error]Poulet tikka masala![/] {message}")


def print_model_info(provider: str, model: str) -> None:
    console.print(f"[secondary]provider[/] {provider}  [secondary]model[/] {model}")


def print_todo_list(show_if_all_done: bool = False) -> None:
    """Print compact task checklist. Called inline after todo_write and at end of turn.

    show_if_all_done=True: show even when all items are done (used for inline display
    so the user sees the final ✓ state before the model clears the list).
    show_if_all_done=False (default): auto-hide when all done (end-of-turn display
    avoids showing a completed list on subsequent turns).
    """
    from .tools.implementations import get_todo_list
    items = get_todo_list()
    if not items:
        return
    if not show_if_all_done and all(i["status"] == "done" for i in items):
        return
    console.print()
    console.print(" [bold dim]Tasks[/]")
    for item in items:
        status = item["status"]
        text   = item["text"]
        if status == "done":
            console.print(f"  [green]✓[/]  [dim]{text}[/]")
        elif status == "in_progress":
            console.print(f"  [yellow]→[/]  {text}")
        else:
            console.print(f"  [dim]○  {text}[/]")


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
    """Context manager that renders LLM streaming text as live markdown.

    Usage: call write() for each incoming text chunk. The Live display updates
    on newline boundaries to keep re-parsing overhead low. Call close() (or use
    as a context manager via with-statement) to finalise and exit the Live area.
    """

    def __init__(self, display_name: str = "minion") -> None:
        self._buffer: list[str] = []
        self._display_name = display_name
        self._live: Optional["Live"] = None  # type: ignore[name-defined]
        self._entered = False

    def __enter__(self) -> "MarkdownStreamer":
        from rich.live import Live
        from rich.markdown import Markdown
        console.print(f"\n[bold {BLUE}]{self._display_name}[/] ›")
        self._live = Live(
            Markdown(""),
            console=console,
            refresh_per_second=12,
            vertical_overflow="visible",
            transient=False,
        )
        self._live.__enter__()
        self._entered = True
        return self

    def write(self, text: str) -> None:
        if not self._entered:
            return
        self._buffer.append(text)
        from rich.markdown import Markdown
        self._live.update(Markdown("".join(self._buffer)))  # type: ignore[union-attr]

    def close(self) -> None:
        """Finalise and exit the Live context. Safe to call if never entered."""
        if not self._entered:
            return
        if self._buffer:
            from rich.markdown import Markdown
            self._live.update(Markdown("".join(self._buffer)), refresh=True)  # type: ignore[union-attr]
        self._live.__exit__(None, None, None)  # type: ignore[union-attr]
        self._entered = False

    def __exit__(self, *args: object) -> None:
        self.close()


# ─── Tool Use Display (Phase 3) ───────────────────────────────────────────────

_TOOL_NAME_COLORS: dict[str, str] = {
    "write_file": YELLOW,
    "edit_file":  YELLOW,
    "run_shell":  "red",
    "web_fetch":  "red",
}


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
    """Display a tool call the agent is about to make.

    Scalar and short string values appear inline. Multiline strings (e.g. the
    content argument of write_file) are printed as an indented block below the
    header line so the user can review the full content before confirming.

    mode_badge: None = normal, "edits" = yellow » suffix, "yolo" = color ⚡ suffix
    """
    label = f"[muted][dry-run][/] " if dry_run else ""
    agent_prefix = f"[muted][{agent_label}][/] " if agent_label else ""
    name_color = _TOOL_NAME_COLORS.get(name, "bold")
    badge_str = ""
    if mode_badge == "edits":
        badge_str = f" [{YELLOW}]»[/]"
    elif mode_badge == "yolo":
        badge_str = f" [{name_color}]⚡[/]"
    elif mode_badge == "trusted":
        badge_str = " [green]~[/]"

    inline_args = []
    block_args = []  # (key, value) pairs that need a separate block

    for k, v in inputs.items():
        # write_file/edit_file: suppress content fields from inline display —
        # the confirmation prompt already shows a diff, so this would be redundant.
        if name == "write_file" and k == "content":
            continue
        if name == "edit_file" and k in ("old_string", "new_string"):
            continue
        if isinstance(v, str) and "\n" in v:
            block_args.append((k, v))  # shown below, not inline
        elif isinstance(v, str) and len(v) > 60:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]\"{v[:50]}…\"[/]")
        else:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]{v!r}[/]")

    console.print(f"{agent_prefix}[bold {YELLOW}]⚙[/]  {label}[{name_color}]{name}[/]{badge_str}  {'  '.join(inline_args)}")

    for k, v in block_args:
        lines = v.count("\n") + 1
        console.print(f"  [muted]{k} ({lines} lines):[/]")
        for line in v.splitlines():
            console.print(f"  [muted]│[/] {line}")


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


# ─── Spinner coordination (A2A remote wait) ──────────────────────────────────
# Allows nested code (e.g. A2A client) to pause the spinner before showing an
# interactive questionary prompt, then resume it after the user answers.

_active_status = None  # Rich Status object, set by executor while polling


def set_active_status(status) -> None:
    global _active_status
    _active_status = status


def pause_spinner() -> None:
    """Stop the active spinner so interactive prompts can render cleanly."""
    if _active_status is not None:
        _active_status.stop()


def resume_spinner() -> None:
    """Restart the active spinner after an interactive prompt completes."""
    if _active_status is not None:
        _active_status.start()


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
