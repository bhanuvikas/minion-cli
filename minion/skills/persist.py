"""Atomic YAML rewrite helper for skill manifest files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def update_skill_yaml(path: Path, updates: dict[str, Any]) -> None:
    """Load the skill YAML, apply updates atomically, write back via .tmp rename.

    Keys mapped to None are removed from the file (saves as absent, not null).
    Keys mapped to any other value are set or updated.
    """
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key, value in updates.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        yaml.dump(raw, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.rename(path)
