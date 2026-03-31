"""CLI entry point — argument parsing and delegation only.

Single responsibility: define the typer app, parse CLI arguments,
and hand off to the right module. No business logic lives here.
"""

from typing import Optional

import typer
from dotenv import load_dotenv

from . import __version__
from .llm import get_client
from .repl import run_repl
from .runner import run_prompt
from .theme import YELLOW, console, print_error

load_dotenv()  # must run before any LLM client is constructed

app = typer.Typer(
    name="minion",
    help="🍌 Minion — your agentic coding assistant.",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
)


@app.command()
def main(
    prompt: Optional[str] = typer.Argument(
        None,
        help="Prompt to send to Minion. Omit to start interactive REPL mode.",
    ),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p",
        help="LLM provider: anthropic | openai | openrouter",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help="Model ID (overrides MINION_MODEL env var)",
    ),
    version: bool = typer.Option(
        False, "--version",
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
        run_prompt(prompt, client)
    else:
        run_repl(client)
