"""minion doctor — health-check API keys, memory, MCP, and A2A."""

import os
from pathlib import Path

import typer

from ..theme import YELLOW, console

_OK   = "[bold green]✓[/]"
_FAIL = "[bold red]✗[/]"
_WARN = "[bold yellow]![/]"


def doctor_cmd() -> None:
    """Check API keys, memory dir, MCP servers, and A2A agents for problems."""
    console.print(f"\n[bold {YELLOW}]minion doctor[/]\n")
    _check_api_key()
    _check_memory_dir()
    _check_config_toml()
    _check_mcp()
    _check_a2a()
    console.print()


def _check_api_key() -> None:
    key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
    )
    if key:
        console.print(f"  {_OK}  API key found")
    else:
        console.print(
            f"  {_FAIL}  No API key found. "
            "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or OPENROUTER_API_KEY."
        )


def _check_memory_dir() -> None:
    memory_dir = Path.home() / ".minion" / "memory"
    if memory_dir.exists():
        console.print(f"  {_OK}  Memory dir exists: {memory_dir}")
    else:
        console.print(f"  {_WARN}  Memory dir not created yet: {memory_dir} (will be created on first run)")


def _check_config_toml() -> None:
    config_path = Path.home() / ".minion" / "config.toml"
    if config_path.exists():
        console.print(f"  {_OK}  config.toml found: {config_path}")
    else:
        console.print(f"  {_WARN}  No config.toml (using defaults). Create at: {config_path}")


def _check_mcp() -> None:
    from ..mcp import load_mcp_manager
    manager = load_mcp_manager(Path.cwd())
    if manager.has_tools():
        defs = manager.get_tool_definitions()
        console.print(f"  {_OK}  MCP: {len(defs)} tool(s) available")
    else:
        console.print(f"  {_WARN}  MCP: no servers configured (optional)")
    manager.shutdown()


def _check_a2a() -> None:
    from ..a2a import load_a2a_manager
    manager = load_a2a_manager(Path.cwd())
    if not manager.has_agents():
        console.print(f"  {_WARN}  A2A: no remote agents configured (optional)")
        return
    names = manager.agent_names()
    reachable = sum(
        1 for name in names
        if manager._clients[name].fetch_agent_card() is not None
    )
    mark = _OK if reachable == len(names) else _WARN
    console.print(
        f"  {mark}  A2A: {reachable}/{len(names)} agent(s) reachable ({', '.join(names)})"
    )
