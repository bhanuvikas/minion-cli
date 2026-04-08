"""MCPManager — manages multiple MCPClient connections.

Responsible for:
- Connecting to all configured servers at REPL startup (graceful degradation)
- Exposing a merged, namespaced tool definition list to the agent loop
- Routing namespaced tool calls (server__tool) to the right server
- Emitting Nefario trace events for MCP lifecycle operations
- Coordinating shutdown of all server processes
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..theme import console
from ..tracing import get_tracer
from .client import MCPClient
from .config import MCPServerConfig, load_mcp_config

if TYPE_CHECKING:
    pass


class MCPManager:
    """Manages multiple MCP server connections for the duration of a session."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}  # keyed by server name

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect_all(self, configs: dict[str, MCPServerConfig]) -> None:
        """Connect to all configured servers. Warns and skips failed servers.

        Pattern mirrors load_skill_registry's graceful degradation: a bad server
        config never prevents minion from starting.
        """
        for name, config in configs.items():
            client = MCPClient(name, config)
            t0 = time.monotonic()
            try:
                client.connect()
                latency_ms = int((time.monotonic() - t0) * 1000)
                self._clients[name] = client
                get_tracer().emit(
                    "mcp_server_connect",
                    server_name=name,
                    command=config.command,
                    tool_count=len(client.tools),
                    success=True,
                    latency_ms=latency_ms,
                )
            except (RuntimeError, OSError) as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                console.print(
                    f"[muted]Warning: MCP server '{name}' failed to connect: {e}[/]"
                )
                get_tracer().emit(
                    "mcp_server_connect",
                    server_name=name,
                    command=config.command,
                    tool_count=0,
                    success=False,
                    error=str(e),
                    latency_ms=latency_ms,
                )
                get_tracer().emit(
                    "mcp_error",
                    server_name=name,
                    tool_name="",
                    error=str(e),
                    context="connect",
                )

    def shutdown(self) -> None:
        """Terminate all server subprocesses. Errors are silently ignored."""
        for client in self._clients.values():
            client.shutdown()
        self._clients.clear()

    # ── Tool access ───────────────────────────────────────────────────────────

    def has_tools(self) -> bool:
        """True if at least one connected server has at least one tool."""
        return any(client.tools for client in self._clients.values())

    def get_tool_definitions(self) -> list[dict]:
        """Return merged list of all tool definitions from all connected clients.

        Each dict is in Anthropic API format with a namespaced name:
            {"name": "notes__create_note", "description": ..., "input_schema": ...}
        """
        defs: list[dict] = []
        for client in self._clients.values():
            defs.extend(client.get_tool_definitions())
        return defs

    def is_dangerous(self, namespaced_name: str) -> bool:
        """True if this tool requires user confirmation before execution.

        A tool is dangerous if:
        - The server was configured with confirm_all=True, OR
        - The tool's MCP annotation has destructiveHint=True.
        Returns False (safe) for unknown tools or servers not connected.
        """
        server_name = namespaced_name.split("__", 1)[0]
        client = self._clients.get(server_name)
        if client is None:
            return False
        return client.is_dangerous(namespaced_name)

    def call_tool(self, namespaced_name: str, arguments: dict) -> str:
        """Route a namespaced tool call to the correct server.

        Splits 'server__tool' on '__' (maxsplit=1), locates the client,
        and delegates execution. Emits mcp_tool_call and mcp_tool_result
        Nefario events around the call.

        Returns an error string if the server is not connected or the call fails.
        """
        parts = namespaced_name.split("__", 1)
        if len(parts) != 2:
            return f"Error: malformed MCP tool name '{namespaced_name}' (expected 'server__tool')"

        server_name, tool_name = parts
        client = self._clients.get(server_name)
        if client is None:
            return f"Error: MCP server '{server_name}' is not connected"

        get_tracer().emit(
            "mcp_tool_call",
            server_name=server_name,
            tool_name=tool_name,
            namespaced_name=namespaced_name,
            inputs=arguments,
        )

        t0 = time.monotonic()
        result = client.call_tool(tool_name, arguments)
        latency_ms = int((time.monotonic() - t0) * 1000)
        success = not result.startswith("Error:")

        get_tracer().emit(
            "mcp_tool_result",
            server_name=server_name,
            tool_name=tool_name,
            output=result,
            success=success,
            latency_ms=latency_ms,
        )

        if not success:
            get_tracer().emit(
                "mcp_error",
                server_name=server_name,
                tool_name=tool_name,
                error=result,
                context="call",
            )

        return result

    # ── Display helpers ───────────────────────────────────────────────────────

    def server_summary(self) -> list[tuple[str, list[str]]]:
        """Return list of (server_name, [tool_name, ...]) for display."""
        return [
            (name, [t.name for t in client.tools])
            for name, client in self._clients.items()
        ]


def load_mcp_manager(cwd: Path | None = None) -> "MCPManager":
    """Load MCP config and connect to all configured servers.

    Always returns an MCPManager — if no servers are configured or all fail,
    the manager is empty (has_tools() returns False, get_tool_definitions() returns []).
    Callers never need to null-check the return value.
    """
    configs = load_mcp_config(cwd)
    manager = MCPManager()
    if configs:
        manager.connect_all(configs)
    return manager
