"""Tool executor — dispatches tool calls, handles confirmation and dry-run.

Single responsibility: given a ToolUseBlock from the model, decide whether to
execute it (dry-run check, confirmation for dangerous tools), dispatch to the
right implementation, and surface the result via theme helpers.

Keeps UX concerns (confirmation prompts, display) out of implementations.py,
and keeps business-logic concerns (which tools are dangerous, dispatch table)
out of runner.py.
"""

import questionary

from ..config import MINION_STYLE
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

    def __init__(self, dry_run: bool = False, mcp_manager=None) -> None:
        self.dry_run = dry_run
        self._mcp_manager = mcp_manager  # type: MCPManager | None

    def execute(self, tool_block: ToolUseBlock) -> str:
        """Execute a tool call and return the result string for context injection."""
        name = tool_block.name
        inputs = tool_block.input

        print_tool_call(name, inputs, dry_run=self.dry_run)

        if self.dry_run:
            return "[dry-run: tool not executed]"

        if name in DANGEROUS_TOOLS:
            confirmed = questionary.confirm(
                f"  Allow {name}?",
                default=False,
                style=MINION_STYLE,
            ).ask()
            if not confirmed:
                result = "User declined tool execution."
                print_tool_result(result)
                return result

        fn = _DISPATCH.get(name)
        if fn is None:
            # MCP tool: namespaced as "server__tool"
            if "__" in name and self._mcp_manager is not None:
                if self._mcp_manager.is_dangerous(name):
                    confirmed = questionary.confirm(
                        f"  Allow {name}?",
                        default=False,
                        style=MINION_STYLE,
                    ).ask()
                    if not confirmed:
                        result = "User declined tool execution."
                        print_tool_result(result)
                        return result
                try:
                    result = self._mcp_manager.call_tool(name, inputs)
                except Exception as e:
                    result = f"Error: {e}"
                print_tool_result(result)
                return result
            error = f"Unknown tool: '{name}'"
            print_tool_error(error)
            return f"Error: {error}"

        get_tracer().emit("tool_call", tool_name=name, inputs=inputs)
        try:
            spinner_label = _TOOL_SPINNER_LABELS.get(name, f"[muted]{name}...[/]")
            with console.status(spinner_label, spinner="dots"):
                result = fn(**inputs)
            print_tool_result(result)
            get_tracer().emit("tool_result", tool_name=name, output=result, success=True)
            return result
        except Exception as e:
            error = str(e)
            print_tool_error(error)
            get_tracer().emit("tool_result", tool_name=name, output=error, success=False)
            return f"Error: {error}"
