"""MCPClientBase — transport-agnostic MCP protocol logic.

Contains everything that is identical between the stdio and Streamable HTTP
transports:
  - Dataclasses (MCPTool, MCPResource, MCPPromptArg, MCPPrompt)
  - MCP protocol handshake sequence (initialize → tools/list → prompts/list)
  - All public tool/resource/prompt methods
  - Notification dispatch (_handle_notification)
  - Schema parsing helpers (_parse_tool, _parse_resource, _parse_prompt)

Concrete transports (MCPClient for stdio, MCPHTTPClient for Streamable HTTP)
inherit from this class and implement five abstract methods:

    _initialize_transport()  — start transport (spawn process / validate URL)
    _after_handshake()       — post-handshake hook (HTTP: open GET stream)
    _send_request()          — send a JSON-RPC request, return response dict
    _send_notification()     — fire-and-forget JSON-RPC notification
    shutdown()               — tear down transport resources
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import MCPServerConfig

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "minion-cli", "version": "0.1.0"}


# ── Domain dataclasses ────────────────────────────────────────────────────────

@dataclass
class MCPTool:
    """A tool discovered from an MCP server."""
    name: str
    description: str
    input_schema: dict      # Anthropic snake_case format
    server_name: str
    destructive: bool = False  # from annotations.destructiveHint

    @property
    def namespaced_name(self) -> str:
        return f"{self.server_name}__{self.name}"

    def to_anthropic_schema(self) -> dict:
        return {
            "name": self.namespaced_name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class MCPResource:
    """A URI-addressable resource discovered from an MCP server."""
    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"
    server_name: str = ""


@dataclass
class MCPPromptArg:
    """One argument definition for an MCP prompt template."""
    name: str
    description: str = ""
    required: bool = False


@dataclass
class MCPPrompt:
    """A prompt template discovered from an MCP server."""
    name: str
    description: str = ""
    arguments: list[MCPPromptArg] = field(default_factory=list)
    server_name: str = ""

    @property
    def namespaced_name(self) -> str:
        return f"{self.server_name}__{self.name}"


# ── Abstract base ─────────────────────────────────────────────────────────────

class MCPClientBase(ABC):
    """Transport-agnostic MCP client.

    Handles the MCP protocol (JSON-RPC handshake, capability negotiation,
    tool/resource/prompt discovery) independently of how bytes move between
    client and server. Subclasses supply the transport by implementing the
    five abstract methods below.
    """

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        self.name = name
        self.config = config
        self._next_id: int = 1

        # Populated by connect()
        self.tools: list[MCPTool] = []
        self.prompts: list[MCPPrompt] = []
        self._capabilities: dict = {}
        self._has_resources_capability: bool = False

        # Optional: called from background thread on notifications/message
        self.notification_callback: Optional[Callable[[str, dict], None]] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Run the full MCP handshake and discover tools/prompts.

        Calls _initialize_transport() first (transport setup), then the
        JSON-RPC handshake sequence, then _after_handshake() (HTTP: open GET
        stream). Raises RuntimeError with a human-readable message on failure.
        """
        self._initialize_transport()

        resp = self._send_request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"logging": {}},
                "clientInfo": _CLIENT_INFO,
            },
        )
        init_result = resp.get("result", {})
        if "serverInfo" not in init_result:
            raise RuntimeError("server did not return serverInfo in initialize response")
        self._capabilities = init_result.get("capabilities", {})
        self._has_resources_capability = "resources" in self._capabilities

        self._send_notification("notifications/initialized")

        tools_resp = self._send_request("tools/list")
        raw_tools = tools_resp.get("result", {}).get("tools", [])
        self.tools = [self._parse_tool(t) for t in raw_tools if isinstance(t, dict)]

        if "prompts" in self._capabilities:
            prompts_resp = self._send_request("prompts/list")
            raw_prompts = prompts_resp.get("result", {}).get("prompts", [])
            self.prompts = [self._parse_prompt(p) for p in raw_prompts if isinstance(p, dict)]

        self._after_handshake()

    # ── Public interface ──────────────────────────────────────────────────────

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool by its raw (un-prefixed) name. Returns result as string."""
        try:
            resp = self._send_request(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
        except (IOError, RuntimeError) as e:
            return f"Error: MCP server '{self.name}' is not responding: {e}"

        result = resp.get("result", {})
        parts = [
            str(item.get("text", ""))
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "\n".join(parts) if parts else "(empty response)"
        return f"Error: {text}" if result.get("isError") else text

    def get_tool_definitions(self) -> list[dict]:
        """Return Anthropic-schema tool dicts with namespaced names."""
        return [t.to_anthropic_schema() for t in self.tools]

    def list_resources(self) -> list[MCPResource]:
        """Fetch the current resource list live from the server."""
        if not self._has_resources_capability:
            return []
        try:
            resp = self._send_request("resources/list")
            raw = resp.get("result", {}).get("resources", [])
            return [self._parse_resource(r) for r in raw if isinstance(r, dict)]
        except (IOError, RuntimeError):
            return []

    def read_resource(self, uri: str) -> str:
        """Read a resource by URI and return its content as a string."""
        try:
            resp = self._send_request("resources/read", {"uri": uri})
        except (IOError, RuntimeError) as e:
            return f"Error reading resource '{uri}': {e}"

        parts: list[str] = []
        for item in resp.get("result", {}).get("contents", []):
            if not isinstance(item, dict):
                continue
            if "text" in item:
                parts.append(str(item["text"]))
            elif item.get("type") == "blob":
                parts.append(f"[binary content, mimeType: {item.get('mimeType', 'unknown')}]")
        return "\n".join(parts) if parts else "(empty resource)"

    def get_prompt(self, prompt_name: str, arguments: dict | None = None) -> list[dict]:
        """Get a prompt template by name, optionally with arguments."""
        params: dict = {"name": prompt_name}
        if arguments:
            params["arguments"] = arguments
        try:
            resp = self._send_request("prompts/get", params)
        except (IOError, RuntimeError) as e:
            return [{"role": "user", "content": {"type": "text",
                     "text": f"Error getting prompt '{prompt_name}': {e}"}}]
        return resp.get("result", {}).get("messages", [])

    def set_log_level(self, level: str) -> None:
        """Ask the server to filter log notifications at the given level (best-effort)."""
        try:
            self._send_request("logging/setLevel", {"level": level})
        except (IOError, RuntimeError):
            pass

    def is_dangerous(self, namespaced_name: str) -> bool:
        """True if this tool requires user confirmation before execution."""
        if self.config.confirm_all:
            return True
        tool_name = namespaced_name.split("__", 1)[-1]
        return any(t.name == tool_name and t.destructive for t in self.tools)

    # ── Notification dispatch ─────────────────────────────────────────────────

    def _handle_notification(self, msg: dict) -> None:
        """Dispatch a server-sent notification to the registered callback.

        Any JSON-RPC message without an "id" field (i.e. any notification) is
        forwarded to the callback. The callback receives the server name and the
        notification's "params" dict. Standard MCP notifications include:

          notifications/message              — server log entry (level, logger, data)
          notifications/tools/list_changed   — server tool list has changed
          notifications/resources/updated    — a resource has changed

        Servers may also emit custom notification methods (e.g. workspace file
        change events). The callback is responsible for filtering by method if needed.
        """
        method = msg.get("method")
        if method is not None and self.notification_callback is not None:
            self.notification_callback(self.name, msg.get("params", {}) or {})

    # ── Parsing helpers ───────────────────────────────────────────────────────

    def _parse_tool(self, raw: dict) -> MCPTool:
        annotations = raw.get("annotations", {}) or {}
        return MCPTool(
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            input_schema=raw.get("inputSchema", {"type": "object", "properties": {}}),
            server_name=self.name,
            destructive=bool(annotations.get("destructiveHint", False)),
        )

    def _parse_resource(self, raw: dict) -> MCPResource:
        return MCPResource(
            uri=raw.get("uri", ""),
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            mime_type=raw.get("mimeType", "text/plain"),
            server_name=self.name,
        )

    def _parse_prompt(self, raw: dict) -> MCPPrompt:
        args = [
            MCPPromptArg(
                name=a.get("name", ""),
                description=a.get("description", ""),
                required=bool(a.get("required", False)),
            )
            for a in raw.get("arguments", [])
            if isinstance(a, dict)
        ]
        return MCPPrompt(
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            arguments=args,
            server_name=self.name,
        )

    # ── Abstract transport interface ──────────────────────────────────────────

    @abstractmethod
    def _initialize_transport(self) -> None:
        """Set up transport resources before the handshake.

        Stdio: spawn subprocess + start reader thread.
        HTTP:  validate and parse the URL (no network activity yet).
        Raises RuntimeError on failure.
        """

    @abstractmethod
    def _after_handshake(self) -> None:
        """Hook called after the MCP handshake completes.

        Stdio: no-op (reader thread already running).
        HTTP:  open the persistent GET stream for server-initiated notifications.
        """

    @abstractmethod
    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and return the parsed response dict.

        Raises IOError if the transport is closed, RuntimeError on server error.
        """

    @abstractmethod
    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""

    @abstractmethod
    def shutdown(self) -> None:
        """Tear down transport resources. Must never raise."""
