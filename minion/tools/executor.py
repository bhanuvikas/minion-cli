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
from typing import Optional

import questionary

from ..config import MINION_STYLE

# Serializes questionary.confirm() calls across threads (sync path only).
_CONFIRM_LOCK = threading.Lock()

# Async confirmation lock — lazy-initialised on first use inside an event loop.
_ASYNC_CONFIRM_LOCK: Optional[asyncio.Lock] = None


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


def _get_async_confirm_lock() -> asyncio.Lock:
    global _ASYNC_CONFIRM_LOCK
    if _ASYNC_CONFIRM_LOCK is None:
        _ASYNC_CONFIRM_LOCK = asyncio.Lock()
    return _ASYNC_CONFIRM_LOCK
from ..llm.base import ToolUseBlock
from ..theme import console, print_tool_call, print_tool_error, print_tool_result
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
                 agent_label=None, remote_task_runner=None, confirm_callback=None) -> None:
        self.dry_run = dry_run
        self._mcp_manager = mcp_manager          # type: MCPManager | None
        self._agent_runner = agent_runner        # type: Callable[[str, str | None], str] | None
        self._agent_label = agent_label          # type: str | None — shown as prefix on tool calls
        self._remote_task_runner = remote_task_runner  # type: Callable[[str, str], str] | None
        self._confirm_callback = confirm_callback  # type: Callable[[str], bool] | None

    def execute(self, tool_block: ToolUseBlock) -> str:
        """Execute a tool call and return the result string for context injection."""
        name = tool_block.name
        inputs = tool_block.input

        # When running inside a parallel Live display, route tool-call display
        # through the slot callback instead of console.print (which would corrupt Live).
        from ..agents.display import get_agent_display_callback as _get_agent_cb
        _agent_cb = _get_agent_cb()
        if _agent_cb is not None:
            _agent_cb("tool_call", name=name, inputs=inputs)
        else:
            print_tool_call(name, inputs, dry_run=self.dry_run, agent_label=self._agent_label)

        if self.dry_run:
            return "[dry-run: tool not executed]"

        # spawn_agent: delegate to the injected agent runner callable
        if name == "spawn_agent":
            if self._agent_runner is None:
                result = "Error: subagents not available (agents disabled or at max depth)."
                if _agent_cb is None:
                    print_tool_error(result)
                return result
            task = inputs.get("task", "")
            role = inputs.get("role")
            get_tracer().emit("tool_call", tool_name="spawn_agent", inputs=inputs)
            result = self._agent_runner(task, role)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=result, success=True)
            return result

        # send_remote_task: delegate to the injected remote task runner callable
        if name == "send_remote_task":
            if self._remote_task_runner is None:
                result = "Error: no remote A2A agents configured."
                if _agent_cb is None:
                    print_tool_error(result)
                return result
            agent = inputs.get("agent", "")
            task = inputs.get("task", "")
            get_tracer().emit("tool_call", tool_name="send_remote_task", inputs=inputs)
            if _agent_cb is not None:
                result = self._remote_task_runner(agent, task)
            else:
                from ..theme import set_active_status
                _status = console.status(f"[muted]waiting for {agent}...[/]", spinner="dots")
                _status.start()
                set_active_status(_status)
                try:
                    result = self._remote_task_runner(agent, task)
                finally:
                    _status.stop()
                    set_active_status(None)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=result, success=True)
            return result

        if name in DANGEROUS_TOOLS:
            question, detail = _confirm_prompt(name, inputs)
            if self._confirm_callback is not None:
                confirmed = self._confirm_callback(question, detail)
            else:
                with _CONFIRM_LOCK:
                    if detail:
                        # write_file detail is a rich-formatted diff; others are plain muted text.
                        if name in ("write_file", "edit_file"):
                            console.print(detail)
                        else:
                            console.print(f"[muted]{detail}[/]")
                    _flush_stdin()
                    confirmed = questionary.confirm(
                        f"  {question}",
                        default=False,
                        style=MINION_STYLE,
                    ).ask()
            if not confirmed:
                result = "User declined tool execution."
                if _agent_cb is None:
                    print_tool_result(result)
                return result

        fn = _DISPATCH.get(name)
        if fn is None:
            # MCP tool: namespaced as "server__tool"
            if "__" in name and self._mcp_manager is not None:
                if self._mcp_manager.is_dangerous(name):
                    with _CONFIRM_LOCK:
                        _flush_stdin()
                        confirmed = questionary.confirm(
                            f"  Allow {name}?",
                            default=False,
                            style=MINION_STYLE,
                        ).ask()
                    if not confirmed:
                        result = "User declined tool execution."
                        if _agent_cb is None:
                            print_tool_result(result)
                        return result
                try:
                    # call_tool is async; sync execute() is called from threads
                    # where asyncio.run() is safe (no running event loop in thread).
                    result = asyncio.run(self._mcp_manager.call_tool(name, inputs))
                except Exception as e:
                    result = f"Error: {e}"
                if _agent_cb is None:
                    print_tool_result(result)
                return result
            error = f"Unknown tool: '{name}'"
            if _agent_cb is None:
                print_tool_error(error)
            return f"Error: {error}"

        get_tracer().emit("tool_call", tool_name=name, inputs=inputs)
        try:
            spinner_label = TOOL_SPINNER_LABELS.get(name, f"[muted]{name}...[/]")
            _spin_cm = contextlib.nullcontext() if _agent_cb is not None else console.status(spinner_label, spinner="dots")
            with _spin_cm:
                result = fn(**inputs)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name=name, output=result, success=True)
            return result
        except Exception as e:
            error = str(e)
            if _agent_cb is None:
                print_tool_error(error)
            get_tracer().emit("tool_result", tool_name=name, output=error, success=False)
            return f"Error: {error}"

    async def execute_async(self, tool_block: ToolUseBlock) -> str:
        """Async version of execute(). Runs confirmations and tools without blocking the event loop.

        Confirmation dialogs use asyncio.Lock + asyncio.to_thread so the event loop
        stays responsive while waiting for user input. Tool functions run in a thread
        pool via asyncio.to_thread so they don't block the event loop either.
        """
        name = tool_block.name
        inputs = tool_block.input

        from ..agents.display import get_agent_display_callback as _get_agent_cb
        _agent_cb = _get_agent_cb()
        if _agent_cb is not None:
            _agent_cb("tool_call", name=name, inputs=inputs)
        else:
            print_tool_call(name, inputs, dry_run=self.dry_run, agent_label=self._agent_label)

        if self.dry_run:
            return "[dry-run: tool not executed]"

        if name == "spawn_agent":
            if self._agent_runner is None:
                result = "Error: subagents not available (agents disabled or at max depth)."
                if _agent_cb is None:
                    print_tool_error(result)
                return result
            task = inputs.get("task", "")
            role = inputs.get("role")
            get_tracer().emit("tool_call", tool_name="spawn_agent", inputs=inputs)
            result = await asyncio.to_thread(self._agent_runner, task, role)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=result, success=True)
            return result

        if name == "send_remote_task":
            if self._remote_task_runner is None:
                result = "Error: no remote A2A agents configured."
                if _agent_cb is None:
                    print_tool_error(result)
                return result
            agent = inputs.get("agent", "")
            task = inputs.get("task", "")
            get_tracer().emit("tool_call", tool_name="send_remote_task", inputs=inputs)
            if _agent_cb is not None:
                result = await asyncio.to_thread(self._remote_task_runner, agent, task)
            else:
                from ..theme import set_active_status
                _status = console.status(f"[muted]waiting for {agent}...[/]", spinner="dots")
                _status.start()
                set_active_status(_status)
                try:
                    result = await asyncio.to_thread(self._remote_task_runner, agent, task)
                finally:
                    _status.stop()
                    set_active_status(None)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=result, success=True)
            return result

        if name in DANGEROUS_TOOLS:
            question, detail = _confirm_prompt(name, inputs)
            if self._confirm_callback is not None:
                confirmed = await asyncio.to_thread(self._confirm_callback, question, detail)
            else:
                async with _get_async_confirm_lock():
                    if detail:
                        if name in ("write_file", "edit_file"):
                            console.print(detail)
                        else:
                            console.print(f"[muted]{detail}[/]")
                    _flush_stdin()
                    confirmed = await asyncio.to_thread(
                        lambda: questionary.confirm(
                            f"  {question}", default=False, style=MINION_STYLE
                        ).ask()
                    )
            if not confirmed:
                result = "User declined tool execution."
                if _agent_cb is None:
                    print_tool_result(result)
                return result

        fn = _DISPATCH.get(name)
        if fn is None:
            if "__" in name and self._mcp_manager is not None:
                if self._mcp_manager.is_dangerous(name):
                    async with _get_async_confirm_lock():
                        _flush_stdin()
                        confirmed = await asyncio.to_thread(
                            lambda: questionary.confirm(
                                f"  Allow {name}?", default=False, style=MINION_STYLE
                            ).ask()
                        )
                    if not confirmed:
                        result = "User declined tool execution."
                        if _agent_cb is None:
                            print_tool_result(result)
                        return result
                try:
                    result = await self._mcp_manager.call_tool(name, inputs)
                except Exception as e:
                    result = f"Error: {e}"
                if _agent_cb is None:
                    print_tool_result(result)
                return result
            error = f"Unknown tool: '{name}'"
            if _agent_cb is None:
                print_tool_error(error)
            return f"Error: {error}"

        get_tracer().emit("tool_call", tool_name=name, inputs=inputs)
        try:
            spinner_label = TOOL_SPINNER_LABELS.get(name, f"[muted]{name}...[/]")
            _spin_cm = contextlib.nullcontext() if _agent_cb is not None else console.status(spinner_label, spinner="dots")
            with _spin_cm:
                result = await asyncio.to_thread(fn, **inputs)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name=name, output=result, success=True)
            return result
        except Exception as e:
            error = str(e)
            if _agent_cb is None:
                print_tool_error(error)
            get_tracer().emit("tool_result", tool_name=name, output=error, success=False)
            return f"Error: {error}"
