"""Core execution pipeline: user prompt → LLM → streamed response.

Single responsibility: take a prompt string and a client, call the LLM,
stream the output to the terminal.

From Phase 3 onward this grows into the full agent loop
(ReAct: think → act → observe → repeat), which is why it lives in its own file.
"""

import sys
from typing import Optional

from .conversation import ContextSnapshot, Conversation, _context_limit
from .llm.base import LLMClient, Message
from .prompts import SYSTEM_PROMPT
from .theme import BLUE, YELLOW, console, print_error, print_usage

# Estimated system prompt token count (chars // 4). Computed once at import time
# since SYSTEM_PROMPT is a module-level constant that never changes.
_SYSTEM_PROMPT_TOKENS = len(SYSTEM_PROMPT) // 4


def run_prompt(
    prompt: str,
    client: LLMClient,
    conversation: Optional[Conversation] = None,
) -> None:
    """Send a prompt, show a spinner until the first token, then stream the rest.

    If a Conversation is provided, the prompt is added to history and the
    assistant reply is stored back — enabling multi-turn memory.
    If None (one-shot mode), a single-message list is used and nothing is stored.
    """
    if conversation is not None:
        conversation.add_user(prompt)
        messages = conversation.messages
    else:
        messages = [Message(role="user", content=prompt)]

    # Spinner runs while we block waiting for the first token (real latency).
    # client.stream() is inside the try so API init errors surface cleanly.
    try:
        stream = client.stream(messages, system=SYSTEM_PROMPT)
        with console.status(f"[{YELLOW}]🍌  Bee-do bee-do...[/]", spinner="dots"):
            first_chunk = next(stream, None)
    except Exception as e:
        if conversation is not None and conversation.messages:
            conversation.messages.pop()
        print_error(str(e))
        return

    if first_chunk is None:
        if conversation is not None and conversation.messages:
            conversation.messages.pop()
        print_error("Received an empty response from the model.")
        return

    # sys.stdout.write bypasses Rich's markup scanner on each tiny chunk,
    # which would add unnecessary overhead on high-frequency streaming.
    console.print(f"[bold {BLUE}]minion[/] › ", end="")
    sys.stdout.write(first_chunk)
    sys.stdout.flush()

    chunks = [first_chunk]
    try:
        for chunk in stream:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            chunks.append(chunk)
    except KeyboardInterrupt:
        pass  # Ctrl+C mid-stream — stop cleanly without a traceback

    print()  # final newline

    usage = client.last_usage

    if conversation is not None:
        full_text = "".join(chunks)
        conversation.add_assistant(full_text, usage)
        if usage:
            conversation.truncate_if_needed(usage.input_tokens, usage.output_tokens)
        snapshot = conversation.build_snapshot(usage, _SYSTEM_PROMPT_TOKENS)
    elif usage:
        # One-shot mode: build a minimal snapshot for the footer display
        snapshot = ContextSnapshot(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            context_limit=_context_limit(usage.model),
            session_total=usage.input_tokens + usage.output_tokens,
            turn_count=1,
            system_prompt_tokens=_SYSTEM_PROMPT_TOKENS,
        )
    else:
        snapshot = None

    print_usage(snapshot)
