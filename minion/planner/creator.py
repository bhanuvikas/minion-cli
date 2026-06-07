"""Plan creation, refinement, and execution.

create_plan()   — streaming planner loop (explore → markdown document)
_refine_plan()  — revise an existing plan given user feedback
execute_plan()  — inject plan into system prompt and run the ReAct agent
"""

import contextlib
import sys
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..llm.conversation import Conversation
from ..llm.base import (
    ContentTextBlock, ContentToolUseBlock, LLMClient,
    StreamComplete, TextChunk, ToolUseBlock,
)
from ..runner import run_prompt
from ..theme import YELLOW, console, print_error
from ..tools.definitions import TOOL_DEFINITIONS
from ..tools.executor import ToolExecutor
from ..tracing import get_tracer
from .storage import load_plan, save_plan

if TYPE_CHECKING:
    from ..repl import ReplState

# ─── Read-only tool subset for the planner ────────────────────────────────────

_PLANNER_TOOL_NAMES = {"get_file_outline", "search_file", "glob", "read_file", "list_directory"}
PLANNER_TOOL_DEFINITIONS = [t for t in TOOL_DEFINITIONS if t.name in _PLANNER_TOOL_NAMES]

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
    (numbered steps as coarse-grained phases, not micro-tasks — e.g. "Add
    HighScoreManager class", "Integrate with game loop", "Write tests".
    Each step is a meaningful milestone. Include specific function/line references.)
    ## Key Design Decisions
    (choices made during planning and why — table or bullets)
    ## Gotchas
    (things that could go wrong, things to watch for during implementation)
    ## Verification
    (2–4 specific commands only — e.g. run tests, import the module, smoke-test
    the feature. Do not pad with exhaustive test matrices or repeat passing checks.)
    ## Summary Document
    (ONE file only — skip this section entirely if the implementation already
    produces documentation files as deliverables, e.g. README.md, ARCHITECTURE.md,
    DEVELOPMENT.md. Those files ARE the summary. Only add a separate summary file
    if the task produces no user-facing documentation at all.)

QUALITY RULES (apply to all documents):
- Be self-contained. A reader should understand completely without additional context.
- Use exact file paths (relative to project root), function names, and line numbers.
- Never write "TODO: figure this out" or equivalent implementation placeholders.
- Write in the third person: "The implementation adds..." not "I will add...".
- Implementation Order steps are coarse phases, not a micro-task checklist.
- Deferred work must be listed explicitly in a Deferred or Out of Scope section.
- The document ends when it ends — no trailing commentary or sign-off.
"""

# ─── Plan injection header for execution ──────────────────────────────────────

_PLAN_INJECTION_HEADER = """

## Active Mission Plan

The following plan has been approved by the user. Execute it step by step.
Read each section carefully before beginning. Do not skip steps.

Execution discipline — follow these strictly:
- Before doing any work, call todo_write with the Implementation Order steps as
  pending items so the user can see live progress. Mark each step in_progress when
  you begin it and done when it is complete.
- Work through each section in order. Do not skip or reorder steps.
- Run each verification command from the plan's Verification section ONCE.
  If a test passes, move on. Do not re-run tests that have already passed.
- Once verification passes: if the implementation already produced documentation
  files (README.md, ARCHITECTURE.md, DEVELOPMENT.md, etc.) as part of the work,
  stop there — those ARE the summary. Do not create any additional completion,
  mission, or report file on top of them.
- If no documentation was produced as a deliverable, write exactly one summary at
  the path the plan's ## Summary Document section names (or IMPLEMENTATION_NOTES.md).
  Write it once, then stop.
- Never create files like MISSION_COMPLETE.md, COMPLETION_REPORT.md, DELIVERABLES.txt,
  PROJECT_METRICS.txt, or INDEX.md just to announce the task is done. Working code
  and passing tests already say that.
- Do not loop back for "one final check" after already confirming success.
- If the user asks you to commit: run git add + git commit, show the commit
  hash and the list of changed files, then stop. The summary doc and the plan
  already capture what changed — do not write a separate commit summary.

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
    spinner_label: str = "",
    renderer=None,
) -> tuple[str, list[ToolUseBlock], str]:
    """Run one planner streaming call.

    Always collects the response silently — the plan document is rendered as a
    Rich Markdown panel by the caller after the full loop completes, so streaming
    the raw text to stdout would just produce redundant noise before the panel.

    Returns (full_text, tool_blocks, stop_reason).
    Raises on stream error (let caller handle).
    """
    effective_spinner = spinner_label or f"[{YELLOW}]Planning...[/]"
    # In TUI mode suppress console.status() — it gets captured by _CaptureBuf
    # and dumps spinner frames as garbage text. TUI shows its own thinking animation.
    _spinner = (
        contextlib.nullcontext()
        if renderer is not None
        else console.status(effective_spinner, spinner="dots")
    )
    try:
        stream = client.stream(conv.messages, system=PLANNER_SYSTEM_PROMPT, tools=tools)
        with _spinner:
            first_event = next(stream, None)
    except Exception:
        raise

    if first_event is None:
        raise RuntimeError("Planner received an empty response from the model.")

    text_chunks: list[str] = []
    tool_blocks: list[ToolUseBlock] = []
    stop_reason = "end_turn"

    def _process(event) -> None:
        nonlocal stop_reason
        if isinstance(event, TextChunk):
            text_chunks.append(event.text)
        elif isinstance(event, ToolUseBlock):
            tool_blocks.append(event)
        elif isinstance(event, StreamComplete):
            stop_reason = event.stop_reason

    _spinner2 = (
        contextlib.nullcontext()
        if renderer is not None
        else console.status(effective_spinner, spinner="dots")
    )
    with _spinner2:
        _process(first_event)
        try:
            for event in stream:
                _process(event)
        except KeyboardInterrupt:
            pass

    # Flush narration for tool-use turns so user sees LLM's reasoning.
    # Final end_turn text is suppressed — the caller renders the markdown Panel.
    if tool_blocks and text_chunks:
        narration = "".join(text_chunks)
        _tui_app = getattr(renderer, "_app", None) if renderer is not None else None
        if _tui_app is not None:
            from ..tui.render import assistant_turn as _at
            _tui_app.conversation.append_block(_at(narration, "planning"))
        else:
            console.print(f"[bold {YELLOW}]planning[/] › ", end="")
            sys.stdout.write(narration)
            print()

    return "".join(text_chunks), tool_blocks, stop_reason


# ─── Public API ───────────────────────────────────────────────────────────────

def create_plan(
    goal: str,
    client: LLMClient,
    project_context=None,
    recent_messages=None,
    renderer=None,
) -> Optional[PlanResult]:
    """Explore the codebase with read-only tools and produce a markdown plan.

    Uses a fresh Conversation isolated from the main REPL history so planner
    tool calls don't pollute the execution context.

    recent_messages: optional list of recent plain-text Message objects from the
    REPL conversation. Injected as context before the goal so the planner is
    aware of things the user discussed before invoking /plan.

    Returns None if the planner fails or is interrupted.
    """
    t_start = _time.monotonic()
    get_tracer().emit("plan_start", goal=goal)

    conv = Conversation()

    context_block = ""
    if recent_messages:
        lines = []
        for m in recent_messages:
            if isinstance(m.content, str) and m.content.strip():
                label = "User" if m.role == "user" else "Assistant"
                lines.append(f"{label}: {m.content.strip()}")
        if lines:
            context_block = (
                "Recent conversation context (may be relevant to your planning):\n"
                + "\n\n".join(lines)
                + "\n\n---\n\n"
            )

    user_message = (
        f"{context_block}"
        f"**Planning goal:** {goal}\n\n"
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
                client, conv, tools=PLANNER_TOOL_DEFINITIONS, renderer=renderer
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
    permission_store=None,       # PermissionStore | None
    renderer=None,               # OutputRenderer | None
    hook_runner=None,            # HookRunner | None
    mcp_manager=None,            # MCPManager | None
    confirmation_manager=None,   # ConfirmationManager | None
) -> None:
    """Execute a plan by injecting it into the system prompt and running run_prompt.

    The plan document is injected as a system prompt augmentation — it persists
    across all ReAct iterations without being truncated and the agent can
    re-read it at any point.
    """
    from ..llm.reflection import ReflectionConfig

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
        max_iterations=40,
        approval_mode=state.approval_mode,
        permission_store=permission_store,
        renderer=renderer,
        hook_runner=hook_runner,
        mcp_manager=mcp_manager,
        confirmation_manager=confirmation_manager,
    )

    get_tracer().emit("plan_complete", plan_path=str(plan_path))
