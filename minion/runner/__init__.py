"""minion.runner — ReAct loop entry points.

Decomposed into focused submodules:
  context.py  — @mention resolution, message serialization
  parallel.py — parallel agent and tool dispatch
  loop.py     — streaming iteration and main run_prompt_async loop

Public API: run_prompt() and run_prompt_async(). Internal helpers are
re-exported for backward compatibility with existing callers and tests.
"""

from .loop import (
    MAX_ITERATIONS,
    _IterationResult,
    _build_content_blocks,
    _complete_cancelled_tools,
    _run_reflection,
    _stream_one_iteration_async,
    run_prompt,
    run_prompt_async,
)
from .parallel import (
    _agent_slots,
    _execute_parallel_agents_async,
    _execute_parallel_tools_async,
    _execute_tools_async,
    _tool_slots,
)
from .context import _resolve_mentions, _serialize_messages, _snapshot_messages

__all__ = [
    # Public API
    "run_prompt",
    "run_prompt_async",
    "MAX_ITERATIONS",
    # Internal helpers exposed for tests
    "_IterationResult",
    "_complete_cancelled_tools",
    "_stream_one_iteration_async",
    "_execute_tools_async",
    "_resolve_mentions",
]
