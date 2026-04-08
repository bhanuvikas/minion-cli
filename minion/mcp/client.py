"""MCPClient — single MCP server connection via stdio JSON-RPC 2.0.

Implements the MCP client side of the stdio transport. The server is a subprocess;
we write JSON-RPC requests to its stdin and read responses from its stdout.
Each message is a single newline-terminated JSON object.

MCP initialization sequence:
    Client → initialize (id=1)
    Server → result with serverInfo + capabilities
    Client → notifications/initialized  (no id, no response)
    Client → tools/list (id=2)
    Server → result with tool schemas

Tool call sequence:
    Client → tools/call (id=N, params={name, arguments})
    Server → result with content list and isError flag

Tool annotations (MCP 2025 revision):
    Each tool schema may include an "annotations" object with:
        readOnlyHint:   bool  — tool does not modify external state
        destructiveHint: bool — tool may perform destructive operations (delete, overwrite, etc.)
    We store destructive=True for tools where destructiveHint is true.
    Tools without annotations default to destructive=False (safe — no confirmation).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .config import MCPServerConfig

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "minion-cli", "version": "0.1.0"}


@dataclass
class MCPTool:
    """A tool discovered from an MCP server."""
    name: str               # raw tool name as reported by server
    description: str
    input_schema: dict      # already in Anthropic snake_case format
    server_name: str
    destructive: bool = False  # from annotations.destructiveHint

    @property
    def namespaced_name(self) -> str:
        return f"{self.server_name}__{self.name}"

    def to_anthropic_schema(self) -> dict:
        """Return the tool definition dict in the format expected by Claude API."""
        return {
            "name": self.namespaced_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class MCPClient:
    """Manages a single MCP server subprocess connection.

    Lifecycle:
        client = MCPClient("notes", config)
        client.connect()           # spawns process, handshake, fetches tools
        result = client.call_tool("create_note", {"title": "...", "content": "..."})
        client.shutdown()          # terminates subprocess
    """

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        self.name = name
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._next_id: int = 1
        self.tools: list[MCPTool] = []  # populated by connect()

    # ── Public interface ──────────────────────────────────────────────────────

    def connect(self) -> None:
        """Spawn server subprocess and complete the MCP initialize handshake.

        Raises RuntimeError with a human-readable message if any step fails.
        MCPManager.connect_all() catches this and warns instead of crashing.
        """
        env = {**os.environ, **self.config.env}
        try:
            self._process = subprocess.Popen(
                self.config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise RuntimeError(f"failed to start '{self.config.command[0]}': {e}") from e

        # Initialize handshake
        resp = self._send_request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        if "serverInfo" not in resp.get("result", {}):
            raise RuntimeError("server did not return serverInfo in initialize response")

        # Notify server that initialization is complete (no response expected)
        self._send_notification("notifications/initialized")

        # Discover tools
        tools_resp = self._send_request("tools/list")
        raw_tools = tools_resp.get("result", {}).get("tools", [])
        self.tools = [self._parse_tool(t) for t in raw_tools if isinstance(t, dict)]

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool by its raw (un-prefixed) server name.

        Returns the result as a plain string. MCP content lists are joined;
        only "text" content types are currently handled.
        If the server reports isError=True, the error text is returned (not raised).
        If the process is dead, returns an error string.
        """
        try:
            resp = self._send_request(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
        except IOError as e:
            return f"Error: MCP server '{self.name}' is not responding: {e}"
        except RuntimeError as e:
            return f"Error: {e}"

        result = resp.get("result", {})
        content_list = result.get("content", [])
        is_error = result.get("isError", False)

        # Extract text from content items
        parts = []
        for item in content_list:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append(json.dumps(item))

        text = "\n".join(parts) if parts else "(empty response)"
        if is_error:
            return f"Error: {text}"
        return text

    def get_tool_definitions(self) -> list[dict]:
        """Return Anthropic-schema tool dicts with namespaced names."""
        return [t.to_anthropic_schema() for t in self.tools]

    def is_dangerous(self, namespaced_name: str) -> bool:
        """True if the tool has destructiveHint=True or confirm_all is set on this server."""
        if self.config.confirm_all:
            return True
        tool_name = namespaced_name.split("__", 1)[-1]
        for tool in self.tools:
            if tool.name == tool_name:
                return tool.destructive
        return False

    def shutdown(self) -> None:
        """Terminate the server subprocess cleanly. Never raises."""
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.terminate()
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
        except Exception:
            pass
        finally:
            self._process = None

    # ── Private protocol helpers ──────────────────────────────────────────────

    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and return the parsed response dict."""
        req_id = self._next_id
        self._next_id += 1
        payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        return self._read_response(req_id)

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def _write(self, obj: dict) -> None:
        """Serialize and write one JSON-RPC message to the server's stdin."""
        if self._process is None or self._process.stdin is None:
            raise IOError("server process is not running")
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        self._process.stdin.write(line.encode())
        self._process.stdin.flush()

    def _read_response(self, expected_id: int) -> dict:
        """Read one line from stdout and return the parsed JSON object.

        Raises:
            IOError: if stdout is closed (server process died)
            RuntimeError: if the response id doesn't match or contains an error
        """
        if self._process is None or self._process.stdout is None:
            raise IOError("server process is not running")
        line = self._process.stdout.readline()
        if not line:
            raise IOError("server closed stdout unexpectedly")
        try:
            parsed = json.loads(line.decode())
        except json.JSONDecodeError as e:
            raise RuntimeError(f"invalid JSON from server: {e}") from e

        if parsed.get("id") != expected_id:
            raise RuntimeError(
                f"id mismatch: expected {expected_id}, got {parsed.get('id')}"
            )
        if "error" in parsed:
            err = parsed["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"server error: {msg}")
        return parsed

    def _parse_tool(self, raw: dict) -> MCPTool:
        """Parse a raw tool dict from tools/list into an MCPTool.

        MCP uses camelCase 'inputSchema'; we rename to snake_case 'input_schema'
        to match minion's native tool format. The schema contents are unchanged.
        Annotations are optional; missing = not destructive.
        """
        annotations = raw.get("annotations", {}) or {}
        destructive = bool(annotations.get("destructiveHint", False))
        return MCPTool(
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            input_schema=raw.get("inputSchema", {"type": "object", "properties": {}}),
            server_name=self.name,
            destructive=destructive,
        )
