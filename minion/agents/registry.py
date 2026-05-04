"""Agent role registry — three-tier loader.

Loads AgentRoleManifest instances from three tiers (lowest → highest priority):
  1. minion/agents/builtin/   (shipped with the package)
  2. ~/.minion/agents/         (user-global custom roles)
  3. <cwd>/.minion/agents/     (project-local custom roles)

Higher-tier names shadow lower-tier names, so a project role named "researcher"
replaces the builtin one for that session.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..theme import startup_warnings
from .manifest import AgentRoleManifest, load_manifest

if TYPE_CHECKING:
    pass

# Alias — same pattern as SkillRegistry in minion/skills/registry.py
AgentRegistry = dict[str, AgentRoleManifest]

_BUILTIN_DIR = Path(__file__).parent / "builtin"


def load_agent_registry(cwd: Path) -> AgentRegistry:
    """Build the merged AgentRegistry from all three tiers.

    Returns an empty dict (never None) if no YAML files are found.
    Unreadable or invalid files are skipped with a warning.
    """
    registry: AgentRegistry = {}

    tiers = [
        (_BUILTIN_DIR, "builtin"),
        (Path.home() / ".minion" / "agents", "user"),
        (cwd / ".minion" / "agents", "project"),
    ]

    for tier_dir, source in tiers:
        if not tier_dir.is_dir():
            continue
        for yaml_path in sorted(tier_dir.glob("*.yaml")):
            try:
                manifest = load_manifest(yaml_path, source=source)
                registry[manifest.name] = manifest
            except (ValueError, yaml.YAMLError, OSError) as exc:
                startup_warnings.append(f"[muted]  ⚠ Skipping agent role '{yaml_path.name}': {exc}[/]")

    return registry
