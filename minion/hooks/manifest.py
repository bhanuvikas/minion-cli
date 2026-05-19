"""HookManifest — dataclass for a single hook definition loaded from YAML.

YAML format for user/project hooks (~/.minion/hooks/*.yaml, .minion/hooks/*.yaml):

    name: block-force-push
    description: Prevent force pushes to remote
    event: PreToolUse
    tools: [run_shell, bash]   # optional list — omit to match all tools
    command: ~/.minion/hooks/check-force-push.sh
    timeout: 10               # optional, default 30
    blocking: true            # optional — overrides event default

Legacy single-tool format (still supported on read):
    tool: run_shell
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class HookManifest:
    name: str
    description: str
    event: str              # "PreToolUse" | "PostToolUse" | "SessionStart" | ...
    command: str            # shell command; run in cwd at fire time
    tools: Optional[list[str]] = None  # None = all tools; list = filter to these tools
    timeout: int = 30
    blocking: Optional[bool] = None  # None = event-default (True for Pre, False for Post)
    source: str = "user"             # "user" | "project"
    source_path: Optional[Path] = None


def load_manifest(path: Path, source: str = "user") -> HookManifest:
    """Parse a single hooks YAML file into HookManifest."""
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if "name" not in data:
        raise ValueError(f"missing required field 'name'")
    if "event" not in data:
        raise ValueError(f"missing required field 'event'")
    if "command" not in data:
        raise ValueError(f"missing required field 'command'")

    # Resolve tools: prefer new `tools` list, fall back to legacy `tool` string.
    raw_tools = data.get("tools")
    raw_tool = data.get("tool")
    if raw_tools is not None:
        if isinstance(raw_tools, list):
            tools: Optional[list[str]] = [str(t) for t in raw_tools] or None
        else:
            tools = [str(raw_tools)] if raw_tools else None
    elif raw_tool:
        tools = [str(raw_tool)]
    else:
        tools = None

    return HookManifest(
        name=str(data["name"]),
        description=str(data.get("description", "")),
        event=str(data["event"]),
        command=str(data["command"]),
        tools=tools,
        timeout=int(data.get("timeout", 30)),
        blocking=data.get("blocking"),
        source=source,
        source_path=path,
    )
