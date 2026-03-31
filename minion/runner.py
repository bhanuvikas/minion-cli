"""Agent loop: user prompt → [LLM ↔ tools]* → streamed response.

The ReAct pattern lives here: on each iteration the model either emits text
(done) or a tool call (execute, inject result, loop). runner.py orchestrates;
tools/ executes; theme.py displays.
"""

import sys
from typing import Optional

from .conversation import Conversation
from .llm.base import LLMClient, LLMResponse, StreamComplete, TextChunk, ToolUseBlock
from .prompts import SYSTEM_PROMPT
from .theme import BLUE, YELLOW, console, print_error, print_iteration_limit, print_usage
from .tools.definitions import TOOL_DEFINITIONS
from .tools.executor import ToolExecutor

_SYSTEM_PROMPT_TOKENS = len(SYSTEM_PROMPT) // 4
MAX_ITERATIONS = 20


def run_prompt(
    prompt: str,
    client: LLMClient,
    conversation: Conversation,
    dry_run: bool = False,
) -> None:
    """Run the ReAct agent loop for a single user prompt.

    Streams text to stdout, executes tool calls, injects observations, and
    loops until the model signals end_turn or MAX_ITERATIONS is reached.
    """
    executor = ToolExecutor(dry_run=dry_run)
    conversation.add_user(prompt)
    final_usage: Optional[LLMResponse] = None

    for iteration in range(MAX_ITERATIONS):
        # ── Spin until the first event arrives ────────────────────────────────
        try:
            stream = client.stream(conversation.messages, system=SYSTEM_PROMPT, tools=TOOL_DEFINITIONS)
            with console.status(f"[{YELLOW}]🍌  Bee-do bee-do...[/]", spinner="dots"):
                first_event = next(stream, None)
        except Exception as e:
            conversation.messages.pop()
            print_error(str(e))
            return

        if first_event is None:
            conversation.messages.pop()
            print_error("Received an empty response from the model.")
            return

        # ── Process the full stream for this iteration ────────────────────────
        text_chunks: list[str] = []
        tool_blocks: list[ToolUseBlock] = []
        stop_reason = "end_turn"
        iteration_usage: Optional[LLMResponse] = None
        printed_prefix = False

        def _handle_event(event) -> None:
            nonlocal printed_prefix, stop_reason, iteration_usage
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
                iteration_usage = LLMResponse(
                    content="",
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    model=event.model,
                )

        _handle_event(first_event)
        try:
            for event in stream:
                _handle_event(event)
        except KeyboardInterrupt:
            pass  # Ctrl+C mid-stream — stop cleanly

        if text_chunks:
            print()  # newline after streamed text

        if iteration_usage:
            final_usage = iteration_usage

        # ── Build content blocks (text + tool_use) for this assistant turn ────
        content_blocks: list[dict] = []
        full_text = "".join(text_chunks)
        if full_text:
            content_blocks.append({"type": "text", "text": full_text})
        for tb in tool_blocks:
            content_blocks.append({"type": "tool_use", "id": tb.id, "name": tb.name, "input": tb.input})

        # ── End turn: store reply and finish ──────────────────────────────────
        if stop_reason == "end_turn":
            conversation.add_assistant(full_text, iteration_usage)
            break

        # ── Tool use: store blocks, execute, inject results, loop ─────────────
        if stop_reason == "tool_use":
            conversation.add_assistant_blocks(content_blocks, iteration_usage)

            for tool_block in tool_blocks:
                result = executor.execute(tool_block)
                conversation.add_tool_result(tool_block.id, result)

            if dry_run:
                console.print(f"\n[muted]Dry-run complete. {len(tool_blocks)} tool call(s) shown.[/]")
                break

            print()  # blank line before next LLM iteration

    else:
        # for/else: loop exhausted without break — hit MAX_ITERATIONS
        print_iteration_limit(MAX_ITERATIONS)

    # ── Post-loop: truncation, snapshot, footer ───────────────────────────────
    if final_usage:
        conversation.truncate_if_needed(final_usage.input_tokens, final_usage.output_tokens)
    snapshot = conversation.build_snapshot(final_usage, _SYSTEM_PROMPT_TOKENS)
    print_usage(snapshot)
