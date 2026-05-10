"""MCP slash command handlers: /mcp resource, /mcp prompt, /mcp reload, /mcp list."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from ..theme import BLUE, YELLOW, console

if TYPE_CHECKING:
    from ..llm.conversation import Conversation
    from ..mcp.manager import MCPManager


def _extract_mcp_text(msg: dict) -> str:
    """Extract plain text from an MCP message dict."""
    content = msg.get("content", {})
    if isinstance(content, dict) and content.get("type") == "text":
        return content.get("text", "")
    if isinstance(content, str):
        return content
    return ""


def _inject_mcp_message(msg: dict, conversation: "Conversation") -> None:
    """Inject one MCP message into the conversation at the correct role."""
    text = _extract_mcp_text(msg)
    role = msg.get("role", "user")
    if role == "user":
        conversation.add_user(text)
    elif role == "assistant":
        conversation.add_assistant(text, usage=None)


async def _handle_mcp_command(raw: str, mcp_manager: "MCPManager") -> Optional[list[dict]]:
    """Handle the /mcp slash command family.

    Subcommands:
        /mcp [list|status]           — list servers, tools, resources, prompt templates
        /mcp resource <uri>          — read and display a resource by URI
        /mcp prompt <name> [k=v ...] — get a prompt template and inject it
        /mcp reload                  — reconnect to all MCP servers

    Returns None normally. Returns the raw MCP messages list when /mcp prompt
    succeeds — the REPL loop injects prefix messages into conversation history
    and uses the last user-role message as the run_prompt() input.
    """
    parts = raw.split(maxsplit=2)
    sub = parts[1].strip() if len(parts) > 1 else ""

    # ── /mcp resource <uri> ──────────────────────────────────────────────────
    if sub == "resource":
        if len(parts) < 3:
            console.print("[muted]Usage: /mcp resource <uri>  (e.g. /mcp resource notes://ideas)[/]")
            return None
        uri = parts[2].strip()
        content = await mcp_manager.read_resource(uri)
        console.print(f"[bold {YELLOW}]Resource:[/] [bold]{uri}[/]")
        console.print(content)
        return None

    # ── /mcp prompt <name> [key=value ...] ──────────────────────────────────
    if sub == "prompt":
        if len(parts) < 3:
            console.print(
                "[muted]Usage: /mcp prompt <server__name> [key=value ...]\n"
                "       e.g.  /mcp prompt notes__summarize_notes\n"
                "             /mcp prompt notes__find_related topic=AI\n"
                "             /mcp prompt notes__draft_note title=arch context=microkernel design[/]"
            )
            return None
        tokens = parts[2].strip().split()
        namespaced_name = tokens[0]

        # Smarter arg parsing: words after a key= are accumulated as the value
        # until the next key= token, letting multi-word values work without quotes.
        arguments: dict = {}
        current_key: Optional[str] = None
        current_val_parts: list[str] = []
        for token in tokens[1:]:
            if "=" in token:
                if current_key is not None:
                    arguments[current_key] = " ".join(current_val_parts)
                k, _, v = token.partition("=")
                current_key = k.strip()
                current_val_parts = [v] if v else []
            elif current_key is not None:
                current_val_parts.append(token)
        if current_key is not None:
            arguments[current_key] = " ".join(current_val_parts)

        # Collect missing arguments interactively before calling the server.
        prompt_info = mcp_manager.get_prompt_info(namespaced_name)
        if prompt_info is not None:
            import questionary
            from ..config import MINION_STYLE
            for arg in prompt_info.arguments:
                if arg.name in arguments:
                    continue
                desc = f" ({arg.description})" if arg.description else ""
                label = f"  {arg.name}{desc}"
                if arg.required:
                    label += " [required]"
                else:
                    label += " [optional, Enter to skip]"
                value = questionary.text(f"{label}:", style=MINION_STYLE).ask()
                if value is None:
                    console.print("[muted]Cancelled.[/]")
                    return None
                value = value.strip()
                if not value:
                    if arg.required:
                        console.print(f"[red]Required argument '{arg.name}' cannot be empty.[/]")
                        return None
                else:
                    arguments[arg.name] = value

        messages = await mcp_manager.get_prompt(namespaced_name, arguments or None)
        if not messages:
            console.print(f"[muted]Prompt '{namespaced_name}' returned no messages.[/]")
            return None

        if len(messages) == 1 and _extract_mcp_text(messages[0]).startswith("Error"):
            console.print(f"[red]Prompt error:[/] {_extract_mcp_text(messages[0])}")
            return None

        n = len(messages)
        console.print(
            f"[muted]Injecting {n} message{'s' if n > 1 else ''} "
            f"from '{namespaced_name}'…[/]"
        )
        return messages

    # ── /mcp reload ──────────────────────────────────────────────────────────
    if sub == "reload":
        from pathlib import Path as _Path
        console.print(f"[muted]Reloading MCP servers…[/]")
        await mcp_manager.reconnect_all(cwd=_Path.cwd())
        for _warn in mcp_manager.connection_warnings:
            console.print(_warn)
        n = len(list(mcp_manager._states))
        console.print(f"[{YELLOW}]MCP reloaded: {n} server{'s' if n != 1 else ''} connected.[/]")
        return None

    # ── /mcp [list|status] ───────────────────────────────────────────────────
    if sub not in ("", "list", "status"):
        console.print(
            "[muted]Usage:\n"
            "  /mcp [list|status]              — list servers and capabilities\n"
            "  /mcp resource <uri>             — read a resource\n"
            "  /mcp prompt <name> [key=value]  — inject a prompt template\n"
            "  /mcp reload                     — reconnect to all MCP servers[/]"
        )
        return None

    summary = await mcp_manager.server_summary_async()
    if not summary:
        console.print(
            "[muted]No MCP servers connected. "
            "Add servers to ~/.minion/mcp.json or .minion/mcp.json[/]"
        )
        return None

    total_tools     = sum(len(s["tools"])     for s in summary)
    total_resources = sum(len(s["resources"]) for s in summary)
    total_prompts   = sum(len(s["prompts"])   for s in summary)
    console.print(
        f"[bold {YELLOW}]MCP servers[/] [muted]("
        f"{total_tools} tools, {total_resources} resources, {total_prompts} prompts):[/]"
    )
    for s in summary:
        console.print(
            f"  [bold {BLUE}]{s['name']}[/]  "
            f"[muted]{len(s['tools'])}t · {len(s['resources'])}r · {len(s['prompts'])}p[/]"
        )
        for t in s["tools"]:
            console.print(f"    [muted]tool[/]   {t}")
        for r in s["resources"]:
            label = f" — {r['description']}" if r.get("description") else ""
            console.print(f"    [muted]resource[/] {r['uri']}{label}")
        for p in s["prompts"]:
            args_str = ""
            if p.get("arguments"):
                args_str = "  [muted](" + ", ".join(
                    a["name"] + ("*" if a.get("required") else "") for a in p["arguments"]
                ) + ")[/]"
            console.print(f"    [muted]prompt[/]  {s['name']}__{p['name']}{args_str}")

    return None
