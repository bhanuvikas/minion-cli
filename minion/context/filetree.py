"""File tree builder — walks a directory and produces a compact text tree.

Respects ignore rules from three sources, applied in this order:
  1. ALWAYS_IGNORE — hardcoded noise (build artifacts, caches, vcs metadata)
  2. .gitignore    — developer's existing exclusion rules
  3. .minionignore — minion-specific exclusions (same syntax as .gitignore)

.gitignore and .minionignore patterns are additive. The format supported:
  - Exact name matches:       __pycache__
  - Glob patterns:            *.pyc, dist-*
  - Relative path patterns:   src/generated/
  (Full .gitignore spec — negation, anchored patterns — is not implemented.
   That would require a library. The common 90% case is covered here.)
"""

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

# ─── Hardcoded noise — always excluded regardless of ignore files ─────────────

ALWAYS_IGNORE: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", ".minion",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".dmypy.json",
    "node_modules", ".next", ".nuxt", ".svelte-kit",
    ".venv", "venv", "env", ".env",
    "build", "dist", "target", "out", "bin", "obj",
    ".tox", "coverage", ".coverage",
    ".DS_Store", "Thumbs.db",
})

_MAX_ENTRIES = 80   # total file+dir entries before truncating
_MAX_FILES_PER_DIR = 20  # files shown per directory before folding the rest


# ─── Ignore rules ─────────────────────────────────────────────────────────────

@dataclass
class IgnoreRules:
    """Compiled ignore patterns loaded from .gitignore and .minionignore."""
    patterns: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, cwd: Path) -> "IgnoreRules":
        patterns: list[str] = []
        for filename in (".gitignore", ".minionignore"):
            ignore_file = cwd / filename
            if not ignore_file.exists():
                continue
            for raw_line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip().rstrip("/")
                if line and not line.startswith("#") and not line.startswith("!"):
                    patterns.append(line)
        return cls(patterns=patterns)

    def is_ignored(self, entry: Path, cwd: Path) -> bool:
        """Return True if this entry should be excluded from the tree."""
        name = entry.name

        if name in ALWAYS_IGNORE:
            return True

        for pattern in self.patterns:
            # Match against bare name
            if fnmatch.fnmatch(name, pattern):
                return True
            # Match against path relative to project root
            try:
                rel = str(entry.relative_to(cwd))
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, f"{pattern}/*"):
                    return True
            except ValueError:
                pass

        return False


# ─── Tree builder ─────────────────────────────────────────────────────────────

def build_file_tree(cwd: Path, max_depth: int = 3) -> str:
    """Return an indented text tree of the project, respecting ignore rules.

    Entries are sorted: directories first (alphabetically), then files.
    Stops emitting entries after _MAX_ENTRIES to keep the block token-compact.
    """
    rules = IgnoreRules.load(cwd)
    lines: list[str] = []
    counter = _Counter()

    _walk(cwd, cwd, rules, depth=0, max_depth=max_depth, lines=lines, counter=counter)

    if counter.truncated:
        lines.append(f"  ... ({counter.truncated} more entries not shown)")

    return "\n".join(lines) if lines else "(empty)"


# ─── Internal helpers ─────────────────────────────────────────────────────────

class _Counter:
    """Shared mutable counter so the recursive walker can stop globally."""
    __slots__ = ("shown", "truncated")

    def __init__(self) -> None:
        self.shown = 0
        self.truncated = 0

    @property
    def full(self) -> bool:
        return self.shown >= _MAX_ENTRIES


def _walk(
    directory: Path,
    cwd: Path,
    rules: IgnoreRules,
    depth: int,
    max_depth: int,
    lines: list[str],
    counter: _Counter,
) -> None:
    if depth > max_depth or counter.full:
        return

    try:
        entries = sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except PermissionError:
        return

    indent = "  " * depth
    files_this_dir = 0

    for entry in entries:
        if counter.full:
            counter.truncated += 1
            continue

        if rules.is_ignored(entry, cwd):
            continue

        if entry.is_dir():
            lines.append(f"{indent}{entry.name}/")
            counter.shown += 1
            _walk(entry, cwd, rules, depth + 1, max_depth, lines, counter)
        elif entry.is_file():
            if files_this_dir >= _MAX_FILES_PER_DIR:
                counter.truncated += 1
                continue
            lines.append(f"{indent}{entry.name}")
            counter.shown += 1
            files_this_dir += 1
