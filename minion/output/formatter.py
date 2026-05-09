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

    Scalar / short string values appear inline.
    Multiline strings are rendered as an indented block below the header.
    Content keys for write_file/edit_file are suppressed — the diff preview
    already shows that information.

    mode_badge: None=normal  "edits"=yellow»  "yolo"=⚡  "trusted"=green~
    """
    from ..theme import BLUE, YELLOW, _TOOL_NAME_COLORS

    label        = "[muted][dry-run][/] " if dry_run else ""
    agent_prefix = f"[muted][{agent_label}][/] " if agent_label else ""
    name_color   = _TOOL_NAME_COLORS.get(name, "")
    name_style   = f"bold {name_color}".strip()  # "bold" when no color entry

    badge_str = ""
    if mode_badge == "edits":
        badge_str = f" [{YELLOW}]»[/]"
    elif mode_badge == "yolo":
        badge_str = f" [{name_color or YELLOW}]⚡[/]"
    elif mode_badge == "trusted":
        badge_str = " [green]~[/]"

    inline_args: list[str] = []
    block_lines: list[str] = []

    for k, v in inputs.items():
        if name == "write_file" and k == "content":
            continue
        if name == "edit_file" and k in ("old_string", "new_string"):
            continue
        if isinstance(v, str) and "\n" in v:
            n = v.count("\n") + 1
            block_lines.append(f"  [muted]{k} ({n} lines):[/]")
            for line in v.splitlines():
                block_lines.append(f"  [muted]│[/] {escape(line)}")
        elif isinstance(v, str) and len(v) > 60:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]\"{escape(v[:50])}…\"[/]")
        else:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]{v!r}[/]")

    header = (
        f"{agent_prefix}[bold {YELLOW}]⚙[/]  "
        f"{label}[{name_style}]{name}[/]{badge_str}"
        f"  {'  '.join(inline_args)}"
    )
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
