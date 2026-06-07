"""Agent role manifest — dataclass and YAML loader.

AgentRoleManifest defines the identity of a subagent role: its full system
prompt, the tools it may use, and how many iterations it is allowed.

Contrast with SkillManifest: skills *augment* the orchestrator's system prompt
via a prompt: field that may contain {arg} placeholders. Roles *are* the full
system prompt for an isolated child agent — no augmentation, no placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class AgentRoleManifest:
    """Describes a named agent role loaded from a YAML file.

    Fields
    ------
    name          : Role key used in spawn_agent calls (e.g. "researcher").
                    Defaults to the YAML filename stem if absent.
    description   : One-line description shown in /agents and `minion agent list`.
    system_prompt : Full system prompt for the worker agent. Replaces
                    BASE_SYSTEM_PROMPT entirely — not an augmentation.
    tools         : None = all native tools; [] = no tools; [...] = named subset.
    max_iterations: ReAct loop iteration limit for this role.
    source        : Loading tier — "builtin" | "user" | "project".
    source_path   : Absolute path to the YAML file on disk. Always set after load.
    """

    name: str
    description: str
    system_prompt: str
    tools: Optional[list[str]] = None
    max_iterations: int = 20
    source: str = "builtin"
    source_path: Optional[Path] = None
    model: Optional[str] = None    # model override slug; None = inherit session model
    color: Optional[str] = None    # one of gold/green/blue/orange/silver/muted; None = tier default


def load_manifest(path: Path, source: str = "builtin") -> AgentRoleManifest:
    """Parse a YAML file into an AgentRoleManifest.

    Raises ValueError if `system_prompt` is missing (the only required field
    beyond `name`, which falls back to the filename stem).
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    system_prompt = raw.get("system_prompt", "")
    if not system_prompt or not system_prompt.strip():
        raise ValueError(
            f"Agent role YAML '{path}' must define a non-empty 'system_prompt'."
        )

    name = raw.get("name") or path.stem
    description = raw.get("description", "")
    tools: Optional[list[str]] = raw.get("tools")  # None if key absent
    max_iterations = int(raw.get("max_iterations", 20))
    model: Optional[str] = raw.get("model") or None
    color: Optional[str] = raw.get("color") or None

    return AgentRoleManifest(
        name=name,
        description=description,
        system_prompt=system_prompt.strip(),
        tools=tools,
        max_iterations=max_iterations,
        source=source,
        source_path=path,
        model=model,
        color=color,
    )
