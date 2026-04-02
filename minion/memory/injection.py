"""inject_memories — format retrieved memories into the system prompt.

Called once per turn in repl.py before run_prompt(). Returns the base
system prompt unchanged when no memories are provided.

The memory block is appended to the system prompt (not injected as a
separate message). Note: this means the system prompt changes per turn
and is not prompt-cache-friendly. A future Phase 12 optimisation can
move injection to a prepended message in conversation.messages.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .record import MemoryRecord


_SECTION_LABELS = {
    "identity":   "About the user",
    "preference": "User preferences",
    "project":    "Project context",
    "event":      "From past sessions",
}

_SECTION_ORDER = ["identity", "preference", "project", "event"]


def inject_memories(base_system_prompt: str, memories: list["MemoryRecord"]) -> str:
    """Append a formatted ## What I Remember block to the system prompt.

    Memories are grouped by category into labelled sections so the model
    can easily distinguish always-injected context (identity, preference,
    project) from query-relevant episodic events.

    Returns base_system_prompt unchanged when memories is empty.
    """
    if not memories:
        return base_system_prompt

    by_category: dict[str, list] = {cat: [] for cat in _SECTION_ORDER}
    for m in memories:
        cat = m.category if m.category in by_category else "event"
        by_category[cat].append(m)

    lines = ["\n\n## What I Remember\n"]
    for cat in _SECTION_ORDER:
        records = by_category[cat]
        if not records:
            continue
        lines.append(f"\n### {_SECTION_LABELS[cat]}")
        for m in records:
            age = _format_age(m.created_at)
            tag_hint = f" [{', '.join(m.tags[:3])}]" if m.tags else ""
            lines.append(f"- {tag_hint}{m.content}  *(remembered {age})*")

    return base_system_prompt + "\n".join(lines)


def _format_age(iso_timestamp: str) -> str:
    """Return a human-readable age string for a memory's created_at timestamp.

    Examples: "just now", "3 hours ago", "2 days ago", "unknown"
    """
    if not iso_timestamp:
        return "unknown"
    try:
        created = datetime.fromisoformat(iso_timestamp)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        seconds = max(0.0, (now - created).total_seconds())
    except (ValueError, OverflowError):
        return "unknown"

    if seconds < 60:
        return "just now"
    minutes = math.floor(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = math.floor(seconds / 3600)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = math.floor(seconds / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"
