"""MCP server configuration — loads ~/.minion/mcp.json and .minion/mcp.json.

Two-tier loading (user → project). Project server names shadow user server names.
If neither file exists, returns an empty dict — MCP is simply disabled.

Config format — stdio server:
    {
      "servers": {
        "notes": {
          "command": ["python", "examples/mcp_notes_server.py"],
          "env": {},
          "confirm_all": false
        }
      }
    }

Config format — Streamable HTTP server:
    {
      "servers": {
        "workspace": {
          "url": "http://localhost:9000/mcp",
          "confirm_all": false
        }
      }
    }

Fields (all servers):
    confirm_all  — if true, every tool on this server requires user confirmation
                   regardless of its destructiveHint annotation (optional, default false)

Fields (stdio only):
    command      — subprocess argv list (required for stdio)
    env          — extra environment variables merged into os.environ (optional)

Fields (HTTP only):
    url          — full URL to the MCP endpoint, e.g. "http://localhost:9000/mcp"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..theme import console


@dataclass
class MCPServerConfig:
    name: str
    command: list[str] = field(default_factory=list)   # stdio transport: subprocess argv
    env: dict[str, str] = field(default_factory=dict)  # stdio transport: extra env vars
    url: str = ""                                       # HTTP transport: endpoint URL
    confirm_all: bool = False

    @property
    def transport(self) -> str:
        """Return 'http' if a URL is configured, otherwise 'stdio'."""
        return "http" if self.url else "stdio"


def load_mcp_config(cwd: Path | None = None) -> dict[str, MCPServerConfig]:
    """Load MCP server configs from user and project tiers.

    Returns a dict[server_name, MCPServerConfig]. Project tier shadows user tier
    (same name = project config wins).

    Args:
        cwd: Project root for resolving .minion/mcp.json. Defaults to Path.cwd().
    """
    project_root = cwd or Path.cwd()
    tiers = [
        Path.home() / ".minion" / "mcp.json",
        project_root / ".minion" / "mcp.json",
    ]

    configs: dict[str, MCPServerConfig] = {}
    for path in tiers:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            console.print(f"[muted]Warning: skipping malformed {path}: {e}[/]")
            continue

        servers = raw.get("servers", {})
        if not isinstance(servers, dict):
            console.print(f"[muted]Warning: 'servers' in {path} must be an object — skipping.[/]")
            continue

        for name, spec in servers.items():
            if not isinstance(spec, dict):
                console.print(f"[muted]Warning: server '{name}' in {path} must be an object — skipping.[/]")
                continue

            url = spec.get("url", "")
            command = spec.get("command", [])

            if url:
                # Streamable HTTP transport
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    console.print(f"[muted]Warning: server '{name}' has invalid 'url' (must start with http:// or https://) — skipping.[/]")
                    continue
                configs[name] = MCPServerConfig(
                    name=name,
                    url=url,
                    confirm_all=bool(spec.get("confirm_all", False)),
                )
            elif command:
                # stdio transport
                if not isinstance(command, list) or not command:
                    console.print(f"[muted]Warning: server '{name}' missing valid 'command' list — skipping.[/]")
                    continue
                configs[name] = MCPServerConfig(
                    name=name,
                    command=[str(c) for c in command],
                    env={str(k): str(v) for k, v in spec.get("env", {}).items()},
                    confirm_all=bool(spec.get("confirm_all", False)),
                )
            else:
                console.print(f"[muted]Warning: server '{name}' must have either 'command' (stdio) or 'url' (HTTP) — skipping.[/]")

    return configs
