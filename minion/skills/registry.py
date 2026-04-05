"""Skill registry — 3-tier discovery and lookup.

Tiers (lowest to highest priority — last write wins on name collision):
  1. builtin  — minion/skills/builtin/*.yaml  (shipped with minion)
  2. user     — ~/.minion/skills/*.yaml        (personal global skills)
  3. project  — .minion/skills/*.yaml          (project-specific, relative to cwd)

A project skill named "commit" shadows the builtin "commit".
"""

from __future__ import annotations

from pathlib import Path
from typing import ItemsView, Iterator

from .manifest import SkillManifest, load_manifest
from ..theme import console

_BUILTIN_DIR = Path(__file__).parent / "builtin"
_USER_DIR = Path.home() / ".minion" / "skills"


class SkillRegistry:
    """Immutable mapping of skill name → SkillManifest.

    Created by load_skill_registry() — do not instantiate directly.
    """

    def __init__(self, skills: dict[str, SkillManifest]) -> None:
        self._skills = skills

    def get(self, name: str) -> SkillManifest | None:
        return self._skills.get(name)

    def items(self) -> ItemsView[str, SkillManifest]:
        return self._skills.items()

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __iter__(self) -> Iterator[str]:
        return iter(self._skills)

    def __len__(self) -> int:
        return len(self._skills)


def load_skill_registry(cwd: Path | None = None) -> SkillRegistry:
    """Discover and load skills from all 3 tiers.

    Skills are keyed by name. When the same name appears in multiple tiers,
    the highest-priority tier wins (project > user > builtin).

    Args:
        cwd: Project root for resolving .minion/skills/. Defaults to Path.cwd().

    Returns:
        A SkillRegistry populated with all discovered skills.
    """
    cwd = cwd or Path.cwd()
    tiers = [
        (_BUILTIN_DIR, "builtin"),
        (_USER_DIR, "user"),
        (cwd / ".minion" / "skills", "project"),
    ]
    skills: dict[str, SkillManifest] = {}
    for directory, source in tiers:
        if not directory.exists():
            continue
        for yaml_path in sorted(directory.glob("*.yaml")):
            try:
                manifest = load_manifest(yaml_path, source=source)
                skills[manifest.name] = manifest
            except Exception as e:
                console.print(f"[muted]Warning: skipping skill {yaml_path.name}: {e}[/]")
    return SkillRegistry(skills)
