import sys
from typing import Optional

import typer
from dotenv import load_dotenv

from . import __version__
from .llm import Message, get_client
from .llm.base import LLMClient
from .prompts import SYSTEM_PROMPT
from .theme import BLUE, YELLOW, console, print_error, print_greeting, print_model_info

load_dotenv()  # load .env before any client is constructed

app = typer.Typer(
    name="minion",
    help="🍌 Minion — your agentic coding assistant.",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
)

# ─── REPL slash commands ──────────────────────────────────────────────────────
REPL_COMMANDS = {
    "/help": "Show available commands",
    "/model": "Show current provider and model",
    "/quit": "Exit Minion (also: /exit, Ctrl+D)",
}


def _handle_slash_command(raw: str, client: LLMClient) -> bool:
    """Process a slash command. Returns True if the input was a slash command."""
    cmd = raw.strip().lower()

    if cmd in ("/quit", "/exit"):
        console.print(f"[{YELLOW}]Poopaye! (Goodbye!) 👋[/]")
        raise typer.Exit()

    if cmd == "/help":
        console.print(f"\n[bold {YELLOW}]Available commands:[/]")
        for command, description in REPL_COMMANDS.items():
            console.print(f"  [{BLUE}]{command:<10}[/]  {description}")
        console.print()
        return True

    if cmd == "/model":
        print_model_info(client.provider_name, client.model_id)
        return True

    if cmd.startswith("/"):
        console.print(
            f"[muted]Unknown command '{cmd}'. Type [bold]/help[/bold] for available commands.[/]"
        )
        return True

    return False


# ─── Core prompt runner ───────────────────────────────────────────────────────

def _run_prompt(prompt: str, client: LLMClient) -> None:
    """Send a prompt, show a spinner until the first token, then stream the rest."""
    messages = [Message(role="user", content=prompt)]

    # Get the stream iterator. Nothing happens yet — generators are lazy.
    stream = client.stream(messages, system=SYSTEM_PROMPT)

    # Show a spinner while we wait for the first token (the actual latency).
    # console.status() cleans up the spinner line before we start printing output.
    try:
        with console.status(f"[{YELLOW}]🍌  Bee-do bee-do...[/]", spinner="dots"):
            first_chunk = next(stream, None)
    except Exception as e:
        print_error(str(e))
        return

    if first_chunk is None:
        print_error("Received an empty response from the model.")
        return

    # Print the "minion › " label then stream the rest directly to stdout.
    # We use sys.stdout.write for streaming chunks to avoid Rich's per-chunk
    # overhead and markup-scanning on raw LLM text.
    console.print(f"[bold {BLUE}]minion[/] › ", end="")
    sys.stdout.write(first_chunk)
    sys.stdout.flush()

    try:
        for chunk in stream:
            sys.stdout.write(chunk)
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass  # Ctrl+C mid-stream — just stop cleanly

    print()  # final newline


# ─── CLI entry points ─────────────────────────────────────────────────────────

@app.command()
def main(
    prompt: Optional[str] = typer.Argument(
        None,
        help="Prompt to send to Minion. Omit to start interactive REPL mode.",
    ),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="LLM provider: anthropic | openai | openrouter",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Model ID (overrides MINION_MODEL env var)",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit.",
        is_eager=True,
    ),
) -> None:
    """🍌 [bold yellow]Minion[/bold yellow] — your agentic coding assistant.

    Run without arguments to start interactive REPL mode.
    Pass a prompt as an argument for a quick one-shot answer.
    """
    if version:
        console.print(f"minion-cli [bold {YELLOW}]v{__version__}[/]")
        raise typer.Exit()

    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    if prompt:
        _run_prompt(prompt, client)
    else:
        _run_repl(client)


def _run_repl(client: LLMClient) -> None:
    print_greeting()
    console.print(f"[muted]Type [bold]/help[/bold] for commands · [bold]/quit[/bold] to exit[/]\n")

    while True:
        try:
            user_input = console.input(f"[bold {YELLOW}]you[/] › ")
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{YELLOW}]Poopaye! 👋[/]")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if _handle_slash_command(user_input, client):
            continue

        _run_prompt(user_input, client)
        console.print()  # blank line between exchanges
