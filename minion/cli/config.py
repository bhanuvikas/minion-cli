"""minion config / model / setup — configuration leaf commands."""

from pathlib import Path
from typing import Optional

import typer

from ..theme import YELLOW, console, print_error


def config_cmd() -> None:
    """Show effective configuration (config.toml + env + CLI flags)."""
    from ..config import format_config, load_config
    cfg = load_config(cwd=Path.cwd())
    console.print(f"\n[bold {YELLOW}]Effective configuration[/] [muted](config.toml + env):[/]\n")
    console.print(format_config(cfg))
    console.print()


def model_cmd(
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
) -> None:
    """Interactively configure provider, model ID, and API keys."""
    from ..config import run_model_config
    from ..llm.factory import get_client
    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)
    run_model_config(client)


def setup_cmd() -> None:
    """Interactive first-run setup — configure your API key and provider."""
    import asyncio
    from ..config import run_setup_wizard
    asyncio.run(run_setup_wizard())
