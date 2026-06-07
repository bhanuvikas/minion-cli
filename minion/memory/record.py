"""MemoryRecord — the single unit of persistent memory.

Each record is stored as a markdown file with YAML-like frontmatter:

    ---
    id: abc123
    type: semantic
    scope: project
    project_path: /home/user/projects/my-project
    tags: database, postgresql
    created_at: 2026-04-02T10:30:00
    superseded_by:
    ---

    User prefers PostgreSQL 15 with pgvector extension for this project.

File format is intentionally human-readable so users can inspect, edit,
and delete memories directly without needing the CLI.

Serialization contract:
  - tags: comma-separated string in the file; list[str] in Python
  - project_path: empty string if None
  - superseded_by: empty string if None
  - content: everything after the closing --- (stripped)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_FRONTMATTER_DELIMITER = "---"


@dataclass
class MemoryRecord:
    """A single unit of persistent memory.

    type:  "semantic"  — stable fact or preference (e.g. "user prefers pytest")
           "episodic"  — timestamped event (e.g. "fixed auth bug on 2026-04-02")

    scope: "global"    — applies across all projects
           "project"   — specific to one codebase (project_path is set)

    category: "identity"   — who the user is (name, role, background)
              "preference" — how they like to work (tools, style, conventions)
              "project"    — technical facts about a project or cross-project patterns
              "event"      — catch-all: timestamped occurrences, discoveries, observations

    superseded_by: set when a newer memory replaces this one during
    consolidation; superseded records are excluded from retrieval results.
    """

    id: str
    content: str
    type: str                        # "semantic" | "episodic"
    scope: str                       # "global" | "project"
    project_path: Optional[str]      # absolute cwd when created; None for global
    tags: list[str] = field(default_factory=list)
    created_at: str = ""             # ISO 8601 timestamp
    superseded_by: Optional[str] = None  # ID of the record that replaced this one
    category: str = "project"        # "identity" | "preference" | "project" | "event"
    pinned: bool = False             # pinned memories sort to top in /memories modal

    # ─── Serialization ────────────────────────────────────────────────────────

    def to_file_content(self) -> str:
        """Render to the markdown-with-frontmatter file format."""
        lines = [
            _FRONTMATTER_DELIMITER,
            f"id: {self.id}",
            f"type: {self.type}",
            f"category: {self.category}",
            f"scope: {self.scope}",
            f"project_path: {self.project_path or ''}",
            f"tags: {', '.join(self.tags)}",
            f"created_at: {self.created_at}",
            f"superseded_by: {self.superseded_by or ''}",
            f"pinned: {str(self.pinned).lower()}",
            _FRONTMATTER_DELIMITER,
            "",
            self.content,
        ]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_file(cls, path: Path) -> "MemoryRecord":
        """Parse a MemoryRecord from a .md file written by to_file_content()."""
        text = path.read_text(encoding="utf-8")
        return cls._parse(text)

    @classmethod
    def _parse(cls, text: str) -> "MemoryRecord":
        """Parse raw file content. Separated for testability."""
        parts = text.split(f"{_FRONTMATTER_DELIMITER}\n", 2)
        if len(parts) < 3:
            raise ValueError(f"Invalid memory file format — expected two '{_FRONTMATTER_DELIMITER}' delimiters")

        frontmatter_text = parts[1]
        content = parts[2].strip()

        kv: dict[str, str] = {}
        for line in frontmatter_text.splitlines():
            if ": " in line:
                key, _, value = line.partition(": ")
                kv[key.strip()] = value.strip()
            elif line.endswith(":"):
                kv[line[:-1].strip()] = ""

        raw_tags = kv.get("tags", "")
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        return cls(
            id=kv["id"],
            content=content,
            type=kv["type"],
            scope=kv["scope"],
            project_path=kv.get("project_path") or None,
            tags=tags,
            created_at=kv.get("created_at", ""),
            superseded_by=kv.get("superseded_by") or None,
            category=kv.get("category", "project"),
            pinned=kv.get("pinned", "false").lower() == "true",
        )

    # ─── Dict serialization (for JSON-based indices) ──────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "type": self.type,
            "category": self.category,
            "scope": self.scope,
            "project_path": self.project_path,
            "tags": self.tags,
            "created_at": self.created_at,
            "superseded_by": self.superseded_by,
            "pinned": self.pinned,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryRecord":
        return cls(
            id=data["id"],
            content=data["content"],
            type=data["type"],
            scope=data["scope"],
            project_path=data.get("project_path"),
            tags=data.get("tags", []),
            created_at=data.get("created_at", ""),
            superseded_by=data.get("superseded_by"),
            category=data.get("category", "project"),
            pinned=data.get("pinned", False),
        )
