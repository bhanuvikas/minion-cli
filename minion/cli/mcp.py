"""minion mcp — list MCP server connections."""

from pathlib import Path

import typer

from ..theme import YELLOW, console

app = typer.Typer(name="mcp", help="Manage MCP server connections.", add_completion=False)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """List MCP-connected servers and tools. Run without subcommand to list all."""
    if ctx.invoked_subcommand is None:
        _list_mcp()


@app.command("list")
def mcp_list() -> None:
    """List all MCP servers and their tools (from ~/.minion/mcp.json and .minion/mcp.json)."""
    _list_mcp()


def _list_mcp() -> None:
    from ..mcp import load_mcp_manager
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
