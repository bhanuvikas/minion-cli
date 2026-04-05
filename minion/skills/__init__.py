"""Skills system — named, reusable prompt+tool workflows.

Public API:
  load_skill_registry()  — discover skills from 3 tiers (builtin/user/project)
  SkillRegistry          — lookup and iteration
  SkillManifest          — the dataclass representing a loaded skill
"""

from .manifest import SkillManifest
from .registry import SkillRegistry, load_skill_registry

__all__ = ["SkillManifest", "SkillRegistry", "load_skill_registry"]
