"""Tool display formatters — single source of truth for tool call/result/error/todo markup.

Each function returns a Rich markup string. Callers decide how to render it:
  ConsoleRenderer  → console.print(format_*(…))
  TuiRenderer      → conversation.append_system(format_*(…))

This replaces the four parallel implementations that previously existed in
both theme.py (print_* functions that printed directly) and executor.py
(_*_markup functions that returned strings for TUI routing).
"""

from __future__ import annotations

from rich.markup import escape


def format_tool_call(
    name: str,
    inputs: dict,
    *,
    dry_run: bool = False,
    agent_label: str | None = None,
    mode_badge: str | None = None,
) -> str:
    """Return Rich markup for a tool call header line (and optional block args).

    Scalar / short string values appear inline via the shared tool_slot_header_frags()
    semantic core, converted to Rich markup by frags_to_rich_markup().
    Multiline strings are rendered as an indented block below the header —
    a scrollback-specific concern not needed in single-line slot/inspector contexts.

    mode_badge: None=normal  "edits"=yellow»  "yolo"=⚡  "trusted"=green~
    """
    from ..display_utils import _SKIP_KEYS, frags_to_rich_markup, tool_slot_header_frags
    from ..theme import YELLOW

    label        = "[muted][dry-run][/] " if dry_run else ""
    agent_prefix = f"[muted][{agent_label}][/] " if agent_label else ""

    badge_str = ""
    if mode_badge == "edits":
        badge_str = f" [{YELLOW}]»[/]"
    elif mode_badge == "yolo":
        from ..theme import _TOOL_NAME_COLORS as _tnc
        _yolo_color = _tnc.get(name, "") or YELLOW
        badge_str = f" [{_yolo_color}]⚡[/]"
    elif mode_badge == "trusted":
        badge_str = " [green]~[/]"

    # Split inputs: multiline strings → block display (scrollback has room for them);
    # everything else → inline frags via the shared semantic core.
    # Skip-key filtering (content, old_string, new_string) is handled by tool_slot_header_frags().
    inline_inputs = {k: v for k, v in inputs.items() if not (isinstance(v, str) and "\n" in v)}
    block_pairs   = [(k, v) for k, v in inputs.items()
                     if isinstance(v, str) and "\n" in v and k not in _SKIP_KEYS]

    # Shared semantic core → Rich markup.
    # Frags: [0]=icon, [1]=name, [2:]=key/value pairs (each key frag has leading "  ").
    frags       = tool_slot_header_frags(name, inline_inputs)
    icon_markup = frags_to_rich_markup(frags[:1])   # "⚙  " (bold yellow)
    name_markup = frags_to_rich_markup(frags[1:2])  # tool name (bold + colour)
    args_markup = frags_to_rich_markup(frags[2:])   # "  key=value  key=value" or ""

    header = (
        f"{agent_prefix}{icon_markup}"
        f"{label}{name_markup}{badge_str}"
        + args_markup
    )

    # Block display for multiline values — scrollback-specific, not used in slots/inspector.
    block_lines: list[str] = []
    for k, v in block_pairs:
        n = v.count("\n") + 1
        block_lines.append(f"  [muted]{k} ({n} lines):[/]")
        for line in v.splitlines():
            block_lines.append(f"  [muted]│[/] {escape(line)}")

    if block_lines:
        return header + "\n" + "\n".join(block_lines)
    return header


def format_agent_slot_summary(label: str, task: str, state: dict) -> list[str]:
    """Rich markup lines for an agent slot's scrollback entry (header + status).

    Returns an ordered list of strings, each to be passed to renderer.on_info().
    Used by the TUI scrollback flush after parallel agent execution so the
    committed appearance is consistent with formatter.py's colour palette
    rather than being rebuilt inline in runner code.
    """
    from rich.markup import escape as _e
    from ..theme import GREEN

    task_clean = task.replace("\n", " ").strip()
    if len(task_clean) > 58:
        task_clean = task_clean[:58] + "…"

    lines = [
        f"[#888888]⏺[/]  [bold]\\[{_e(label)}][/]  [#C0C0C0]{_e(task_clean)}[/]"
    ]

    status = state.get("status", "")
    if status == "complete":
        latency = state.get("latency_ms", 0) / 1000
        lines.append(f"   [muted]└─[/]  [bold {GREEN}]done ({latency:.1f}s)[/]")
        preview = state.get("preview", "")
        if preview:
            lines.append(f"       [#C0C0C0]{_e(preview[:100])}[/]")
    elif status == "error":
        error = state.get("error", "unknown error")
        lines.append(f"   [muted]└─[/]  [bold red]Error: {_e(error)}[/]")

    return lines


def format_tool_result(result: str) -> str:
    """Return Rich markup for a successful tool result summary line."""
    first_line  = result.split("\n")[0]
    preview     = escape(first_line[:100]) + ("…" if len(first_line) > 100 else "")
    extra_lines = result.count("\n")
    suffix      = f"  [muted]+{extra_lines} more lines[/]" if extra_lines > 0 else ""
    return f"   [muted]└─[/] {preview}{suffix}"


def format_tool_error(error: str) -> str:
    """Return Rich markup for a tool execution error line."""
    return f"   [bold red]└─ Error:[/] {escape(error)}"


def format_todo_list(*, show_if_all_done: bool = False) -> str:
    """Return Rich markup for the task checklist, or '' if nothing to show.

    The returned string starts with a blank line so callers can pass it
    directly to console.print() or append_system() without extra spacing logic.
    """
    from ..tools.implementations import get_todo_list

    items = get_todo_list()
    if not items:
        return ""
    if not show_if_all_done and all(i["status"] == "done" for i in items):
        return ""

    lines = ["", " [bold dim]Tasks[/]"]
    for item in items:
        status = item["status"]
        text   = item["text"]
        if status == "done":
            lines.append(f"  [green]✓[/]  [dim]{text}[/]")
        elif status == "in_progress":
            lines.append(f"  [yellow]→[/]  {text}")
        else:
            lines.append(f"  [dim]○  {text}[/]")
    return "\n".join(lines)
