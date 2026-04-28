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
import threading
from typing import Optional

import questionary

from ..config import MINION_STYLE

# Serializes questionary.confirm() calls across threads (sync path only).
_CONFIRM_LOCK = threading.Lock()

# Async confirmation lock — lazy-initialised on first use inside an event loop.
_ASYNC_CONFIRM_LOCK: Optional[asyncio.Lock] = None


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
    get_file_outline,
    list_directory,
    read_file,
    run_shell,
    search_code,
    write_file,
)

_TOOL_SPINNER_LABELS: dict[str, str] = {
    "write_file":       "[muted]writing...[/]",
    "run_shell":        "[muted]running...[/]",
    "read_file":        "[muted]reading...[/]",
    "list_directory":   "[muted]listing...[/]",
    "search_code":      "[muted]searching...[/]",
    "get_file_outline": "[muted]analyzing...[/]",
}

_DISPATCH: dict = {
    "read_file":        read_file,
    "write_file":       write_file,
    "list_directory":   list_directory,
    "run_shell":        run_shell,
    "get_file_outline": get_file_outline,
    "search_code":      search_code,
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
            result = self._remote_task_runner(agent, task)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=result, success=True)
            return result

        if name in DANGEROUS_TOOLS:
            if self._confirm_callback is not None:
                confirmed = self._confirm_callback(f"Allow {name}?")
            else:
                with _CONFIRM_LOCK:
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

        fn = _DISPATCH.get(name)
        if fn is None:
            # MCP tool: namespaced as "server__tool"
            if "__" in name and self._mcp_manager is not None:
                if self._mcp_manager.is_dangerous(name):
                    with _CONFIRM_LOCK:
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
            spinner_label = _TOOL_SPINNER_LABELS.get(name, f"[muted]{name}...[/]")
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
            result = await asyncio.to_thread(self._remote_task_runner, agent, task)
            if _agent_cb is None:
                print_tool_result(result)
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=result, success=True)
            return result

        if name in DANGEROUS_TOOLS:
            if self._confirm_callback is not None:
                confirmed = await asyncio.to_thread(self._confirm_callback, f"Allow {name}?")
            else:
                async with _get_async_confirm_lock():
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

        fn = _DISPATCH.get(name)
        if fn is None:
            if "__" in name and self._mcp_manager is not None:
                if self._mcp_manager.is_dangerous(name):
                    async with _get_async_confirm_lock():
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
            spinner_label = _TOOL_SPINNER_LABELS.get(name, f"[muted]{name}...[/]")
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
