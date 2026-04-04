"""Plan creation, refinement, and execution.

create_plan()   — streaming planner loop (explore → markdown document)
_refine_plan()  — revise an existing plan given user feedback
execute_plan()  — inject plan into system prompt and run the ReAct agent
"""

import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..conversation import Conversation
from ..llm.base import (
    ContentTextBlock, ContentToolUseBlock, LLMClient,
    StreamComplete, TextChunk, ToolUseBlock,
)
from ..runner import run_prompt
from ..theme import YELLOW, console, print_error, print_tool_call
from ..tools.definitions import TOOL_DEFINITIONS
from ..tools.executor import ToolExecutor
from ..tracing import get_tracer
from .storage import load_plan, save_plan

if TYPE_CHECKING:
    from ..repl import ReplState

# ─── Read-only tool subset for the planner ────────────────────────────────────

_PLANNER_TOOL_NAMES = {"get_file_outline", "search_code", "read_file", "list_directory"}
PLANNER_TOOL_DEFINITIONS = [t for t in TOOL_DEFINITIONS if t["name"] in _PLANNER_TOOL_NAMES]

MAX_PLAN_ITERS = 12

# ─── System prompt ────────────────────────────────────────────────────────────

PLANNER_SYSTEM_PROMPT = """You are Minion's Mission Planner — a meticulous analyst who \
explores codebases before making plans.

YOUR PROCESS:
1. EXPLORE first. Use your tools aggressively. Read relevant files, list directories,
   search for patterns. Understand what already exists before planning anything.
   Do not guess — read the actual code.

2. PLAN last. Once you have enough context, write the complete plan document as
   your final response. The document is your entire output — no preamble, no code
   fences around it, no explanation. Just the document.

GOAL TYPE DETECTION:
Match the goal to one of two document types:

  Exploratory / conceptual goals ("how should I approach", "design a system for",
  "explain how X works") → produce a DESIGN DOCUMENT:

    # [Title]
    ## Problem Statement
    ## Approach & Tradeoffs
    (table: Option | Pros | Cons | Recommendation)
    ## Proposed Architecture
    ## Module / Component Descriptions
    ## Deferred / Out of Scope
    ## Open Questions

  Actionable / implementation goals ("add X", "fix Y", "refactor Z", "implement",
  "build") → produce an IMPLEMENTATION DOCUMENT:

    # [Title]
    ## Goal
    ## Current State
    (what files exist, what patterns are in use — based on your exploration)
    ## File Map
    (every file to be created or modified, one line each: `filename` — what changes)
    ## Implementation Order
    (numbered steps in exact sequence, with specific function/line references)
    ## Key Design Decisions
    (choices made during planning and why — table or bullets)
    ## Gotchas
    (things that could go wrong, things to watch for during implementation)
    ## Verification
    (commands to run, things to check to confirm the implementation works)

QUALITY RULES (apply to all documents):
- Be self-contained. A reader should understand completely without additional context.
- Use exact file paths (relative to project root), function names, and line numbers.
- Never write "TODO: figure this out" or equivalent implementation placeholders.
- Write in the third person: "The implementation adds..." not "I will add...".
- Deferred work must be listed explicitly in a Deferred or Out of Scope section.
- The document ends when it ends — no trailing commentary or sign-off.
"""

# ─── Plan injection header for execution ──────────────────────────────────────

_PLAN_INJECTION_HEADER = """

## Active Mission Plan

The following plan has been approved by the user. Execute it step by step.
Read each section carefully before beginning. Do not skip steps. After completing
all implementation steps, confirm what was done and run any verification commands
listed in the plan.

---

{plan}
"""


# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass
class PlanResult:
    path: Path
    content: str
    goal: str


# ─── Internal streaming helper ────────────────────────────────────────────────

def _stream_planner_iteration(
    client: LLMClient,
    conv: Conversation,
    tools: Optional[list],
) -> tuple[str, list[ToolUseBlock], str]:
    """Run one planner streaming call.

    Returns (full_text, tool_blocks, stop_reason).
    Raises on stream error (let caller handle).
    """
    try:
        stream = client.stream(conv.messages, system=PLANNER_SYSTEM_PROMPT, tools=tools)
        with console.status(f"[{YELLOW}]planning...[/]", spinner="dots"):
            first_event = next(stream, None)
    except Exception:
        raise

    if first_event is None:
        raise RuntimeError("Planner received an empty response from the model.")

    text_chunks: list[str] = []
    tool_blocks: list[ToolUseBlock] = []
    stop_reason = "end_turn"
    printed_prefix = False

    def _process(event) -> None:
        nonlocal printed_prefix, stop_reason
        if isinstance(event, TextChunk):
            if not printed_prefix:
                console.print(f"[bold {YELLOW}]planning[/] › ", end="")
                printed_prefix = True
            sys.stdout.write(event.text)
            sys.stdout.flush()
            text_chunks.append(event.text)
        elif isinstance(event, ToolUseBlock):
            tool_blocks.append(event)
        elif isinstance(event, StreamComplete):
            stop_reason = event.stop_reason

    _process(first_event)
    try:
        for event in stream:
            _process(event)
    except KeyboardInterrupt:
        pass

    if text_chunks:
        print()

    return "".join(text_chunks), tool_blocks, stop_reason


# ─── Public API ───────────────────────────────────────────────────────────────

def create_plan(
    goal: str,
    client: LLMClient,
    project_context=None,
) -> Optional[PlanResult]:
    """Explore the codebase with read-only tools and produce a markdown plan.

    Uses a fresh Conversation isolated from the main REPL history so planner
    tool calls don't pollute the execution context.
    Returns None if the planner fails or is interrupted.
    """
    t_start = _time.monotonic()
    get_tracer().emit("plan_start", goal=goal)

    conv = Conversation()
    user_message = (
        f"{goal}\n\n"
        "First, explore the codebase using your available tools. Read relevant "
        "files, list directories, search for patterns. Understand what exists "
        "before writing anything.\n"
        "When you have gathered enough context, write the complete plan document "
        "as your final response. Output only the document — no preamble, no "
        "fences, no explanation. Just the document."
    )
    conv.add_user(user_message)

    executor = ToolExecutor(dry_run=False)
    plan_content: Optional[str] = None

    for _ in range(MAX_PLAN_ITERS):
        try:
            full_text, tool_blocks, stop_reason = _stream_planner_iteration(
                client, conv, tools=PLANNER_TOOL_DEFINITIONS
            )
        except Exception as e:
            print_error(str(e))
            return None

        if tool_blocks:
            # Build content blocks for the assistant message
            blocks: list = []
            if full_text:
                blocks.append(ContentTextBlock(text=full_text))
            for tb in tool_blocks:
                blocks.append(ContentToolUseBlock(id=tb.id, name=tb.name, input=tb.input))
            conv.add_assistant_blocks(blocks, usage=None)

            # Execute and inject results
            for tb in tool_blocks:
                result = executor.execute(tb)
                conv.add_tool_result(tb.id, result)
            continue

        if stop_reason == "end_turn" and full_text:
            plan_content = full_text
            break

    if not plan_content:
        print_error("Planner did not produce a plan document.")
        return None

    path = save_plan(plan_content, goal)
    get_tracer().emit(
        "plan_generated",
        plan_path=str(path),
        plan_length_chars=len(plan_content),
        generation_time_ms=int((_time.monotonic() - t_start) * 1000),
    )
    return PlanResult(path=path, content=plan_content, goal=goal)


def _refine_plan(
    original_plan: str,
    feedback: str,
    goal: str,
    client: LLMClient,
) -> Optional[str]:
    """Produce a revised plan given user feedback.

    Builds a three-message conversation (goal → original plan → feedback),
    streams the revision with no tools (re-exploration wastes tokens; the
    model already has full context from the original plan).
    Returns the revised plan text, or None on error.
    """
    conv = Conversation()
    conv.add_user(goal)
    conv.add_assistant(original_plan, usage=None)
    conv.add_user(f"Please revise the plan based on this feedback: {feedback}")

    try:
        full_text, _, stop_reason = _stream_planner_iteration(
            client, conv, tools=None
        )
    except Exception as e:
        print_error(str(e))
        return None

    return full_text if full_text else None


def execute_plan(
    plan_path: Path,
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    state: "ReplState",
) -> None:
    """Execute a plan by injecting it into the system prompt and running run_prompt.

    The plan document is injected as a system prompt augmentation — it persists
    across all ReAct iterations without being truncated and the agent can
    re-read it at any point.
    """
    from ..reflection import ReflectionConfig

    plan_content = load_plan(plan_path)
    augmented = system_prompt + _PLAN_INJECTION_HEADER.format(plan=plan_content)

    get_tracer().emit(
        "plan_execute_start",
        plan_path=str(plan_path),
        plan_length_chars=len(plan_content),
    )

    reflect_config = (
        ReflectionConfig(depth=state.reflect_depth)
        if state.reflect_depth > 0 else None
    )

    run_prompt(
        "Execute the mission plan now. Follow the implementation order exactly.",
        client,
        conversation,
        augmented,
        reflect_config=reflect_config,
        verbose=state.verbose,
    )

    get_tracer().emit("plan_complete", plan_path=str(plan_path))
