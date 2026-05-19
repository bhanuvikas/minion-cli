"""Atomic YAML rewrite helpers for hook manifest files."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    tmp.rename(path)


def update_hook_yaml(path: Path, updates: dict[str, Any]) -> None:
    """Load an existing hook YAML, apply updates, write back atomically.

    Keys mapped to None are removed (field absent, not null).
    """
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key, value in updates.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    _atomic_write(path, raw)


def create_hook_yaml(
    path: Path,
    name: str,
    event: str,
    command: str,
    *,
    tools: Optional[list[str]] = None,
    description: str = "",
    timeout: int = 30,
    blocking: Optional[bool] = None,
) -> None:
    """Write a brand-new hook YAML file atomically.

    ``blocking=None`` (the default) is omitted from the file — the runner
    will apply per-event defaults (PreToolUse blocks, others don't).
    ``tools=None`` means fire on every tool call (field omitted from file).
    """
    raw: dict[str, Any] = {"name": name, "event": event, "command": command}
    if description:
        raw["description"] = description
    if tools:
        raw["tools"] = tools
    raw["timeout"] = timeout
    if blocking is not None:
        raw["blocking"] = blocking
    _atomic_write(path, raw)
