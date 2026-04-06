"""Skill manifest — YAML definition loading and dataclasses.

A skill is a named, reusable prompt+tool workflow. The manifest captures:
  - prompt:   system prompt augmentation (may contain {arg} placeholder)
  - tools:    allowed tool subset (None = all tools; [] = no tools)
  - steps:    skill chaining (list of skill names to invoke in sequence)
  - args:     documented argument definitions (informational)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class SkillArg:
    name: str
    description: str
    required: bool = False


@dataclass
class SkillManifest:
    name: str                              # "commit" → invoked as /commit
    description: str                       # shown in /help, /skills, tab completion
    prompt: str                            # system prompt augmentation; may contain {arg}
    tools: Optional[list[str]] = None      # None = all tools; [] = no tools; [...] = subset
    args: list[SkillArg] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)   # skill chaining
    max_iterations: int = 20
    source: str = "builtin"                # "builtin" | "user" | "project"
    output_format: str = "stream"          # "stream" (live) | "markdown" (collect then render)
    thinking_label: str = ""               # spinner text shown while collecting (output_format=markdown)


def load_manifest(path: Path, source: str = "builtin") -> SkillManifest:
    """Parse a YAML skill file into a SkillManifest.

    Raises ValueError if the required 'prompt' field is missing.
    The 'name' field falls back to the filename stem if absent.
    'tools: null' or absent → None (all tools allowed).
    """
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    name = data.get("name") or path.stem
    description = data.get("description", "")
    prompt = data.get("prompt", "")

    if not prompt:
        raise ValueError(f"Skill '{name}' at {path} is missing required 'prompt' field")

    raw_args = data.get("args") or []
    args = [
        SkillArg(
            name=a["name"],
            description=a.get("description", ""),
            required=a.get("required", False),
        )
        for a in raw_args
    ]

    return SkillManifest(
        name=name,
        description=description,
        prompt=prompt,
        tools=data.get("tools"),       # None if key absent or explicitly null
        args=args,
        steps=data.get("steps") or [],
        max_iterations=data.get("max_iterations", 20),
        source=source,
        output_format=data.get("output_format", "stream"),
        thinking_label=data.get("thinking_label", ""),
    )
