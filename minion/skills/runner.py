"""Skill execution — augment system prompt, filter tools, delegate to run_prompt().

Execution follows the same pattern as execute_plan() in planner/creator.py:
  1. Augment the base system prompt with the skill's instructions
  2. Resolve the tool subset the skill is allowed to use
  3. Call run_prompt() — the existing ReAct agent handles the rest

Skill chaining: if a skill has steps: ["test", "review"], execute_skill() calls
itself recursively for each step in order. The shared conversation accumulates
context across steps so later steps see earlier results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..llm.conversation import Conversation
from ..llm.base import LLMClient, ToolDefinition
from ..llm.reflection import ReflectionConfig
from ..runner import run_prompt
from ..theme import console
from ..tools.definitions import TOOL_DEFINITIONS
from ..tracing import get_tracer
from .manifest import SkillManifest
from .registry import SkillRegistry

if TYPE_CHECKING:
    from ..repl import ReplState


def execute_skill(
    skill: SkillManifest,
    arg: str,
    client: LLMClient,
    conversation: Conversation,
    base_system_prompt: str,
    registry: SkillRegistry,
    state: Optional["ReplState"] = None,
    _chain: frozenset[str] = frozenset(),
) -> None:
    """Execute a skill — chained or direct.

    For chained skills (steps: [name, ...]), calls execute_skill() for each
    step in order using the shared conversation and registry.

    For direct skills, augments the system prompt, builds the user message,
    and delegates to run_prompt().

    Args:
        skill:              The manifest to execute.
        arg:                User-provided argument (e.g. the file path in /review src/auth.py).
        client:             LLM client for the current session.
        conversation:       Shared conversation — context accumulates across skill steps.
        base_system_prompt: The REPL's current system prompt (already includes project context).
        registry:           Used to resolve step names for chained skills.
        state:              REPL state for reflect_depth and verbose; may be None in tests.
        _chain:             Internal — tracks skill names already in the call stack to detect cycles.
    """
    # ── Skill chaining ─────────────────────────────────────────────────────────
    if skill.steps:
        for step_name in skill.steps:
            if step_name in _chain:
                console.print(
                    f"[red]Circular skill chain detected: '{step_name}' is already in the chain "
                    f"{sorted(_chain)}[/]"
                )
                return
            step = registry.get(step_name)
            if step is None:
                console.print(f"[red]Poulet tikka masala! Unknown skill in chain: '{step_name}'[/]")
                return
            execute_skill(step, arg, client, conversation, base_system_prompt, registry, state,
                          _chain | {skill.name})
        return

    # ── Direct execution ───────────────────────────────────────────────────────
    rendered = skill.prompt.replace("{arg}", arg) if "{arg}" in skill.prompt else skill.prompt
    augmented = base_system_prompt + f"\n\n## Active Skill: /{skill.name}\n\n{rendered}"

    user_msg = (
        f"Run the /{skill.name} skill. Target: {arg}"
        if arg
        else f"Run the /{skill.name} skill."
    )

    reflect_config = (
        ReflectionConfig(depth=state.reflect_depth)
        if state and state.reflect_depth > 0
        else None
    )

    get_tracer().emit("skill_start", skill_name=skill.name, arg=arg, source=skill.source)

    render_markdown = skill.output_format == "markdown"
    spinner_label = (
        f"[yellow]🍌  {skill.thinking_label}...[/]"
        if skill.thinking_label
        else None
    )

    run_prompt(
        user_msg,
        client,
        conversation,
        augmented,
        reflect_config=reflect_config,
        verbose=state.verbose if state else False,
        tools=_resolve_tools(skill.tools),
        max_iterations=skill.max_iterations,
        render_markdown=render_markdown,
        markdown_title=f"/{skill.name}",
        spinner_label=spinner_label,
    )

    get_tracer().emit("skill_complete", skill_name=skill.name, arg=arg)


def _resolve_tools(tool_names: Optional[list[str]]) -> Optional[list[ToolDefinition]]:
    """Translate a skill's tool name list to TOOL_DEFINITIONS dicts.

    Returns:
        None  — skill allows all tools (run_prompt defaults to TOOL_DEFINITIONS)
        []    — skill allows no tools
        [...]  — filtered subset of TOOL_DEFINITIONS matching tool_names
    """
    if tool_names is None:
        return None
    name_set = set(tool_names)
    return [t for t in TOOL_DEFINITIONS if t.name in name_set]
