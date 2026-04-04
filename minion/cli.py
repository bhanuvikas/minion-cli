"""CLI entry point — argument parsing and delegation only.

Single responsibility: define the typer app, parse CLI arguments,
and hand off to the right module. No business logic lives here.
"""

import uuid
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from . import __version__
from .context import build_project_context
from .conversation import Conversation
from .llm import get_client
from .prompts import build_system_prompt
from .reflection import ReflectionConfig
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
    invoke_without_command=True,
    # Allow extra positional args so subcommand names (e.g. "trace") are not
    # consumed as the PROMPT argument before subcommand dispatch occurs.
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p",
        help="LLM provider: anthropic | openai | openrouter",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help="Model ID (overrides MINION_MODEL env var)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show tool calls without executing them.",
    ),
    reflect: Optional[int] = typer.Option(
        None, "--reflect",
        help="Enable self-refine reflection. Pass depth (1-3). Example: --reflect 1",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Show critique text and response diffs during reflection.",
    ),
    no_memory: bool = typer.Option(
        False, "--no-memory",
        help="Disable memory retrieval and extraction for this session (private mode).",
    ),
    debug: bool = typer.Option(
        False, "--debug",
        help="Print system prompt and debug info before each turn.",
    ),
    no_trace: bool = typer.Option(
        False, "--no-trace",
        help="Disable session tracing (no .jsonl written to ~/.minion/traces/).",
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
    if ctx.invoked_subcommand is not None:
        return

    if version:
        console.print(f"minion-cli [bold {YELLOW}]v{__version__}[/]")
        raise typer.Exit()

    # Extra positional args (from allow_extra_args=True) become the prompt.
    # This lets subcommand names like "trace" dispatch correctly without
    # being consumed as the prompt argument.
    prompt = " ".join(ctx.args) if ctx.args else None

    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    if prompt:
        project_context = build_project_context(Path.cwd())
        system_prompt = build_system_prompt(project_context)
        reflect_config = ReflectionConfig(depth=reflect) if reflect else None
        run_prompt(
            prompt, client, Conversation(), system_prompt,
            dry_run=dry_run,
            reflect_config=reflect_config,
            verbose=verbose,
        )
    else:
        # REPL mode — initialize tracer unless --no-trace
        if not no_trace:
            from .tracing import init_tracer
            init_tracer(session_id=str(uuid.uuid4()))
        run_repl(
            client, dry_run=dry_run, reflect_depth=reflect or 0,
            verbose=verbose, memory_enabled=not no_memory, debug=debug,
        )


