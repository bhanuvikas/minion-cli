"""Core execution pipeline: user prompt → LLM → streamed response.

Single responsibility: take a prompt string and a client, call the LLM,
stream the output to the terminal.

This module is intentionally small right now. From Phase 3 onward it grows
into the full agent loop (ReAct: think → act → observe → repeat), which is
why it lives in its own file rather than inside cli.py.
"""

import sys

from .llm.base import LLMClient, Message
from .prompts import SYSTEM_PROMPT
from .theme import BLUE, YELLOW, console, print_error, print_usage


def run_prompt(prompt: str, client: LLMClient) -> None:
    """Send a prompt, show a spinner until the first token, then stream the rest."""
    messages = [Message(role="user", content=prompt)]

    # Generators are lazy — nothing happens until next() is called.
    stream = client.stream(messages, system=SYSTEM_PROMPT)

    # Spinner runs while we block waiting for the first token (real latency).
    # console.status() clears the spinner line before we start printing.
    try:
        with console.status(f"[{YELLOW}]🍌  Bee-do bee-do...[/]", spinner="dots"):
            first_chunk = next(stream, None)
    except Exception as e:
        print_error(str(e))
        return

    if first_chunk is None:
        print_error("Received an empty response from the model.")
        return

    # sys.stdout.write bypasses Rich's markup scanner on each tiny chunk,
    # which would add unnecessary overhead on high-frequency streaming.
    console.print(f"[bold {BLUE}]minion[/] › ", end="")
    sys.stdout.write(first_chunk)
    sys.stdout.flush()

    try:
        for chunk in stream:
            sys.stdout.write(chunk)
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass  # Ctrl+C mid-stream — stop cleanly without a traceback

    print()  # final newline
    print_usage(client.last_usage)
