"""Agent loop: user prompt → [LLM ↔ tools]* → streamed response.

Core of the ReAct pattern: on each iteration the model either finishes
(stop_reason="end_turn") or requests a tool call (stop_reason="tool_use").
Tool results are injected back as observations and the loop continues.

Responsibilities:
  run_prompt()              — orchestrates the full agent loop
  _resolve_mentions()       — expand @file.py references before sending to LLM
  _stream_one_iteration()   — one LLM call: spin → stream events → structured result
  _build_content_blocks()   — assemble content block list for conversation storage
  _execute_tools()          — run each tool call, inject results into conversation
"""

import re
import sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .conversation import Conversation
from .llm.base import ContentTextBlock, ContentToolUseBlock, LLMClient, LLMResponse, StreamComplete, TextChunk, ToolUseBlock
from .reflection import ReflectionConfig, ReflectionResult, reflect
from .theme import (
    BLUE, YELLOW, console,
    print_critique, print_diff, print_error, print_iteration_limit,
    print_reflection_header, print_tool_call, print_usage,
)
from .tools.definitions import SIDE_EFFECTING_TOOLS, TOOL_DEFINITIONS
from .tools.executor import ToolExecutor
from .tracing import get_tracer

MAX_ITERATIONS = 20
_SPINNER_LABEL = f"[{YELLOW}]🍌  Bee-do bee-do...[/]"


def _serialize_messages(messages) -> list:
    """Convert conversation messages to a JSON-serializable list for tracing."""
    import dataclasses
    result = []
    for msg in messages:
        content = msg.content
        if isinstance(content, str):
            content_out = content
        elif isinstance(content, list):
            content_out = []
            for block in content:
                try:
                    content_out.append(dataclasses.asdict(block))
                except Exception:
                    content_out.append(str(block))
        else:
            content_out = str(content)
        result.append({"role": msg.role, "content": content_out})
    return result

# Matches @path patterns that contain at least one / or a file extension.
# Examples: @src/auth.py  @README.md  @config/settings.ts
# Does NOT match bare @property, @classmethod (no slash or extension dot).
_MENTION_RE = re.compile(
    r"@("
    r"(?:\w[\w\-]*/)+[\w.\-]+"           # path/with/dirs/file  e.g. @src/auth.py
    r"|[\w][\w\-]*\.[\w]+(?:\.[\w]+)*"   # bare word.ext        e.g. @README.md
    r"|\.[a-zA-Z][\w\-]*(?:\.[\w]+)*"    # bare dotfile         e.g. @.gitignore, @.env.example
    r")"
)


# ─── Result type for a single streaming iteration ─────────────────────────────

@dataclass
class _IterationResult:
    full_text: str
    tool_blocks: list[ToolUseBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Optional[LLMResponse] = None


# ─── @mention resolution ──────────────────────────────────────────────────────

def _resolve_mentions(prompt: str, cwd: Path) -> str:
    """Expand @file.py references by appending file contents to the prompt.

    Preserves the original mention text inline so the model sees what the
    user typed, then appends the actual file contents at the end.
    Deduplicates repeated mentions of the same file.
    """
    mentions = list(dict.fromkeys(_MENTION_RE.findall(prompt)))  # unique, ordered
    if not mentions:
        return prompt

    appended: list[str] = []
    for mention_path in mentions:
        p = cwd / mention_path
        if not p.exists():
            appended.append(f"[@{mention_path}: file not found]")
        elif not p.is_file():
            appended.append(f"[@{mention_path}: not a file — cannot inject]")
        else:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                appended.append(f"[Contents of {mention_path}]\n{content}")
            except Exception as e:
                appended.append(f"[@{mention_path}: error reading file — {e}]")

    if not appended:
        return prompt
    return prompt + "\n\n" + "\n\n".join(appended)


# ─── Private helpers ──────────────────────────────────────────────────────────

def _stream_one_iteration(
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
) -> Optional[_IterationResult]:
    """Run one LLM streaming call and collect all events into a structured result.

    Shows the spinner while waiting for the first event. Writes TextChunk text
    directly to stdout as it arrives. Returns None on error (already displayed)
    and pops the pending user message so conversation history stays consistent.
    """
    _llm_start = _time.monotonic()
    get_tracer().emit(
        "llm_request",
        message_count=len(conversation.messages),
        messages=_serialize_messages(conversation.messages),
        system=system_prompt,
        tools=TOOL_DEFINITIONS,
        tool_names=[t["name"] for t in TOOL_DEFINITIONS],
        model=getattr(client, "model_id", "unknown"),
        estimated_input_tokens=sum(len(str(m.content)) for m in conversation.messages) // 4,
    )
    try:
        stream = client.stream(conversation.messages, system=system_prompt, tools=TOOL_DEFINITIONS)
        with console.status(_SPINNER_LABEL, spinner="dots"):
            first_event = next(stream, None)
    except Exception as e:
        get_tracer().emit(
            "llm_error",
            error=str(e),
            latency_ms=int((_time.monotonic() - _llm_start) * 1000),
        )
        conversation.messages.pop()
        print_error(str(e))
        return None

    if first_event is None:
        conversation.messages.pop()
        print_error("Received an empty response from the model.")
        return None

    text_chunks: list[str] = []
    tool_blocks: list[ToolUseBlock] = []
    stop_reason = "end_turn"
    usage: Optional[LLMResponse] = None
    printed_prefix = False

    def _process(event) -> None:
        nonlocal printed_prefix, stop_reason, usage
        if isinstance(event, TextChunk):
            if not printed_prefix:
                console.print(f"[bold {BLUE}]minion[/] › ", end="")
                printed_prefix = True
            sys.stdout.write(event.text)
            sys.stdout.flush()
            text_chunks.append(event.text)
        elif isinstance(event, ToolUseBlock):
            tool_blocks.append(event)
        elif isinstance(event, StreamComplete):
            stop_reason = event.stop_reason
            usage = LLMResponse(
                content="",
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                model=event.model,
            )
            get_tracer().emit(
                "llm_response",
                response="".join(text_chunks),
                stop_reason=event.stop_reason,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                model=event.model,
                latency_ms=int((_time.monotonic() - _llm_start) * 1000),
            )

    _process(first_event)
    try:
        for event in stream:
            _process(event)
    except KeyboardInterrupt:
        pass  # Ctrl+C mid-stream — stop cleanly, no traceback

    if text_chunks:
        print()  # newline after streamed text

    return _IterationResult(
        full_text="".join(text_chunks),
        tool_blocks=tool_blocks,
        stop_reason=stop_reason,
        usage=usage,
    )


def _build_content_blocks(result: _IterationResult) -> list:
    """Assemble typed ContentBlocks for an assistant tool-use turn."""
    blocks = []
    if result.full_text:
        blocks.append(ContentTextBlock(text=result.full_text))
    for tb in result.tool_blocks:
        blocks.append(ContentToolUseBlock(id=tb.id, name=tb.name, input=tb.input))
    return blocks


def _execute_tools(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
) -> None:
    """Execute each tool call and inject its result into conversation."""
    for tool_block in tool_blocks:
        result = executor.execute(tool_block)
        conversation.add_tool_result(tool_block.id, result)


# ─── Public entry point ───────────────────────────────────────────────────────

def _run_reflection(
    prompt: str,
    response: str,
    client: LLMClient,
    config: ReflectionConfig,
    verbose: bool,
    conversation: Conversation,
) -> None:
    """Run the self-refine loop and update the conversation if refined.

    Delegates all LLM calls to reflection.reflect(). Passes the full
    conversation history as context so the critic and refiner can see tool
    results (e.g. file contents from read_file calls). Handles display of
    critique and diff when verbose=True. Replaces the last assistant message
    with the refined text when refinement occurred.

    Invariant: called only immediately after conversation.add_assistant(),
    so conversation.messages[-1] is always the draft assistant message.
    """
    from .llm.base import Message

    print_reflection_header(round_num=1, max_rounds=config.depth)
    result = reflect(prompt, response, client, config, context_messages=conversation.messages)

    if verbose:
        for c in result.critiques:
            print_critique(c.score, c.response_type, c.critique)
        if result.was_refined:
            print_diff(response, result.final_response)

    if result.was_refined:
        console.print(f"\n[bold {BLUE}]minion[/] › [muted](refined)[/]")
        console.print(result.final_response)
        # Replace the draft with the refined version so future turns reference
        # the improved response, not the original streaming draft.
        conversation.messages[-1] = Message(
            role="assistant", content=result.final_response
        )
    else:
        score_hint = f" · score: {result.final_score}/10" if verbose else ""
        console.print(f"[muted]  ↳ accepted{score_hint}[/]")


def run_prompt(
    prompt: str,
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    dry_run: bool = False,
    reflect_config: Optional[ReflectionConfig] = None,
    verbose: bool = False,
    memory_tokens: int = 0,
) -> None:
    """Run the ReAct agent loop for a single user prompt.

    Loops up to MAX_ITERATIONS. Each iteration is one LLM call; if the model
    requests tool use the results are observed and the loop continues. The loop
    exits when the model signals end_turn or dry_run stops it after the first
    tool-use iteration.

    When reflect_config is provided and depth > 0, runs the self-refine loop
    after the final end_turn response before returning.
    """
    executor = ToolExecutor(dry_run=dry_run)
    prompt = _resolve_mentions(prompt, Path.cwd())
    conversation.add_user(prompt)
    final_usage: Optional[LLMResponse] = None
    side_effects_occurred = False

    for _ in range(MAX_ITERATIONS):
        # ── One LLM call ──────────────────────────────────────────────────────
        result = _stream_one_iteration(client, conversation, system_prompt)
        if result is None:
            return  # error already displayed and user message popped

        if result.usage:
            final_usage = result.usage

        # ── End turn: model finished responding ───────────────────────────────
        if result.stop_reason == "end_turn":
            conversation.add_assistant(result.full_text, result.usage)
            if reflect_config and reflect_config.depth > 0:
                if side_effects_occurred:
                    console.print("[muted]  ↳ reflection skipped (side-effecting tools were used)[/]")
                else:
                    _run_reflection(
                        prompt=prompt,
                        response=result.full_text,
                        client=client,
                        config=reflect_config,
                        verbose=verbose,
                        conversation=conversation,
                    )
            break

        # ── Tool use: execute calls, inject observations, loop ────────────────
        if result.stop_reason == "tool_use":
            conversation.add_assistant_blocks(_build_content_blocks(result), result.usage)

            if dry_run:
                for tb in result.tool_blocks:
                    print_tool_call(tb.name, tb.input, dry_run=True)
                console.print(f"\n[muted]Dry-run complete. {len(result.tool_blocks)} tool call(s) shown.[/]")
                break

            for tb in result.tool_blocks:
                if tb.name in SIDE_EFFECTING_TOOLS:
                    side_effects_occurred = True

            _execute_tools(result.tool_blocks, executor, conversation)
            print()  # blank line before next iteration

    else:
        # for/else fires when the loop exhausted all iterations without a break
        print_iteration_limit(MAX_ITERATIONS)

    # ── Post-loop: truncation, context snapshot, usage footer ─────────────────
    system_prompt_tokens = len(system_prompt) // 4 - memory_tokens
    if final_usage:
        conversation.truncate_if_needed(final_usage.input_tokens, final_usage.output_tokens)
    snapshot = conversation.build_snapshot(final_usage, system_prompt_tokens, memory_tokens)
    print_usage(snapshot)
