"""minion remote — list configured remote agents or serve minion as one."""

from pathlib import Path
from typing import Optional

import typer

from ..theme import YELLOW, console, print_error

app = typer.Typer(
    name="remote",
    help="Remote agents — list configured agents or serve minion as one.",
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """List configured remote agents or start an A2A server."""
    if ctx.invoked_subcommand is None:
        _list_remote()


@app.command("list")
def remote_list() -> None:
    """List configured remote A2A agents and their capabilities."""
    _list_remote()


@app.command("serve")
def remote_serve(
    port: int = typer.Option(8080, "--port", "-p", help="Port to listen on."),
    host: str = typer.Option("localhost", "--host", help="Host to bind to."),
    provider: Optional[str] = typer.Option(None, "--provider", "-P"),
    model: Optional[str] = typer.Option(None, "--model", "-m"),
) -> None:
    """Start an A2A HTTP server — exposes minion as a remote A2A agent.

    Clients can submit tasks via POST /tasks/send or POST /tasks/sendSubscribe
    and discover capabilities at GET /.well-known/agent.json.
    """
    from ..a2a.server import A2AServer
    from ..context import build_project_context
    from ..context.prompts import build_system_prompt
    from ..llm.conversation import Conversation
    from ..llm.factory import get_client
    from ..runner import run_prompt

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
    console.print(f"[bold {YELLOW}]A2A server[/] listening at [bold]http://{host}:{port}[/]")
    console.print(f"[muted]  Agent Card: http://{host}:{port}/.well-known/agent.json[/]")
    console.print(f"[muted]  Ctrl+C to stop[/]\n")
    server.start()


def _list_remote() -> None:
    from ..a2a import load_a2a_manager
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
        console.print(f"  [bold {YELLOW}]{entry['name']:<16}[/] {entry['url']}")
        if entry["card_description"]:
            console.print(f"  {'':16}  [muted]{entry['card_description']}[/]")
