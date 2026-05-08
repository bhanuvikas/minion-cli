"""Tool executor — dispatches tool calls, handles confirmation and dry-run.

Single responsibility: given a ToolUseBlock from the model, decide whether to
execute it (dry-run check, confirmation for dangerous tools), dispatch to the
right implementation, and surface the result via theme helpers.

Keeps UX concerns (confirmation prompts, display) out of implementations.py,
and keeps business-logic concerns (which tools are dangerous, dispatch table)
out of runner.py.
"""

import asyncio
import contextlib
import sys
import threading
import time as _time_exec
from pathlib import Path
from typing import Optional

import questionary

from ..config import MINION_STYLE
from ..permissions import PermissionStore, split_compound, suggest_patterns_for_tool

# Serializes questionary prompts across threads (sync path only).
_CONFIRM_LOCK = threading.Lock()


def _flush_stdin() -> None:
    """Drain any buffered stdin keystrokes before showing a confirmation prompt.

    Prevents Enter presses made during the preceding spinner (e.g. while the
    model was streaming tool-call JSON) from being consumed by questionary and
    auto-declining the prompt without the user seeing it.
    """
    try:
        import termios
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass  # Windows or non-tty — best-effort only


from ..llm.base import ToolUseBlock
from ..output import ConsoleRenderer, OutputRenderer
from ..theme import console, print_trust_saved
from ..tracing import get_tracer
from .definitions import DANGEROUS_TOOLS
from .implementations import (
    _apply_edit,
    edit_file,
    get_file_outline,
    glob,
    list_directory,
    read_file,
    run_shell,
    search_file,
    todo_read,
    todo_write,
    web_fetch,
    write_file,
)

TOOL_SPINNER_LABELS: dict[str, str] = {
    "write_file":       "[muted]writing...[/]",
    "edit_file":        "[muted]editing...[/]",
    "run_shell":        "[muted]running...[/]",
    "read_file":        "[muted]reading...[/]",
    "list_directory":   "[muted]listing...[/]",
    "search_file":      "[muted]searching...[/]",
    "glob":             "[muted]searching...[/]",
    "web_fetch":        "[muted]fetching...[/]",
    "get_file_outline": "[muted]analyzing...[/]",
    "spawn_agent":      "[muted]planning task...[/]",
    "send_remote_task": "[muted]planning task...[/]",
    "todo_write":       "[muted]updating tasks...[/]",
    "todo_read":        "[muted]reading tasks...[/]",
}

def _tool_call_markup(name: str, inputs: dict, dry_run: bool, agent_label: str | None, mode_badge: str | None) -> str:
    """Rich markup matching print_tool_call() output, routed through run_in_terminal in TUI."""
    from ..theme import YELLOW, BLUE, _TOOL_NAME_COLORS
    label = "[muted][dry-run][/] " if dry_run else ""
    agent_prefix = f"[muted][{agent_label}][/] " if agent_label else ""
    name_color = _TOOL_NAME_COLORS.get(name, "")
    name_style = f"bold {name_color}".strip()
    badge_str = ""
    if mode_badge == "edits":
        badge_str = f" [{YELLOW}]»[/]"
    elif mode_badge == "yolo":
        badge_str = f" [{name_color or YELLOW}]⚡[/]"
    elif mode_badge == "trusted":
        badge_str = " [green]~[/]"

    inline_args = []
    block_lines = []
    for k, v in inputs.items():
        if name == "write_file" and k == "content":
            continue
        if name == "edit_file" and k in ("old_string", "new_string"):
            continue
        if isinstance(v, str) and "\n" in v:
            n = v.count("\n") + 1
            block_lines.append(f"  [muted]{k} ({n} lines):[/]")
            for line in v.splitlines():
                block_lines.append(f"  [muted]│[/] {line}")
        elif isinstance(v, str) and len(v) > 60:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]\"{v[:50]}…\"[/]")
        else:
            inline_args.append(f"[muted]{k}=[/][{BLUE}]{v!r}[/]")

    header = f"{agent_prefix}[#888888]⚙[/]  {label}[{name_style}]{name}[/]{badge_str}  {'  '.join(inline_args)}"
    if block_lines:
        return header + "\n" + "\n".join(block_lines)
    return header


def _tool_result_markup(result: str) -> str:
    """Rich markup matching print_tool_result() output, routed through run_in_terminal in TUI."""
    from rich.markup import escape
    if result.startswith("Error:") or result == "User declined tool execution.":
        error_text = result[7:].strip() if result.startswith("Error:") else result
        return f"   [bold red]└─ Error:[/] {escape(error_text)}"
    first_line = result.split("\n")[0]
    preview = escape(first_line[:100]) + ("…" if len(first_line) > 100 else "")
    extra_lines = result.count("\n")
    suffix = f"  [muted]+{extra_lines} more lines[/]" if extra_lines > 0 else ""
    return f"   [muted]└─[/] {preview}{suffix}"


def _err_markup(error: str) -> str:
    """Rich markup matching print_tool_error() output, routed through run_in_terminal in TUI."""
    from rich.markup import escape
    return f"   [bold red]└─ Error:[/] {escape(error)}"


def _todo_list_markup(show_if_all_done: bool = False) -> str:
    """Rich markup equivalent of print_todo_list(), for TUI routing via run_in_terminal."""
    from .implementations import get_todo_list
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


def _diff_detail(path: str, new_content: str) -> str:
    """Return a rich-markup diff string for the write_file confirmation.

    New file: treated as diffing empty string → all lines shown as additions.
    """
    from pathlib import Path as _Path
    from ..diff import format_diff_rich

    try:
        existing = _Path(path).read_text(encoding="utf-8") if _Path(path).exists() else ""
    except Exception:
        existing = ""

    markup = format_diff_rich(existing, new_content)
    return markup if markup else "[muted](no changes)[/]"


def _diff_detail_edit(path: str, old_string: str, new_string: str) -> str:
    """Return a rich-markup diff string for the edit_file confirmation.

    Applies the edit in memory to produce the resulting file, then diffs
    original vs resulting so the user sees the full change in context.
    """
    from pathlib import Path as _Path
    from ..diff import format_diff_rich

    try:
        existing = _Path(path).read_text(encoding="utf-8") if _Path(path).exists() else ""
    except Exception:
        existing = ""

    result = _apply_edit(existing, old_string, new_string)
    if result.startswith("Error:"):
        return f"[muted]{result}[/]"

    markup = format_diff_rich(existing, result)
    return markup if markup else "[muted](no changes)[/]"


def _diff_lines_for_panel(name: str, inputs: dict) -> list[tuple[str, str]]:
    """Compute diff for the TUI permission panel using the same format_diff_rich as non-TUI.

    Returns prompt_toolkit (style, text) pairs ready for FormattedText.
    Limited to 30 lines to keep the panel usable.
    """
    from pathlib import Path as _Path
    from ..diff import format_diff_rich

    if name == "write_file":
        path = inputs.get("path", "")
        new_content = inputs.get("content", "")
        try:
            existing = _Path(path).read_text(encoding="utf-8") if _Path(path).exists() else ""
        except Exception:
            existing = ""
    elif name == "edit_file":
        path = inputs.get("path", "")
        old_str = inputs.get("old_string", "")
        new_str = inputs.get("new_string", "")
        try:
            existing = _Path(path).read_text(encoding="utf-8") if _Path(path).exists() else ""
        except Exception:
            existing = ""
        new_content = existing.replace(old_str, new_str, 1)
    else:
        return []

    markup = format_diff_rich(existing, new_content)
    if not markup:
        return []

    lines = markup.split("\n")
    if len(lines) > 30:
        lines = lines[:30]
        lines.append("[dim]  … (truncated)[/dim]")
    markup = "\n".join(lines)

    from ..tui.render import render_rich as _render_rich
    ansi = _render_rich(markup, width=76)

    from prompt_toolkit.formatted_text import ANSI as _ANSI, to_formatted_text as _to_ft
    return list(_to_ft(_ANSI(ansi + "\n")))


def _confirm_prompt(name: str, inputs: dict) -> tuple[str, str]:
    """Return (question, detail) for a dangerous tool confirmation.

    question — short one-liner used as the questionary prompt text
    detail   — multi-line context (all tool inputs, content previews) printed above it

    Generic: every key in inputs is rendered; multiline values get an 8-line preview.
    Keys already summarised in the question are skipped from detail to avoid repetition.
    """
    def _fmt_inputs(skip: frozenset = frozenset()) -> str:
        lines: list[str] = []
        for k, v in inputs.items():
            if k in skip:
                continue
            if isinstance(v, str) and "\n" in v:
                rows = v.splitlines()
                n = len(rows)
                lines.append(f"  {k} ({n} lines):")
                for row in rows[:8]:
                    lines.append(f"  │ {row}")
                if n > 8:
                    lines.append(f"  │ ... ({n - 8} more lines)")
            elif isinstance(v, str) and len(v) > 80:
                lines.append(f"  {k}: '{v[:80]}...'")
            else:
                lines.append(f"  {k}: {v!r}")
        return "\n".join(lines)

    if name == "run_shell":
        cmd = (inputs.get("command") or "")[:80]
        question = f"Allow run_shell?  `{cmd}`" if cmd else "Allow run_shell?"
        return question, _fmt_inputs(skip=frozenset({"command"}))

    if name == "write_file":
        path = inputs.get("path") or ""
        content = inputs.get("content") or ""
        question = f"Allow write_file?  {path}" if path else "Allow write_file?"
        return question, _diff_detail(path, content)

    if name == "edit_file":
        path = inputs.get("path") or ""
        question = f"Allow edit_file?  {path}" if path else "Allow edit_file?"
        detail = _diff_detail_edit(
            path,
            inputs.get("old_string") or "",
            inputs.get("new_string") or "",
        )
        return question, detail

    if name == "web_fetch":
        url = (inputs.get("url") or "")[:100]
        question = f"Allow web_fetch?  {url}" if url else "Allow web_fetch?"
        return question, ""

    return f"Allow {name}?", _fmt_inputs()


def _determine_mode_badge(
    name: str,
    inputs: dict,
    approval_mode: str,
    permission_store: Optional[PermissionStore],
) -> Optional[str]:
    """Return the auto-approval badge or None when confirmation is required.

    Returns: "yolo" | "edits" | "trusted" | None
    """
    if name not in DANGEROUS_TOOLS:
        return None
    if approval_mode == "yolo":
        return "yolo"
    if approval_mode == "edits" and name in _EDIT_TOOLS:
        return "edits"
    if permission_store is not None:
        if name == "run_shell":
            cmd = inputs.get("command", "")
        elif name == "web_fetch":
            cmd = inputs.get("url", "")
        else:  # write_file / edit_file
            cmd = inputs.get("path", "")
        if cmd and permission_store.is_trusted(name, cmd):
            return "trusted"
    return None


def _inline_edit_select(label: str, choices: list[str]) -> Optional[str]:
    """Select prompt with an inline-editable '[enter custom]' choice.

    Skipped in TUI mode (returns None) because it launches its own prompt_toolkit
    Application which would conflict with the running MinionApp.
    """
    from ..tui import is_tui_active as _is_tui_active
    if _is_tui_active():
        return None
    from prompt_toolkit import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.styles import Style as PTStyle

    EDIT = "[enter custom]"
    edit_idx = choices.index(EDIT) if EDIT in choices else -1
    # Colours matching MINION_STYLE
    YELLOW = "#FFD700"
    BLUE   = "#1E90FF"

    st = {"idx": 0, "buf": "", "result": None}

    def _leave() -> None:
        if st["idx"] == edit_idx:
            st["buf"] = ""

    def _render() -> FormattedText:
        rows: list[tuple[str, str]] = []
        rows.append(("bold", f"   {label}\n"))
        for i, ch in enumerate(choices):
            sel = i == st["idx"]
            pointer = ("fg:" + YELLOW + " bold", "   ❯  ") if sel else ("", "      ")
            if i == edit_idx and sel:
                text = f"{ch}: {st['buf']}▋"
            else:
                text = ch
            rows.append(pointer)
            rows.append(("fg:" + BLUE + " bold" if sel else "", text + "\n"))
        return FormattedText(rows)

    kb = KeyBindings()

    @kb.add("up", eager=True)
    def _up(event):
        _leave()
        st["idx"] = max(0, st["idx"] - 1)
        event.app.invalidate()

    @kb.add("down", eager=True)
    def _down(event):
        _leave()
        st["idx"] = min(len(choices) - 1, st["idx"] + 1)
        event.app.invalidate()

    @kb.add("enter", eager=True)
    def _enter(event):
        if st["idx"] == edit_idx:
            if st["buf"].strip():
                st["result"] = st["buf"].strip()
                event.app.exit()
            # empty buffer — do nothing, user must type or navigate away
        else:
            st["result"] = choices[st["idx"]]
            event.app.exit()

    @kb.add("backspace", eager=True)
    def _bs(event):
        if st["idx"] == edit_idx and st["buf"]:
            st["buf"] = st["buf"][:-1]
            event.app.invalidate()

    @kb.add("c-c", eager=True)
    @kb.add("c-d", eager=True)
    def _cancel(event):
        event.app.exit()

    @kb.add("<any>")
    def _type(event):
        if len(event.data) == 1 and event.data.isprintable():
            if st["idx"] != edit_idx:
                _leave()
                st["idx"] = edit_idx
            st["buf"] += event.data
            event.app.invalidate()

    app = Application(
        layout=Layout(Window(FormattedTextControl(_render, focusable=True))),
        key_bindings=kb,
        style=PTStyle.from_dict({}),
        full_screen=False,
        mouse_support=False,
    )
    app.run()
    return st["result"]


def _run_pattern_dialog(
    name: str,
    inputs: dict,
    scope: str,
    permission_store: PermissionStore,
) -> None:
    """Show per-part pattern selection and save rules to the permission store."""
    if name == "run_shell":
        raw = inputs.get("command", "")
        parts = split_compound(raw) if raw else []
    elif name == "web_fetch":
        raw = inputs.get("url", "")
        parts = [raw] if raw else []
    elif name in ("write_file", "edit_file"):
        raw = inputs.get("path", "")
        parts = [raw] if raw else []
    else:
        parts = []

    if not parts:
        return

    total = len(parts)
    for idx, part in enumerate(parts):
        if not part.strip():
            continue

        patterns = suggest_patterns_for_tool(name, part)
        if total > 1:
            first_token = part.strip().split()[0]
            label = f"Part {idx + 1}/{total} — {first_token}:"
        else:
            label = f"{name}:"

        option_choices = patterns + ["[enter custom]", "skip this part", "skip saving — just run it"]
        choice = _inline_edit_select(label, option_choices)

        if choice is None or choice == "skip saving — just run it":
            return

        if choice == "skip this part":
            continue

        pattern = choice.strip()
        if not pattern:
            continue

        permission_store.add_rule(name, pattern, scope)
        print_trust_saved(name, [pattern], scope)


def _interactive_confirm(
    name: str,
    inputs: dict,
    permission_store: Optional[PermissionStore],
) -> bool:
    """Replace questionary.confirm() with a two-step scope-then-pattern flow.

    Step 1: scope select (once / session / project / global / no).
    Step 2: per-part pattern dialog when a persistent scope is chosen.
    Returns True (approved) or False (declined). Thread-safe via _CONFIRM_LOCK.
    """
    with _CONFIRM_LOCK:
        question, detail = _confirm_prompt(name, inputs)
        if detail:
            if name in ("write_file", "edit_file"):
                console.print(detail)
            else:
                console.print(f"[muted]{detail}[/]")
        _flush_stdin()

        _project_toml = ".minion/permissions.toml"
        from pathlib import Path as _Path
        _global_toml = str(_Path.home() / ".minion" / "permissions.toml")

        choices = [
            " Yes, once",
            " Yes — always (session)",
            f" Yes — always (project  →  {_project_toml})",
            f" Yes — always (global   →  {_global_toml})",
            " No",
        ]
        choice = questionary.select(
            f" {question}",
            choices=choices,
            pointer="  ❯ ",
            style=MINION_STYLE,
        ).ask()

        if choice is None or choice.strip() == "No":
            return False
        if choice.strip() == "Yes, once":
            return True

        if "session" in choice:
            scope = "session"
        elif "project" in choice:
            scope = "project"
        else:
            scope = "global"

        if permission_store is not None:
            _run_pattern_dialog(name, inputs, scope, permission_store)

        return True


_DISPATCH: dict = {
    "read_file":        read_file,
    "write_file":       write_file,
    "edit_file":        edit_file,
    "list_directory":   list_directory,
    "glob":             glob,
    "search_file":      search_file,
    "web_fetch":        web_fetch,
    "run_shell":        run_shell,
    "get_file_outline": get_file_outline,
    "todo_write":       todo_write,
    "todo_read":        todo_read,
}

# Tools whose terminal display is suppressed — they communicate through dedicated UI
# (todo_write/todo_read show the Tasks panel instead of raw JSON payloads).
_SILENT_TOOLS: frozenset[str] = frozenset({"todo_write", "todo_read"})

# File-edit tools covered by /edits mode
_EDIT_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file"})


class _RenderBuffer:
    """Captures renderer calls during parallel tool execution for sequential replay.

    Passed as _slot_renderer to execute_async. After all parallel tasks complete,
    call flush_to(renderer) for each buffer in order to write to the scrollback.
    """

    def __init__(self) -> None:
        self._ops: list = []

    def on_tool_call(self, name: str, inputs: dict, *, dry_run: bool = False,
                     agent_label: Optional[str] = None, mode_badge: Optional[str] = None) -> None:
        self._ops.append(lambda r: r.on_tool_call(name, inputs, dry_run=dry_run,
                                                   agent_label=agent_label, mode_badge=mode_badge))

    def on_diff_preview(self, detail: str, *, tool_name: str = "") -> None:
        self._ops.append(lambda r: r.on_diff_preview(detail, tool_name=tool_name))

    def on_tool_result(self, result: str, latency_ms: int = 0) -> None:
        self._ops.append(lambda r: r.on_tool_result(result, latency_ms=latency_ms))

    def on_tool_error(self, error: str) -> None:
        self._ops.append(lambda r: r.on_tool_error(error))

    def on_todo_list(self, *, show_if_all_done: bool = False) -> None:
        self._ops.append(lambda r: r.on_todo_list(show_if_all_done=show_if_all_done))

    def spinner(self, label: str) -> "contextlib.AbstractContextManager":
        return contextlib.nullcontext()

    def flush_to(self, renderer: "OutputRenderer") -> None:
        for op in self._ops:
            op(renderer)


class ToolExecutor:
    """Executes tool calls from the agent loop.

    dry_run=True: prints what would run but never calls implementations.
    Confirmation is requested for DANGEROUS_TOOLS and dangerous MCP tools.

    mcp_manager: if provided, tool names containing '__' are routed to the
    matching MCP server rather than the native _DISPATCH table. Tools flagged
    as destructive (via MCP annotations or confirm_all server config) receive
    the same confirmation prompt as native DANGEROUS_TOOLS.
    """

    def __init__(self, dry_run: bool = False, mcp_manager=None, agent_runner=None,
                 agent_label=None, remote_task_runner=None, confirm_callback=None,
                 approval_mode: str = "off",
                 permission_store: Optional[PermissionStore] = None,
                 hook_runner=None, confirmation_manager=None,
                 renderer: Optional[OutputRenderer] = None) -> None:
        self.dry_run = dry_run
        self._mcp_manager = mcp_manager          # type: MCPManager | None
        self._agent_runner = agent_runner        # type: Callable[[str, str | None], str] | None
        self._agent_label = agent_label          # type: str | None — shown as prefix on tool calls
        self._remote_task_runner = remote_task_runner  # type: Callable[[str, str], str] | None
        self._confirm_callback = confirm_callback  # type: Callable[[str], bool] | None
        self._approval_mode = approval_mode  # "off" | "edits" | "yolo"
        self._permission_store = permission_store
        self._hook_runner = hook_runner          # type: HookRunner | None
        if confirmation_manager is None and confirm_callback is None:
            from ..confirmation import ConfirmationManager
            confirmation_manager = ConfirmationManager(permission_store)
        self._confirmation_manager = confirmation_manager
        # Auto-detect renderer if not provided
        if renderer is None:
            from ..tui import get_tui_app as _detect_tui
            _detected = _detect_tui()
            if _detected is not None:
                from ..output import TuiRenderer
                renderer = TuiRenderer(_detected)
            else:
                renderer = ConsoleRenderer()
        self._renderer = renderer

    def execute(self, tool_block: ToolUseBlock) -> str:
        """Execute a tool call and return the result string for context injection."""
        name = tool_block.name
        inputs = tool_block.input

        _mode_badge = _determine_mode_badge(name, inputs, self._approval_mode, self._permission_store)

        from ..agents.display import get_agent_display_callback as _get_agent_cb
        _agent_cb = _get_agent_cb()
        if _agent_cb is not None:
            _agent_cb("tool_call", name=name, inputs=inputs)
        elif name not in _SILENT_TOOLS:
            self._renderer.on_tool_call(name, inputs, dry_run=self.dry_run, agent_label=self._agent_label, mode_badge=_mode_badge)

        if self.dry_run:
            return "[dry-run: tool not executed]"

        # spawn_agent: delegate to the injected agent runner callable
        if name == "spawn_agent":
            if self._agent_runner is None:
                result = "Error: subagents not available (agents disabled or at max depth)."
                if _agent_cb is None:
                    self._renderer.on_tool_error(result)
                return result
            task = inputs.get("task", "")
            role = inputs.get("role")
            get_tracer().emit("tool_call", tool_name="spawn_agent", inputs=inputs)
            result = self._agent_runner(task, role)
            if _agent_cb is None:
                self._renderer.on_tool_result(result)
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=result, success=True)
            return result

        # send_remote_task: delegate to the injected remote task runner callable
        if name == "send_remote_task":
            if self._remote_task_runner is None:
                result = "Error: no remote A2A agents configured."
                if _agent_cb is None:
                    self._renderer.on_tool_error(result)
                return result
            agent = inputs.get("agent", "")
            task = inputs.get("task", "")
            get_tracer().emit("tool_call", tool_name="send_remote_task", inputs=inputs)
            if _agent_cb is not None:
                result = self._remote_task_runner(agent, task)
            else:
                with self._renderer.spinner(f"[muted]waiting for {agent}...[/]"):
                    result = self._remote_task_runner(agent, task)
            if _agent_cb is None:
                self._renderer.on_tool_result(result)
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=result, success=True)
            return result

        if name in DANGEROUS_TOOLS:
            if _mode_badge is not None:
                # Auto-approved: show the diff preview but skip the confirmation prompt.
                if self._confirm_callback is None and _agent_cb is None:
                    _, detail = _confirm_prompt(name, inputs)
                    if detail:
                        self._renderer.on_diff_preview(detail, tool_name=name)
            else:
                # TUI: pass diff to permission panel. Non-TUI: _interactive_confirm shows it.
                _diff_lns: list = []
                if self._confirm_callback is None:
                    from ..tui import is_tui_active as _is_tui_active
                    if _is_tui_active():
                        _diff_lns = _diff_lines_for_panel(name, inputs)
                if self._confirm_callback is not None:
                    confirmed = self._confirm_callback(name, inputs)
                else:
                    confirmed = self._confirmation_manager.confirm_sync(name, inputs, diff_lines=_diff_lns)
                if not confirmed:
                    result = "User declined tool execution."
                    if _agent_cb is None:
                        self._renderer.on_tool_result(result)
                    return result

        fn = _DISPATCH.get(name)
        if fn is None:
            # MCP tool: namespaced as "server__tool"
            if "__" in name and self._mcp_manager is not None:
                if self._mcp_manager.is_dangerous(name):
                    if self._confirm_callback is not None:
                        confirmed = self._confirm_callback(name, {})
                    else:
                        confirmed = self._confirmation_manager.confirm_sync(name, {})
                    if not confirmed:
                        result = "User declined tool execution."
                        if _agent_cb is None:
                            self._renderer.on_tool_result(result)
                        return result
                try:
                    # call_tool is async; sync execute() is called from threads
                    # where asyncio.run() is safe (no running event loop in thread).
                    result = asyncio.run(self._mcp_manager.call_tool(name, inputs))
                except Exception as e:
                    result = f"Error: {e}"
                if _agent_cb is None:
                    self._renderer.on_tool_result(result)
                return result
            error = f"Unknown tool: '{name}'"
            if _agent_cb is None:
                self._renderer.on_tool_error(error)
            return f"Error: {error}"

        get_tracer().emit("tool_call", tool_name=name, inputs=inputs)
        try:
            spinner_label = TOOL_SPINNER_LABELS.get(name, f"[muted]{name}...[/]")
            _spin_cm = contextlib.nullcontext() if _agent_cb is not None else self._renderer.spinner(spinner_label)
            with _spin_cm:
                result = fn(**inputs)
            if _agent_cb is None and name not in _SILENT_TOOLS:
                self._renderer.on_tool_result(result)
            if _agent_cb is None and name == "todo_write":
                self._renderer.on_todo_list(show_if_all_done=True)
            get_tracer().emit("tool_result", tool_name=name, output=result, success=True)
            return result
        except Exception as e:
            error = str(e)
            if _agent_cb is None:
                self._renderer.on_tool_error(error)
            get_tracer().emit("tool_result", tool_name=name, output=error, success=False)
            return f"Error: {error}"

    async def execute_async(self, tool_block: ToolUseBlock, *,
                            _slot_renderer: Optional["OutputRenderer"] = None) -> str:
        """Async version of execute(). Runs confirmations and tools without blocking the event loop.

        _slot_renderer: when set (parallel mode), rendering is captured in that buffer
        instead of going to self._renderer. After all parallel tasks complete the
        caller flushes each buffer to the real renderer in order.

        Confirmation dialogs use asyncio.Lock + asyncio.to_thread so the event loop
        stays responsive while waiting for user input. Tool functions run in a thread
        pool via asyncio.to_thread so they don't block the event loop either.
        """
        name = tool_block.name
        inputs = tool_block.input
        _t0 = _time_exec.monotonic()

        _mode_badge = _determine_mode_badge(name, inputs, self._approval_mode, self._permission_store)

        from ..agents.display import get_agent_display_callback as _get_agent_cb
        _agent_cb = _get_agent_cb()

        # Split rendering responsibilities:
        #   _immediate_r — call header + diff: always shown before confirmation
        #   _deferred_r  — result/error: buffered in parallel, immediate in single
        #
        #   parallel (_slot_renderer set): immediate→real renderer, deferred→buffer
        #   single   (no _agent_cb):       both → real renderer (same object)
        #   subagent (_agent_cb, no buf):  both → None (slot shows status instead)
        if _slot_renderer is not None:
            _immediate_r = self._renderer
            _deferred_r  = _slot_renderer
        elif _agent_cb is None:
            _immediate_r = self._renderer
            _deferred_r  = self._renderer
        else:
            _immediate_r = None
            _deferred_r  = None

        if _agent_cb is not None and _slot_renderer is None:
            _agent_cb("tool_call", name=name, inputs=inputs)
        if _deferred_r is not None and name not in _SILENT_TOOLS:
            _deferred_r.on_tool_call(name, inputs, dry_run=self.dry_run,
                                     agent_label=self._agent_label, mode_badge=_mode_badge)

        if self.dry_run:
            return "[dry-run: tool not executed]"

        if name == "spawn_agent":
            if self._agent_runner is None:
                result = "Error: subagents not available (agents disabled or at max depth)."
                if _deferred_r is not None:
                    _deferred_r.on_tool_error(result)
                return result
            task = inputs.get("task", "")
            role = inputs.get("role")
            get_tracer().emit("tool_call", tool_name="spawn_agent", inputs=inputs)
            result = await asyncio.to_thread(self._agent_runner, task, role)
            if _deferred_r is not None:
                _deferred_r.on_tool_result(result, latency_ms=int((_time_exec.monotonic() - _t0) * 1000))
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=result, success=True)
            return result

        if name == "send_remote_task":
            if self._remote_task_runner is None:
                result = "Error: no remote A2A agents configured."
                if _deferred_r is not None:
                    _deferred_r.on_tool_error(result)
                return result
            agent = inputs.get("agent", "")
            task = inputs.get("task", "")
            get_tracer().emit("tool_call", tool_name="send_remote_task", inputs=inputs)
            _spin_cm = _deferred_r.spinner(f"[muted]waiting for {agent}...[/]") if _deferred_r is not None else contextlib.nullcontext()
            with _spin_cm:
                result = await asyncio.to_thread(self._remote_task_runner, agent, task)
            if _deferred_r is not None:
                _deferred_r.on_tool_result(result, latency_ms=int((_time_exec.monotonic() - _t0) * 1000))
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=result, success=True)
            return result

        if name in DANGEROUS_TOOLS:
            if _mode_badge is not None:
                # Auto-approved: show diff immediately (before execution).
                if self._confirm_callback is None and _immediate_r is not None:
                    _, detail = _confirm_prompt(name, inputs)
                    if detail:
                        _immediate_r.on_diff_preview(detail, tool_name=name)
            else:
                # TUI: pass diff to permission panel. Non-TUI: _interactive_confirm shows it.
                _diff_lns: list = []
                if self._confirm_callback is None:
                    from ..tui import is_tui_active as _is_tui_active
                    if _is_tui_active():
                        _diff_lns = _diff_lines_for_panel(name, inputs)
                if self._confirm_callback is not None:
                    confirmed = await asyncio.to_thread(self._confirm_callback, name, inputs)
                else:
                    confirmed = await self._confirmation_manager.confirm_async(name, inputs, diff_lines=_diff_lns)
                if not confirmed:
                    result = "User declined tool execution."
                    if _deferred_r is not None:
                        _deferred_r.on_tool_result(result)
                    return result

        # ── Pre-tool hook ──────────────────────────────────────────────────
        if self._hook_runner is not None:
            from ..hooks.events import PreToolUseEvent
            from ..tracing import get_tracer as _get_tracer_hooks
            _pre_event = PreToolUseEvent(
                session_id=_get_tracer_hooks().session_id or "",
                cwd=Path.cwd(),
                tool_name=name,
                tool_input=dict(inputs),
            )
            _block = await self._hook_runner.fire_pre_tool(_pre_event)
            if _block is not None:
                _reason = _block.reason or f"Hook blocked {name}."
                if _deferred_r is not None:
                    _deferred_r.on_tool_error(_reason)
                return f"Error: {_reason}"

        fn = _DISPATCH.get(name)
        if fn is None:
            if "__" in name and self._mcp_manager is not None:
                if self._mcp_manager.is_dangerous(name):
                    if self._confirm_callback is not None:
                        confirmed = await asyncio.to_thread(self._confirm_callback, name, {})
                    else:
                        confirmed = await self._confirmation_manager.confirm_async(name, {})
                    if not confirmed:
                        result = "User declined tool execution."
                        if _deferred_r is not None:
                            _deferred_r.on_tool_result(result)
                        return result
                try:
                    result = await self._mcp_manager.call_tool(name, inputs)
                except Exception as e:
                    result = f"Error: {e}"
                if _deferred_r is not None:
                    _deferred_r.on_tool_result(result, latency_ms=int((_time_exec.monotonic() - _t0) * 1000))
                # ── Post-tool hook (MCP) ───────────────────────────────────
                if self._hook_runner is not None:
                    from ..hooks.events import PostToolUseEvent
                    await self._hook_runner.fire_post_tool(PostToolUseEvent(
                        session_id=_get_tracer_hooks().session_id or "",
                        cwd=Path.cwd(),
                        tool_name=name,
                        tool_input=dict(inputs),
                        tool_result=result,
                        tool_success=not result.startswith("Error:"),
                    ))
                return result
            error = f"Unknown tool: '{name}'"
            if _deferred_r is not None:
                _deferred_r.on_tool_error(error)
            return f"Error: {error}"

        get_tracer().emit("tool_call", tool_name=name, inputs=inputs)
        try:
            spinner_label = TOOL_SPINNER_LABELS.get(name, f"[muted]{name}...[/]")
            _spin_cm = _deferred_r.spinner(spinner_label) if _deferred_r is not None else contextlib.nullcontext()
            with _spin_cm:
                result = await asyncio.to_thread(fn, **inputs)
            if _deferred_r is not None and name not in _SILENT_TOOLS:
                _deferred_r.on_tool_result(result, latency_ms=int((_time_exec.monotonic() - _t0) * 1000))
            if _deferred_r is not None and name == "todo_write":
                _deferred_r.on_todo_list(show_if_all_done=True)
            get_tracer().emit("tool_result", tool_name=name, output=result, success=True)
            # ── Post-tool hook (native, success) ───────────────────────────
            if self._hook_runner is not None:
                from ..hooks.events import PostToolUseEvent
                await self._hook_runner.fire_post_tool(PostToolUseEvent(
                    session_id=get_tracer().session_id or "",
                    cwd=Path.cwd(),
                    tool_name=name,
                    tool_input=dict(inputs),
                    tool_result=result,
                    tool_success=True,
                ))
            return result
        except Exception as e:
            error = str(e)
            if _deferred_r is not None:
                _deferred_r.on_tool_error(error)
            get_tracer().emit("tool_result", tool_name=name, output=error, success=False)
            # ── Post-tool hook (native, error) ─────────────────────────────
            if self._hook_runner is not None:
                from ..hooks.events import PostToolUseEvent
                await self._hook_runner.fire_post_tool(PostToolUseEvent(
                    session_id=get_tracer().session_id or "",
                    cwd=Path.cwd(),
                    tool_name=name,
                    tool_input=dict(inputs),
                    tool_result=f"Error: {error}",
                    tool_success=False,
                ))
            return f"Error: {error}"
