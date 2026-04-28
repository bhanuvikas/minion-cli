"""CLI entry point — argument parsing and delegation only.

Single responsibility: define the typer app, parse CLI arguments,
and hand off to the right module. No business logic lives here.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from . import __version__
from .llm import get_client
from .repl import run_repl, run_repl_async
from .theme import YELLOW, console, print_error

load_dotenv()  # must run before any LLM client is constructed

from .config_file import load_config as _load_config  # noqa: E402 — after dotenv


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

    cfg = _load_config()

    # CLI flags override config.toml defaults
    effective_provider = provider or cfg.llm.provider
    effective_model = model or cfg.llm.model
    effective_reflect = reflect if reflect is not None else cfg.agent.reflect_depth
    effective_verbose = verbose or cfg.agent.verbose
    effective_debug = debug or cfg.agent.debug
    effective_memory = not no_memory and cfg.memory.enabled
    effective_trace = not no_trace and cfg.tracing.enabled

    try:
        client = get_client(effective_provider, effective_model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    if effective_trace:
        from .tracing import init_tracer
        init_tracer(session_id=str(uuid.uuid4()))

    asyncio.run(run_repl_async(
        client, dry_run=dry_run, reflect_depth=effective_reflect,
        verbose=effective_verbose, memory_enabled=effective_memory,
        debug=effective_debug,
    ))


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


# ─── `minion a2a` subcommand ──────────────────────────────────────────────────

_a2a_app = typer.Typer(name="a2a", help="A2A agent protocol — serve and connect to remote agents.", add_completion=False)
app.add_typer(_a2a_app, name="a2a")


@_a2a_app.callback(invoke_without_command=True)
def _a2a_main(ctx: typer.Context) -> None:
    """List configured remote agents or start an A2A server."""
    if ctx.invoked_subcommand is None:
        _list_a2a()


@_a2a_app.command("list")
def a2a_list() -> None:
    """List configured remote A2A agents and their capabilities."""
    _list_a2a()


@_a2a_app.command("serve")
def a2a_serve(
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on."),
    host: str = typer.Option("localhost", "--host", help="Host to bind to."),
    provider: Optional[str] = typer.Option(None, "--provider", "-P"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
) -> None:
    """Start an A2A HTTP server — exposes minion as a remote A2A agent.

    Clients can submit tasks via POST /tasks/send or POST /tasks/sendSubscribe
    and discover capabilities at GET /.well-known/agent.json.
    """
    from .a2a.server import A2AServer
    from .context import build_project_context
    from .conversation import Conversation
    from .prompts import build_system_prompt
    from .runner import run_prompt

    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)

    project_context = build_project_context(Path.cwd())
    base_system_prompt = build_system_prompt(project_context)

    def agent_runner(task_text: str, confirm_callback=None) -> str:
        conversation = Conversation(model=getattr(client, "model_id", "unknown"))
        result = run_prompt(
            task_text, client, conversation, base_system_prompt,
            capture_output=True,
            confirm_callback=confirm_callback,
        )
        return result or "(no response)"

    server = A2AServer(host=host, port=port, agent_runner=agent_runner)
    console.print(
        f"[bold {YELLOW}]A2A server[/] listening at [bold]http://{host}:{port}[/]"
    )
    console.print(f"[muted]  Agent Card: http://{host}:{port}/.well-known/agent.json[/]")
    console.print(f"[muted]  Ctrl+C to stop[/]\n")
    server.start()


def _list_a2a() -> None:
    from .a2a import load_a2a_manager
    manager = load_a2a_manager(Path.cwd())
    if not manager.has_agents():
        console.print(
            "[muted]No remote A2A agents configured. "
            "Add agents to ~/.minion/a2a.json or .minion/a2a.json[/]"
        )
        return
    summary = manager.agent_summary()
    console.print(f"[bold {YELLOW}]Remote A2A agents[/] [muted]({len(summary)} configured):[/]")
    for entry in summary:
        console.print(
            f"  [bold {YELLOW}]{entry['name']:<16}[/] {entry['url']}"
        )
        if entry["card_description"]:
            console.print(f"  {'':16}  [muted]{entry['card_description']}[/]")


# ─── `minion doctor` subcommand ───────────────────────────────────────────────

@app.command("doctor")
def doctor() -> None:
    """Check API keys, memory dir, MCP servers, and A2A agents for problems."""
    import os
    ok_mark = f"[bold green]✓[/]"
    fail_mark = f"[bold red]✗[/]"
    warn_mark = f"[bold yellow]![/]"

    console.print(f"\n[bold {YELLOW}]minion doctor[/]\n")

    # ── API key ────────────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or \
              os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        console.print(f"  {ok_mark}  API key found")
    else:
        console.print(
            f"  {fail_mark}  No API key found. "
            "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY."
        )

    # ── Memory directory ───────────────────────────────────────────────────────
    memory_dir = Path.home() / ".minion" / "memory"
    if memory_dir.exists():
        console.print(f"  {ok_mark}  Memory dir exists: {memory_dir}")
    else:
        console.print(f"  {warn_mark}  Memory dir not created yet: {memory_dir} (will be created on first run)")

    # ── config.toml ────────────────────────────────────────────────────────────
    config_path = Path.home() / ".minion" / "config.toml"
    if config_path.exists():
        console.print(f"  {ok_mark}  config.toml found: {config_path}")
    else:
        console.print(f"  {warn_mark}  No config.toml (using defaults). Create at: {config_path}")

    # ── MCP servers ────────────────────────────────────────────────────────────
    from .mcp import load_mcp_manager
    mcp_manager = load_mcp_manager(Path.cwd())
    if mcp_manager.has_tools():
        defs = mcp_manager.get_tool_definitions()
        console.print(f"  {ok_mark}  MCP: {len(defs)} tool(s) available")
    else:
        console.print(f"  {warn_mark}  MCP: no servers configured (optional)")
    mcp_manager.shutdown()

    # ── A2A agents ─────────────────────────────────────────────────────────────
    from .a2a import load_a2a_manager
    a2a_manager = load_a2a_manager(Path.cwd())
    if a2a_manager.has_agents():
        names = ", ".join(a2a_manager.agent_names())
        reachable = 0
        for name in a2a_manager.agent_names():
            card = a2a_manager._clients[name].fetch_agent_card()
            if card is not None:
                reachable += 1
        console.print(
            f"  {ok_mark if reachable == len(a2a_manager.agent_names()) else warn_mark}"
            f"  A2A: {reachable}/{len(a2a_manager.agent_names())} agent(s) reachable ({names})"
        )
    else:
        console.print(f"  {warn_mark}  A2A: no remote agents configured (optional)")

    console.print()
