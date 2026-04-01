"""Agent loop: user prompt → [LLM ↔ tools]* → streamed response.

Core of the ReAct pattern: on each iteration the model either finishes
(stop_reason="end_turn") or requests a tool call (stop_reason="tool_use").
Tool results are injected back as observations and the loop continues.

Responsibilities:
  run_prompt()          — orchestrates the full agent loop
  _stream_one_iteration() — one LLM call: spin → stream events → structured result
  _build_content_blocks() — assemble content block list for conversation storage
  _execute_tools()        — run each tool call, inject results into conversation
"""

import sys
from dataclasses import dataclass, field
from typing import Optional

from .conversation import Conversation
from .llm.base import ContentTextBlock, ContentToolUseBlock, LLMClient, LLMResponse, StreamComplete, TextChunk, ToolUseBlock
from .prompts import SYSTEM_PROMPT
from .theme import BLUE, YELLOW, console, print_error, print_iteration_limit, print_tool_call, print_usage
from .tools.definitions import TOOL_DEFINITIONS
from .tools.executor import ToolExecutor

_SYSTEM_PROMPT_TOKENS = len(SYSTEM_PROMPT) // 4
MAX_ITERATIONS = 20
_SPINNER_LABEL = f"[{YELLOW}]🍌  Bee-do bee-do...[/]"


# ─── Result type for a single streaming iteration ─────────────────────────────

@dataclass
class _IterationResult:
    full_text: str
    tool_blocks: list[ToolUseBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Optional[LLMResponse] = None


# ─── Private helpers ──────────────────────────────────────────────────────────

def _stream_one_iteration(
    client: LLMClient,
    conversation: Conversation,
) -> Optional[_IterationResult]:
    """Run one LLM streaming call and collect all events into a structured result.

    Shows the spinner while waiting for the first event. Writes TextChunk text
    directly to stdout as it arrives. Returns None on error (already displayed)
    and pops the pending user message so conversation history stays consistent.
    """
    try:
        stream = client.stream(conversation.messages, system=SYSTEM_PROMPT, tools=TOOL_DEFINITIONS)
        with console.status(_SPINNER_LABEL, spinner="dots"):
            first_event = next(stream, None)
    except Exception as e:
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
    """Assemble typed ContentBlocks for an assistant tool-use turn.

    Stores both the text preamble (if any) and tool_use blocks so subsequent
    LLM calls can match tool_result messages back to their tool_use IDs.
    Adapters handle translation to provider wire format.
    """
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
    """Execute each tool call and inject its result into conversation as a tool_result message."""
    for tool_block in tool_blocks:
        result = executor.execute(tool_block)
        conversation.add_tool_result(tool_block.id, result)


# ─── Public entry point ───────────────────────────────────────────────────────

def run_prompt(
    prompt: str,
    client: LLMClient,
    conversation: Conversation,
    dry_run: bool = False,
) -> None:
    """Run the ReAct agent loop for a single user prompt.

    Loops up to MAX_ITERATIONS. Each iteration is one LLM call; if the model
    requests tool use the results are observed and the loop continues. The loop
    exits when the model signals end_turn or dry_run stops it after the first
    tool-use iteration.
    """
    executor = ToolExecutor(dry_run=dry_run)
    conversation.add_user(prompt)
    final_usage: Optional[LLMResponse] = None

    for _ in range(MAX_ITERATIONS):
        # ── One LLM call ──────────────────────────────────────────────────────
        result = _stream_one_iteration(client, conversation)
        if result is None:
            return  # error already displayed and user message popped

        if result.usage:
            final_usage = result.usage

        # ── End turn: model finished responding ───────────────────────────────
        if result.stop_reason == "end_turn":
            conversation.add_assistant(result.full_text, result.usage)
            break

        # ── Tool use: execute calls, inject observations, loop ────────────────
        if result.stop_reason == "tool_use":
            conversation.add_assistant_blocks(_build_content_blocks(result), result.usage)

            if dry_run:
                for tb in result.tool_blocks:
                    print_tool_call(tb.name, tb.input, dry_run=True)
                console.print(f"\n[muted]Dry-run complete. {len(result.tool_blocks)} tool call(s) shown.[/]")
                break

            _execute_tools(result.tool_blocks, executor, conversation)
            print()  # blank line before next iteration

    else:
        # for/else fires when the loop exhausted all iterations without a break
        print_iteration_limit(MAX_ITERATIONS)

    # ── Post-loop: truncation, context snapshot, usage footer ─────────────────
    if final_usage:
        conversation.truncate_if_needed(final_usage.input_tokens, final_usage.output_tokens)
    snapshot = conversation.build_snapshot(final_usage, _SYSTEM_PROMPT_TOKENS)
    print_usage(snapshot)
