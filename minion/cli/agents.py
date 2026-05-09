"""minion agents — list and run agent roles."""

from pathlib import Path
from typing import Optional

import typer

from ..theme import YELLOW, console, print_error

app = typer.Typer(name="agents", help="Manage and run agent roles.", add_completion=False)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """List available roles or run one. Run without subcommand to list all roles."""
    if ctx.invoked_subcommand is None:
        _list_agents()


@app.command("list")
def agents_list() -> None:
    """List all available agent roles with descriptions and tool subsets."""
    _list_agents()


@app.command("run")
def agents_run(
    role: str = typer.Argument(..., help="Role name: researcher, coder, reviewer, tester"),
    task: str = typer.Argument(..., help="Task for the agent to complete"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
) -> None:
    """Run a specific agent role on a task (one-shot)."""
    from ..agents import load_agent_registry
    from ..agents.runner import run_agent
    from ..llm.factory import get_client

    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    registry = load_agent_registry(Path.cwd())
    result = run_agent(task, role, registry, client, parent_depth=0)
    console.print(result)


def _list_agents() -> None:
    from ..agents import load_agent_registry
    registry = load_agent_registry(Path.cwd())
    if not registry:
        console.print("[muted]No agent roles found.[/]")
        return
    for name, role in sorted(registry.items()):
        tools_str = ", ".join(role.tools) if role.tools else "all tools"
        console.print(
            f"  [bold {YELLOW}]{name:<14}[/] [{role.source}] {role.description}\n"
            f"  {'':14}  [muted]tools: {tools_str}[/]"
        )
