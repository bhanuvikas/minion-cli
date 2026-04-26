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
from .llm import get_client
from .repl import run_repl
from .theme import YELLOW, console, print_error

load_dotenv()  # must run before any LLM client is constructed


app = typer.Typer(
    name="minion",
    help="🍌 Minion — your agentic coding assistant.",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
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

    Run to start interactive REPL mode.
    """
    if ctx.invoked_subcommand is not None:
        return

    if version:
        console.print(f"minion-cli [bold {YELLOW}]v{__version__}[/]")
        raise typer.Exit()

    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    if not no_trace:
        from .tracing import init_tracer
        init_tracer(session_id=str(uuid.uuid4()))

    run_repl(
        client, dry_run=dry_run, reflect_depth=reflect or 0,
        verbose=verbose, memory_enabled=not no_memory, debug=debug,
    )


# ─── `minion skills` subcommand ───────────────────────────────────────────────

_skills_app = typer.Typer(name="skills", help="Manage Minion skills.", add_completion=False)
app.add_typer(_skills_app, name="skills")


@_skills_app.callback(invoke_without_command=True)
def _skills_main(ctx: typer.Context) -> None:
    """List or manage skills. Run without subcommand to list all skills."""
    if ctx.invoked_subcommand is None:
        _list_skills()


@_skills_app.command("list")
def skills_list() -> None:
    """List all available skills (builtin, user, and project)."""
    _list_skills()


def _list_skills() -> None:
    from .skills import load_skill_registry
    registry = load_skill_registry()
    for name, skill in registry.items():
        console.print(f"  [bold {YELLOW}]/{name:<14}[/] [{skill.source}] {skill.description}")


# ─── `minion mcp` subcommand ──────────────────────────────────────────────────

_mcp_app = typer.Typer(name="mcp", help="Manage MCP server connections.", add_completion=False)
app.add_typer(_mcp_app, name="mcp")


@_mcp_app.callback(invoke_without_command=True)
def _mcp_main(ctx: typer.Context) -> None:
    """List MCP-connected servers and tools. Run without subcommand to list all."""
    if ctx.invoked_subcommand is None:
        _list_mcp()


@_mcp_app.command("list")
def mcp_list() -> None:
    """List all MCP servers and their tools (from ~/.minion/mcp.json and .minion/mcp.json)."""
    _list_mcp()


def _list_mcp() -> None:
    from .mcp import load_mcp_manager
    manager = load_mcp_manager(Path.cwd())
    summary = manager.server_summary()
    if not summary:
        console.print(
            "[muted]No MCP servers connected. "
            "Add servers to ~/.minion/mcp.json or .minion/mcp.json[/]"
        )
        manager.shutdown()
        return
    total_tools = sum(len(s["tools"]) for s in summary)
    total_resources = sum(len(s["resources"]) for s in summary)
    total_prompts = sum(len(s["prompts"]) for s in summary)
    console.print(
        f"[bold {YELLOW}]MCP servers[/] [muted]("
        f"{total_tools} tools, {total_resources} resources, {total_prompts} prompts):[/]"
    )
    for s in summary:
        console.print(
            f"  [bold {YELLOW}]{s['name']}[/]  "
            f"[muted]{len(s['tools'])}t · {len(s['resources'])}r · {len(s['prompts'])}p[/]"
        )
        for t in s["tools"]:
            console.print(f"    · tool     {t}")
        for r in s["resources"]:
            console.print(f"    · resource {r['uri']}")
        for p in s["prompts"]:
            console.print(f"    · prompt   {s['name']}__{p['name']}")
    manager.shutdown()


# ─── `minion agent` subcommand ────────────────────────────────────────────────

_agent_app = typer.Typer(name="agent", help="Manage and run agent roles.", add_completion=False)
app.add_typer(_agent_app, name="agent")


@_agent_app.callback(invoke_without_command=True)
def _agent_main(ctx: typer.Context) -> None:
    """List available roles or run one. Run without subcommand to list all roles."""
    if ctx.invoked_subcommand is None:
        _list_agents()


@_agent_app.command("list")
def agent_list() -> None:
    """List all available agent roles with descriptions and tool subsets."""
    _list_agents()


@_agent_app.command("run")
def agent_run(
    role: str = typer.Argument(..., help="Role name: researcher, coder, reviewer, tester"),
    task: str = typer.Argument(..., help="Task for the agent to complete"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
) -> None:
    """Run a specific agent role on a task (one-shot)."""
    from .agents import load_agent_registry
    from .agents.runner import run_agent
    from .llm.factory import get_client

    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    registry = load_agent_registry(Path.cwd())
    result = run_agent(task, role, registry, client, parent_depth=0)
    console.print(result)


def _list_agents() -> None:
    from .agents import load_agent_registry
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
