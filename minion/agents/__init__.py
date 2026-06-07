"""Agent roles — orchestrator-worker subagent system.

Public API
----------
load_agent_registry(cwd)        Build the merged registry from all three tiers.
AgentRegistry                   Type alias: dict[str, AgentRoleManifest]
AgentRoleManifest               Dataclass describing one role.
build_subagent_guidance(reg)    Build the system-prompt guidance block from the live registry.
SUBAGENT_GUIDANCE               Static fallback (kept for backward compat; prefers registry).
"""

from pathlib import Path

from .manifest import AgentRoleManifest
from .registry import AgentRegistry, load_agent_registry

__all__ = [
    "AgentRoleManifest",
    "AgentRegistry",
    "load_agent_registry",
    "SUBAGENT_GUIDANCE",
    "build_subagent_guidance",
]

_SUBAGENT_HEADER = """\
## Subagent Capabilities

You have access to `spawn_agent` to delegate focused subtasks to specialized agents.
Each subagent runs in an isolated context with its own tool subset.

Available roles:\
"""

_SUBAGENT_FOOTER = """

Use `spawn_agent` when:
- The task has genuinely independent parts that can run in parallel
- A subtask benefits from a focused, isolated context (e.g. long research before coding)
- The user explicitly asks to use subagents

Do NOT use `spawn_agent` for:
- Simple or single-step questions you can answer directly
- Tasks where each step depends on the previous step's output
- Quick tool calls or lookups that take seconds

Isolation boundary — critical:
Subagents have NO access to the current conversation history or context. Each subagent
starts with a blank slate. Write task descriptions as fully self-contained briefs that
include everything the subagent needs: the goal, relevant file paths or code snippets,
constraints from the conversation, and what "done" looks like. Do not rely on the subagent
knowing anything from context — it doesn't.

Parallel vs. sequential:
Spawn multiple agents simultaneously when their subtasks are truly independent.
Spawn sequentially when a later task depends on an earlier one's output — wait for the
first agent to finish before spawning the next.

Note: subagents cannot spawn further subagents.\
"""

# Static fallback — used when no registry is passed (e.g. one-shot mode without --agents).
SUBAGENT_GUIDANCE = (
    _SUBAGENT_HEADER
    + """
- researcher — gathers information, reads code, produces a report (read-only)
- coder      — implements a specific feature or fix (read + write)
- reviewer   — reviews code for correctness, security, and style (read-only)
- tester     — runs tests, diagnoses failures, and fixes them (read + shell)"""
    + _SUBAGENT_FOOTER
)


def build_subagent_guidance(agent_registry: "AgentRegistry | None" = None) -> str:
    """Build the SUBAGENT_GUIDANCE block from the live registry.

    Generates the 'Available roles' list dynamically so custom agents created
    via /agents are visible to the LLM without restarting the session.
    Falls back to SUBAGENT_GUIDANCE if the registry is empty or None.
    """
    if not agent_registry:
        return SUBAGENT_GUIDANCE
    roles: list[str] = []
    for source in ("builtin", "user", "project"):
        for name, manifest in sorted(
            ((n, m) for n, m in agent_registry.items() if m.source == source),
        ):
            desc = manifest.description.split("\n")[0].rstrip(".") if manifest.description else "no description"
            roles.append(f"- {name:<14} — {desc}")
    return _SUBAGENT_HEADER + "\n" + "\n".join(roles) + _SUBAGENT_FOOTER
