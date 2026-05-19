"""HookManifest — dataclass for a single hook definition loaded from YAML.

YAML format for user/project hooks (~/.minion/hooks/*.yaml, .minion/hooks/*.yaml):

    name: block-force-push
    description: Prevent force pushes to remote
    event: PreToolUse
    tool: run_shell           # optional — omit to match all tools
    command: ~/.minion/hooks/check-force-push.sh
    timeout: 10               # optional, default 30
    blocking: true            # optional — overrides event default
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class HookManifest:
    name: str
    description: str
    event: str              # "PreToolUse" | "PostToolUse" | "SessionStart" | ...
    command: str            # shell command; run in cwd at fire time
    tool: Optional[str] = None       # tool name filter; None = all tools
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
    return HookManifest(
        name=str(data["name"]),
        description=str(data.get("description", "")),
        event=str(data["event"]),
        command=str(data["command"]),
        tool=data.get("tool") or None,
        timeout=int(data.get("timeout", 30)),
        blocking=data.get("blocking"),
        source=source,
        source_path=path,
    )
