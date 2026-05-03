"""A2A (Agent-to-Agent) protocol support for minion-cli.

Phase 11 — Global Domination.

Exports:
    load_a2a_manager(cwd)  — loads config, returns A2AManager
    A2A_REMOTE_GUIDANCE    — injected into system prompt when remote agents are configured
    A2AManager             — routes send_task() calls, emits traces
"""

from .manager import A2AManager, load_a2a_manager

# Injected into the system prompt when a2a_manager has configured agents.
# Lists available agent names so the LLM knows what to pass to send_remote_task.
# Updated dynamically in runner.py based on the loaded agent names.
A2A_REMOTE_GUIDANCE = """\
## Remote A2A Agents

You have access to the `send_remote_task` tool to delegate tasks to remote A2A agents.
Remote agents run independently on external systems and return their result as text.

Use `send_remote_task` when:
- A task benefits from a specialized remote agent or external infrastructure
- Tasks are genuinely independent and can run in parallel
- The remote agent has unique access or capabilities not available locally

Do NOT use `send_remote_task` for tasks your local tools can handle directly.

A "full context" task brief must include the goal, relevant file paths or code snippets,
and any constraints — the remote agent has zero knowledge of the current session.

If a remote agent returns an error or is unavailable, report the failure to the user and
offer to handle the task with local tools instead. Do not retry indefinitely.

Available agents are listed in the `send_remote_task` tool description."""

__all__ = ["A2AManager", "load_a2a_manager", "A2A_REMOTE_GUIDANCE"]
