"""Agent runner — spawns an isolated subagent and returns its response.

run_agent() is the execution engine for the spawn_agent tool. It:
  1. Resolves the requested role from the registry.
  2. Emits agent_spawn trace event and prints a status line.
  3. Calls run_prompt() with capture_output=True and a fresh Conversation.
  4. Emits agent_complete (or agent_error) and returns the response text.

The caller (ToolExecutor) receives a string — the subagent's full response —
which is injected as a tool result into the orchestrator's conversation.
"""

from __future__ import annotations

import time
from typing import Optional

from ..conversation import Conversation
from ..theme import console
from ..tools.definitions import TOOL_DEFINITIONS
from ..tracing import get_tracer
from .display import get_agent_display_callback
from .manifest import AgentRoleManifest
from .registry import AgentRegistry

# Workers at this depth or higher cannot spawn further subagents.
# MAX_AGENT_DEPTH = 1 means: orchestrator (depth=0) can spawn; workers (depth=1) cannot.
MAX_AGENT_DEPTH = 1

_DEFAULT_SUBAGENT_PROMPT = """\
You are a focused subagent. Complete the given task thoroughly and concisely.
Report your findings clearly. Do not ask clarifying questions — work with what you have.
"""


def _resolve_tools(tool_names: Optional[list[str]]) -> Optional[list[dict]]:
    """Translate a role's tool name list to TOOL_DEFINITIONS dicts.

    Returns:
        None  — role allows all native tools
        []    — role allows no tools
        [...]  — filtered subset of TOOL_DEFINITIONS matching tool_names
    """
    if tool_names is None:
        return None
    name_set = set(tool_names)
    return [t for t in TOOL_DEFINITIONS if t["name"] in name_set]


def run_agent(
    task: str,
    role_name: Optional[str],
    registry: AgentRegistry,
    client,  # LLMClient — avoided circular import via duck typing
    parent_depth: int = 0,
) -> str:
    """Spawn an isolated subagent and return its final text response.

    Creates a fresh Conversation (no parent history), picks the role's system
    prompt and tool subset, and runs run_prompt() with capture_output=True so
    the subagent's output is captured rather than streamed to the terminal.

    Parameters
    ----------
    task          : Self-contained task description for the subagent.
    role_name     : Role to use (researcher / coder / reviewer / tester / None).
                    None → "general" fallback with all tools.
    registry      : Loaded AgentRegistry from load_agent_registry().
    client        : LLMClient for making API calls.
    parent_depth  : Depth of the calling agent (0 = orchestrator).
                    The worker gets parent_depth + 1, which blocks further
                    spawning if it reaches MAX_AGENT_DEPTH.
    """
    # Import here to avoid circular import (runner.py imports ToolExecutor which
    # imports agent_runner as a callable — not a module-level import of agents).
    from ..runner import run_prompt

    start = time.monotonic()

    role: Optional[AgentRoleManifest] = registry.get(role_name) if role_name else None
    if role_name and role is None:
        console.print(
            f"[muted]  ⚠  Unknown agent role '{role_name}', using researcher.[/]"
        )
        role = registry.get("researcher")

    system_prompt = role.system_prompt if role else _DEFAULT_SUBAGENT_PROMPT
    tools = _resolve_tools(role.tools) if role else None
    max_iter = role.max_iterations if role else 20
    effective_role = role.name if role else "general"

    # Check if a parallel live display is managing output for this thread.
    # When set, route all status output through the callback instead of
    # console.print(), and force silent mode to avoid conflicts with Live.
    display_callback = get_agent_display_callback()

    get_tracer().emit(
        "agent_spawn",
        role=effective_role,
        task=task,
        depth=parent_depth + 1,
    )

    if display_callback:
        display_callback("running")
    else:
        console.print(f"[muted]  ⚙[/]  [bold]\\[{effective_role}][/] [muted]running...[/]")

    conversation = Conversation()
    try:
        result = run_prompt(
            prompt=task,
            client=client,
            conversation=conversation,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=max_iter,
            capture_output=True,           # return text to orchestrator
            agent_depth=parent_depth + 1,  # prevents recursive spawning
            agent_label=effective_role,    # labels LLM text and tool calls
        )
        text = result or "(no response)"
        latency_ms = int((time.monotonic() - start) * 1000)

        if display_callback:
            preview = text.split("\n")[0][:100]
            display_callback("complete", latency_ms=latency_ms, preview=preview)
        else:
            console.print(
                f"[muted]  ✓[/]  [bold]\\[{effective_role}][/] [muted]complete ({latency_ms / 1000:.1f}s)[/]"
            )

        get_tracer().emit(
            "agent_complete",
            role=effective_role,
            task=task[:120],
            result=text,
            result_length=len(text),
            latency_ms=latency_ms,
        )
        return text

    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        if display_callback:
            display_callback("error", error=str(exc))
        else:
            console.print(f"[muted]  ✗[/]  [bold]\\[{effective_role}][/] [muted]error: {exc}[/]")
        get_tracer().emit(
            "agent_error",
            role=effective_role,
            task=task[:120],
            error=str(exc),
        )
        return f"Error in [{effective_role}] subagent: {exc}"
