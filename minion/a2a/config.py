"""A2A agent configuration — loads ~/.minion/a2a.json and .minion/a2a.json.

Two-tier loading (user → project). Project agent names shadow user agent names.
If neither file exists, returns an empty dict — A2A is simply disabled.

Config format:
    {
      "agents": {
        "remote_coder": {
          "url": "http://localhost:8080",
          "timeout_seconds": 60
        },
        "external_reviewer": {
          "url": "https://reviewer.example.com",
          "timeout_seconds": 120
        }
      }
    }

Fields:
    url              — base URL of the remote A2A agent (required)
                       Agent Card served at <url>/.well-known/agent.json
    timeout_seconds  — request timeout in seconds (optional, default 60)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..theme import console


@dataclass
class A2AAgentConfig:
    name: str
    url: str              # base URL; agent card at url/.well-known/agent.json
    timeout_seconds: int = 60


def load_a2a_config(cwd: Path | None = None) -> dict[str, A2AAgentConfig]:
    """Load A2A agent configs from user and project tiers.

    Returns dict[agent_name, A2AAgentConfig]. Project tier shadows user tier
    on name collision. Returns {} if no config files found.

    Args:
        cwd: Project root for resolving .minion/a2a.json. Defaults to Path.cwd().
    """
    project_root = cwd or Path.cwd()
    tiers = [
        Path.home() / ".minion" / "a2a.json",
        project_root / ".minion" / "a2a.json",
    ]

    configs: dict[str, A2AAgentConfig] = {}
    for path in tiers:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            console.print(f"[muted]Warning: skipping malformed {path}: {e}[/]")
            continue

        agents = raw.get("agents", {})
        if not isinstance(agents, dict):
            console.print(f"[muted]Warning: 'agents' in {path} must be an object — skipping.[/]")
            continue

        for name, spec in agents.items():
            if not isinstance(spec, dict):
                console.print(f"[muted]Warning: agent '{name}' in {path} must be an object — skipping.[/]")
                continue

            url = spec.get("url", "")
            if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
                console.print(
                    f"[muted]Warning: agent '{name}' in {path} has invalid 'url' "
                    f"(must start with http:// or https://) — skipping.[/]"
                )
                continue

            timeout = spec.get("timeout_seconds", 60)
            if not isinstance(timeout, int) or timeout <= 0:
                timeout = 60

            configs[name] = A2AAgentConfig(
                name=name,
                url=url.rstrip("/"),
                timeout_seconds=timeout,
            )

    return configs
