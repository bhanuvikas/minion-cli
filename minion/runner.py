"""Agent loop: user prompt → [LLM ↔ tools]* → streamed response.

The ReAct pattern lives here: on each iteration the model either emits text
(done) or a tool call (execute, inject result, loop). runner.py orchestrates;
tools/ executes; theme.py displays.

Phase 2 run_prompt() was a straight pipeline: prompt → stream → done.
Phase 3 run_prompt() is a loop: prompt → stream events → [tool? → observe → repeat].
"""

import sys
from typing import Optional

from .conversation import ContextSnapshot, Conversation, _context_limit
from .llm.base import LLMClient, LLMResponse, Message, StreamComplete, TextChunk, ToolUseBlock
from .prompts import SYSTEM_PROMPT
from .theme import BLUE, YELLOW, console, print_error, print_iteration_limit, print_usage
from .tools.definitions import TOOL_DEFINITIONS
from .tools.executor import ToolExecutor

_SYSTEM_PROMPT_TOKENS = len(SYSTEM_PROMPT) // 4
MAX_ITERATIONS = 20


def run_prompt(
    prompt: str,
    client: LLMClient,
    conversation: Optional[Conversation] = None,
    dry_run: bool = False,
) -> None:
    """Run the ReAct agent loop for a single user prompt.

    Streams text to stdout, executes tool calls, injects observations, and
    loops until the model signals end_turn or MAX_ITERATIONS is reached.

    conversation=None keeps one-shot behaviour from Phase 1 — nothing is stored.
    dry_run=True displays tool calls without executing them.
    """
    executor = ToolExecutor(dry_run=dry_run)

    if conversation is not None:
        conversation.add_user(prompt)
        messages = conversation.messages
    else:
        # One-shot: maintain a local message list for this call only
        messages = [Message(role="user", content=prompt)]

    final_usage: Optional[LLMResponse] = None

    for iteration in range(MAX_ITERATIONS):
        # ── Spin until the first event arrives ────────────────────────────────
        try:
            stream = client.stream(messages, system=SYSTEM_PROMPT, tools=TOOL_DEFINITIONS)
            with console.status(f"[{YELLOW}]🍌  Bee-do bee-do...[/]", spinner="dots"):
                first_event = next(stream, None)
        except Exception as e:
            _pop_last_user(conversation, messages)
            print_error(str(e))
            return

        if first_event is None:
            _pop_last_user(conversation, messages)
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

        # ── Build content blocks to store in conversation ─────────────────────
        # Preserves the exact structure the Anthropic API requires for subsequent
        # calls: text block (if any) followed by tool_use blocks.
        content_blocks: list[dict] = []
        full_text = "".join(text_chunks)
        if full_text:
            content_blocks.append({"type": "text", "text": full_text})
        for tb in tool_blocks:
            content_blocks.append({"type": "tool_use", "id": tb.id, "name": tb.name, "input": tb.input})

        # ── End turn: store reply, wrap up ────────────────────────────────────
        if stop_reason == "end_turn":
            if conversation is not None:
                conversation.add_assistant(full_text, iteration_usage)
            break

        # ── Tool use: execute each tool, inject results, loop ────────────────
        if stop_reason == "tool_use":
            if conversation is not None:
                conversation.add_assistant_blocks(content_blocks, iteration_usage)
            else:
                messages.append(Message(role="assistant", content=content_blocks))

            for tool_block in tool_blocks:
                result = executor.execute(tool_block)
                if conversation is not None:
                    conversation.add_tool_result(tool_block.id, result)
                else:
                    messages.append(Message(
                        role="user",
                        content=[{"type": "tool_result", "tool_use_id": tool_block.id, "content": result}],
                    ))

            # In dry-run mode we've shown what would be called — stop here.
            # Continuing would loop with placeholder results and confuse the model.
            if dry_run:
                console.print(f"\n[muted]Dry-run complete. {len(tool_blocks)} tool call(s) shown.[/]")
                break

            print()  # blank line before next LLM iteration

    else:
        # for/else: loop exhausted without a break — hit MAX_ITERATIONS
        print_iteration_limit(MAX_ITERATIONS)

    # ── Post-loop: truncation, snapshot, footer ───────────────────────────────
    if conversation is not None:
        if final_usage:
            conversation.truncate_if_needed(final_usage.input_tokens, final_usage.output_tokens)
        snapshot = conversation.build_snapshot(final_usage, _SYSTEM_PROMPT_TOKENS)
    elif final_usage:
        snapshot = ContextSnapshot(
            model=final_usage.model,
            input_tokens=final_usage.input_tokens,
            output_tokens=final_usage.output_tokens,
            context_limit=_context_limit(final_usage.model),
            session_total=final_usage.input_tokens + final_usage.output_tokens,
            turn_count=1,
            system_prompt_tokens=_SYSTEM_PROMPT_TOKENS,
        )
    else:
        snapshot = None

    print_usage(snapshot)


def _pop_last_user(
    conversation: Optional[Conversation],
    messages: list[Message],
) -> None:
    """Remove the most-recently-added user message on error.

    Keeps conversation history in a consistent user/assistant alternating state
    so the next prompt doesn't start with two consecutive user messages.
    """
    if conversation is not None:
        if conversation.messages:
            conversation.messages.pop()
    else:
        if messages:
            messages.pop()
