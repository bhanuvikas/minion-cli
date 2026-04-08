"""MCP server configuration — loads ~/.minion/mcp.json and .minion/mcp.json.

Two-tier loading (user → project). Project server names shadow user server names.
If neither file exists, returns an empty dict — MCP is simply disabled.

Config format:
    {
      "servers": {
        "notes": {
          "command": ["python", "examples/mcp_notes_server.py"],
          "env": {},
          "confirm_all": false
        }
      }
    }

Fields:
    command      — subprocess argv list (required)
    env          — extra environment variables merged into os.environ (optional)
    confirm_all  — if true, every tool on this server requires user confirmation
                   regardless of its destructiveHint annotation (optional, default false)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..theme import console


@dataclass
class MCPServerConfig:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    confirm_all: bool = False


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
            command = spec.get("command")
            if not isinstance(command, list) or not command:
                console.print(f"[muted]Warning: server '{name}' missing valid 'command' list — skipping.[/]")
                continue
            configs[name] = MCPServerConfig(
                name=name,
                command=[str(c) for c in command],
                env={str(k): str(v) for k, v in spec.get("env", {}).items()},
                confirm_all=bool(spec.get("confirm_all", False)),
            )

    return configs
