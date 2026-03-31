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
from ..theme import print_tool_call, print_tool_error, print_tool_result
from .definitions import DANGEROUS_TOOLS
from .implementations import list_directory, read_file, run_shell, write_file

_DISPATCH: dict = {
    "read_file": read_file,
    "write_file": write_file,
    "list_directory": list_directory,
    "run_shell": run_shell,
}


class ToolExecutor:
    """Executes tool calls from the agent loop.

    dry_run=True: prints what would run but never calls implementations.
    Confirmation is requested for DANGEROUS_TOOLS before executing.
    """

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

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
            error = f"Unknown tool: '{name}'"
            print_tool_error(error)
            return f"Error: {error}"

        try:
            result = fn(**inputs)
            print_tool_result(result)
            return result
        except Exception as e:
            error = str(e)
            print_tool_error(error)
            return f"Error: {error}"
