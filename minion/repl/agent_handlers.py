"""Direct /agent and /remote slash command handlers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..theme import BLUE, YELLOW, console, print_error

if TYPE_CHECKING:
    from ..a2a.manager import A2AManager
    from ..agents.manifest import AgentRoleManifest
    from ..llm.conversation import Conversation


def _handle_remote_command(raw: str, a2a_manager: "A2AManager | None") -> None:
    """Handle the /remote slash command family.

    Subcommands:
        /remote [list]             — list configured remote agents
        /remote run <agent> <task> — send a task to a named remote agent
    """
    parts = raw.strip().split(None, 3)
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub in ("", "list", "status"):
        if a2a_manager is None or not a2a_manager.has_agents():
            console.print(
                "[muted]No remote agents configured. "
                "Add agents to ~/.minion/a2a.json or .minion/a2a.json[/]"
            )
            return
        summary = a2a_manager.agent_summary()
        from rich.table import Table
        table = Table(show_header=True, header_style="bold", expand=False, box=None)
        table.add_column("agent", style=YELLOW)
        table.add_column("url")
        table.add_column("description", style="muted")
        for entry in summary:
            table.add_row(entry["name"], entry["url"], entry["card_description"])
        console.print(table)
        return

    if sub == "run":
        if len(parts) < 4:
            if len(parts) == 3:
                print_error(f"Usage: /remote run <agent> <task>  (missing task for agent '{parts[2]}')")
            else:
                print_error("Usage: /remote run <agent> <task>")
            return
        agent_name = parts[2]
        task = parts[3].strip()
        if not task:
            print_error("Task cannot be empty.")
            return
        if a2a_manager is None or not a2a_manager.has_agents():
            print_error("No remote agents configured.")
            return
        with console.status(f"[muted]  ⚙  [{agent_name}] running...[/]", spinner="dots"):
            result = a2a_manager.send_task(agent_name, task)
        console.print(result)
        return

    print_error(f"Unknown /remote subcommand '{sub}'. Usage: /remote [list | run <agent> <task>]")


async def _run_agent_handoff_summary(
    agent_conversation: "Conversation",
    agent_manifest: "AgentRoleManifest",
    client,
) -> str:
    """Ask the agent to summarize its conversation as a handoff for minion.

    The agent (not a neutral LLM) produces the summary because it has full
    context of what was discussed and what mattered.
    """
    from ..compact.summary import _conversation_to_text
    from ..llm.base import Message

    summary_request = (
        "Please provide a concise handoff summary of our conversation for the main minion assistant. "
        "Include: what the user asked you to do, key findings and outputs, any files created or "
        "modified, and clear recommendations for next steps. "
        "Write it as a briefing from you to minion."
    )
    messages = list(agent_conversation.messages) + [
        Message(role="user", content=summary_request)
    ]
    response = await asyncio.to_thread(
        client.complete,
        messages=messages,
        system=agent_manifest.system_prompt,
    )
    return response.content.strip()
