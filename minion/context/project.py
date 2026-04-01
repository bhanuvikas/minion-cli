"""ProjectContext — combines manifest, file tree, and MINION.md into one object.

This is the single thing that callers (repl.py, cli.py) interact with.
build_project_context() is called once at startup; the result is passed to
build_system_prompt() in prompts.py to produce the session's system prompt.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .filetree import build_file_tree
from .manifest import ProjectManifest, detect_project

MINION_MD_FILENAME = "MINION.md"


@dataclass
class ProjectContext:
    """Everything minion knows about the project before the first prompt.

    to_prompt_block() produces the text injected into the system prompt.
    All fields are optional — we degrade gracefully for unrecognised projects.
    """
    cwd: Path
    manifest: Optional[ProjectManifest]
    file_tree: str
    minion_md: Optional[str]      # raw contents of MINION.md, None if absent

    def to_prompt_block(self) -> str:
        """Return the full context block for system prompt injection."""
        sections: list[str] = ["## Project Context"]

        if self.manifest:
            sections.append(self.manifest.to_text())
        else:
            sections.append(f"Working directory: {self.cwd}")

        sections.append(f"\nFile structure:\n{self.file_tree}")

        if self.minion_md:
            sections.append(f"## Project Instructions (MINION.md)\n\n{self.minion_md.strip()}")

        return "\n".join(sections)

    @property
    def label(self) -> str:
        """Short human-readable label for startup display."""
        if self.manifest:
            parts = [self.manifest.language]
            if self.manifest.framework:
                parts.append(self.manifest.framework)
            return " · ".join(parts)
        return self.cwd.name


def build_project_context(cwd: Path) -> ProjectContext:
    """Build a ProjectContext by scanning the given directory.

    Called once at startup. All I/O failures are handled gracefully so
    a bad .gitignore or unreadable file never prevents minion from starting.
    """
    manifest = detect_project(cwd)

    try:
        file_tree = build_file_tree(cwd)
    except Exception:
        file_tree = "(could not read file tree)"

    minion_md: Optional[str] = None
    minion_md_path = cwd / MINION_MD_FILENAME
    if minion_md_path.exists():
        try:
            minion_md = minion_md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    return ProjectContext(
        cwd=cwd,
        manifest=manifest,
        file_tree=file_tree,
        minion_md=minion_md,
    )
