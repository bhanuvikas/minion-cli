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

load_dotenv(Path.home() / ".minion" / ".env")                 # user-level defaults
load_dotenv(Path.cwd() / ".minion" / ".env", override=True)  # project-level overrides

from .config import load_config as _load_config  # noqa: E402 — after dotenv


app = typer.Typer(
    name="minion",
    help="🍌 Minion — your agentic coding assistant.",
    add_completion=True,
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
)

# Subcommand names that typer registers — used by _entry() to distinguish
# "minion doctor" (subcommand) from "minion 'fix the bug'" (one-shot prompt).
_KNOWN_SUBCOMMANDS = frozenset({"doctor", "skills", "mcp", "agents", "remote", "setup", "config", "model", "memory"})
# Options that consume the next token as their value.
_OPTS_WITH_VALUE = frozenset({
    "-p", "--provider",
    "-m", "--model",
    "--reflect",
    "--install-completion",   # consume optional shell-type arg so _entry doesn't treat it as a prompt
    "--show-completion",
})


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
    Pass a prompt to run a single task and exit: [bold]minion "what is 2+2?"[/bold]
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


# ─── `minion agents` subcommand ───────────────────────────────────────────────

_agent_app = typer.Typer(name="agents", help="Manage and run agent roles.", add_completion=False)
app.add_typer(_agent_app, name="agents")


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


# ─── `minion remote` subcommand ───────────────────────────────────────────────

_a2a_app = typer.Typer(name="remote", help="Remote agents — list configured agents or serve minion as one.", add_completion=False)
app.add_typer(_a2a_app, name="remote")


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
    from .llm.conversation import Conversation
    from .context.prompts import build_system_prompt
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


# ─── `minion config` subcommand ───────────────────────────────────────────────

@app.command("config")
def config_cmd() -> None:
    """Show effective configuration (config.toml + env + CLI flags)."""
    from .config import format_config, load_config
    cfg = load_config(cwd=Path.cwd())
    console.print(f"\n[bold {YELLOW}]Effective configuration[/] [muted](config.toml + env):[/]\n")
    console.print(format_config(cfg))
    console.print()


# ─── `minion model` subcommand ────────────────────────────────────────────────

@app.command("model")
def model_cmd(
    provider: Optional[str] = typer.Option(None, "--provider", "-p"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
) -> None:
    """Interactively configure provider, model ID, and API keys."""
    from .config import run_model_config
    try:
        client = get_client(provider, model)
    except ValueError as e:
        print_error(str(e))
        raise typer.Exit(code=1)
    run_model_config(client)


# ─── `minion memory` subcommand ───────────────────────────────────────────────

_memory_app = typer.Typer(name="memory", help="Browse and manage persistent memory.", add_completion=False)
app.add_typer(_memory_app, name="memory")


@_memory_app.callback(invoke_without_command=True)
def _memory_main(ctx: typer.Context) -> None:
    """List all memories. Run without subcommand to show all stored memories."""
    if ctx.invoked_subcommand is None:
        _memory_recall(query=None)


@_memory_app.command("recall")
def memory_recall(
    query: Optional[str] = typer.Argument(None, help="Optional search query"),
) -> None:
    """Show stored memories, optionally filtered by a search query."""
    _memory_recall(query)


@_memory_app.command("add")
def memory_add(
    text: str = typer.Argument(..., help="Text to remember"),
    global_scope: bool = typer.Option(False, "--global", "-g", help="Store globally (not project-scoped)"),
    category: str = typer.Option("project", "--category", "-c",
                                  help="Category: identity, preference, project, event"),
) -> None:
    """Store a new memory."""
    import uuid
    from datetime import datetime, timezone
    from .memory.config import MemoryConfig
    from .memory.record import MemoryRecord
    from .memory.store import MemoryStore

    if category not in ("identity", "preference", "project", "event"):
        print_error("--category must be one of: identity, preference, project, event")
        raise typer.Exit(code=1)

    scope = "global" if global_scope else "project"
    project_path: Optional[str] = None if global_scope else str(Path.cwd())
    store = MemoryStore(config=MemoryConfig(), project_cwd=Path.cwd())
    record = MemoryRecord(
        id=str(uuid.uuid4()),
        content=text.strip("\"'"),
        type="semantic",
        scope=scope,
        project_path=project_path,
        tags=[],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        superseded_by=None,
        category=category,
    )
    store.store(record)
    console.print(f"[bold {YELLOW}]Remembered[/] [muted]({scope}·{category}):[/] {text}")


@_memory_app.command("forget")
def memory_forget(
    query: str = typer.Argument(..., help="Memory ID (first 8 chars) or substring of the content"),
) -> None:
    """Delete memories matching an ID or content substring."""
    from .memory.config import MemoryConfig
    from .memory.store import MemoryStore

    store = MemoryStore(config=MemoryConfig(), project_cwd=Path.cwd())
    count = store.delete(query.strip("\"'"))
    if count:
        console.print(f"[bold {YELLOW}]Forgotten.[/] [muted]({count} memory removed)[/]")
    else:
        console.print("[muted]No matching memory found.[/]")


def _memory_recall(query: Optional[str]) -> None:
    from .memory.config import MemoryConfig
    from .memory.store import MemoryStore

    store = MemoryStore(config=MemoryConfig(), project_cwd=Path.cwd())
    memories = store.list_all(query=query or None)
    if not memories:
        console.print("[muted]No memories stored yet.[/]")
        return
    for m in memories:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(m.created_at)
            delta = datetime.now(timezone.utc) - dt
            days = delta.days
            age = f"{days}d ago" if days > 0 else "today"
        except Exception:
            age = m.created_at
        console.print(
            f"  [{YELLOW}]{m.id[:8]}[/] [{m.category}·{m.scope}] "
            f"{m.content} [muted]({age})[/]"
        )


# ─── First-run detection ──────────────────────────────────────────────────────

def _needs_setup() -> bool:
    """Return True if no API key is configured anywhere — first-run indicator."""
    import os
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(key):
            return False
    return True


# ─── `minion setup` subcommand ────────────────────────────────────────────────

@app.command("setup")
def setup_cmd() -> None:
    """Interactive first-run setup — configure your API key and provider."""
    from .config import run_setup_wizard
    asyncio.run(run_setup_wizard())


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


# ─── One-shot entry point ─────────────────────────────────────────────────────

def _run_one_shot(prompt: str, raw_argv: list) -> None:
    """Run a single prompt and exit. Called by _entry() before typer routing."""
    import sys as _sys

    cfg = _load_config()

    # Parse the flags we care about from raw_argv (everything before the prompt)
    provider = model = None
    reflect = cfg.agent.reflect_depth
    dry_run = False
    verbose = cfg.agent.verbose

    i = 0
    while i < len(raw_argv):
        tok = raw_argv[i]
        if tok in ("-p", "--provider") and i + 1 < len(raw_argv):
            provider = raw_argv[i + 1]; i += 2
        elif tok in ("-m", "--model") and i + 1 < len(raw_argv):
            model = raw_argv[i + 1]; i += 2
        elif tok == "--reflect" and i + 1 < len(raw_argv):
            try:
                reflect = int(raw_argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif tok == "--dry-run":
            dry_run = True; i += 1
        elif tok in ("-v", "--verbose"):
            verbose = True; i += 1
        else:
            i += 1

    effective_provider = provider or cfg.llm.provider
    effective_model = model or cfg.llm.model

    try:
        client = get_client(effective_provider, effective_model)
    except ValueError as e:
        print_error(str(e))
        _sys.exit(1)

    from .context import build_project_context
    from .context.prompts import build_system_prompt
    from .runner import run_prompt_async
    from .llm.reflection import ReflectionConfig
    from .llm.conversation import Conversation
    from .tools.permissions import PermissionStore

    import os as _os
    project_cwd = Path.cwd()
    system_prompt = build_system_prompt(build_project_context(project_cwd))
    conversation = Conversation()
    reflect_cfg = ReflectionConfig(depth=reflect) if reflect else None
    permission_store = PermissionStore(project_cwd=project_cwd)
    stream_markdown = _os.getenv("MINION_MARKDOWN", "true").lower() != "false"

    asyncio.run(run_prompt_async(
        prompt, client, conversation, system_prompt,
        dry_run=dry_run, reflect_config=reflect_cfg,
        verbose=verbose, permission_store=permission_store,
        stream_markdown=stream_markdown,
    ))


def _entry() -> None:
    """Smart entry point: intercepts one-shot prompts before typer's subcommand routing.

    typer/click groups always try to resolve the first positional arg as a subcommand,
    so `minion "fix the bug"` would fail with "No such command 'fix the bug'".
    This wrapper scans sys.argv first: if the first non-option positional arg is not
    a known subcommand, it extracts it as a one-shot prompt and bypasses typer entirely.
    """
    import sys

    raw = sys.argv[1:]
    prefix: list = []   # option tokens before the prompt
    i = 0

    while i < len(raw):
        tok = raw[i]
        if tok in _OPTS_WITH_VALUE and i + 1 < len(raw):
            prefix += [tok, raw[i + 1]]
            i += 2
        elif tok.startswith("-"):
            prefix.append(tok)
            i += 1
        elif tok in _KNOWN_SUBCOMMANDS:
            app()   # known subcommand → let typer handle
            return
        else:
            # First positional arg that isn't a subcommand → one-shot prompt
            prompt = " ".join(raw[i:])
            if _needs_setup():
                from .config import run_setup_wizard
                asyncio.run(run_setup_wizard())
            _run_one_shot(prompt, prefix)
            return

    # No positional args at all → REPL / --help / --version via typer
    if _needs_setup() and not any(a.startswith("-") for a in raw):
        # Only trigger wizard on bare `minion` (no flags) so --help etc. still work
        from .config import run_setup_wizard
        asyncio.run(run_setup_wizard())
    app()
