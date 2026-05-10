"""Interactive REPL package — public API re-exports.

Callers import from `minion.repl` as before; nothing else needs to change.
"""

from .state import CommandContext, REPL_COMMANDS, ReplState
from .session import run_repl, run_repl_async
from .commands import _handle_slash_command, _get_last_response_text
from .input import _SlashCompleter
from .init_md import _generate_minion_md, _generate_minion_md_llm

__all__ = [
    "ReplState",
    "REPL_COMMANDS",
    "CommandContext",
    "run_repl",
    "run_repl_async",
    "_handle_slash_command",
    "_get_last_response_text",
    "_SlashCompleter",
    "_generate_minion_md",
    "_generate_minion_md_llm",
]
