"""Plan file storage — save, load, and list plan documents.

Plans live in <project-cwd>/.minion/plans/ as timestamped markdown files.
Project-local storage keeps plans scoped to the codebase they describe.
"""

import re
from datetime import datetime
from pathlib import Path


def plans_dir() -> Path:
    """Return the plans directory, creating it if needed."""
    d = Path.cwd() / ".minion" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_filename(goal: str) -> str:
    """Build a unique filename: YYYY-MM-DD-<slug>.md."""
    date = datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower().strip())
    slug = slug[:40].strip("-")
    base = f"{date}-{slug}.md"
    target = plans_dir() / base
    if not target.exists():
        return base
    # Collision — append suffix
    for n in range(2, 100):
        candidate = f"{date}-{slug}-{n}.md"
        if not (plans_dir() / candidate).exists():
            return candidate
    return base  # fallback (extremely unlikely)


def save_plan(content: str, goal: str) -> Path:
    """Write plan content to a new file; return the path."""
    filename = _make_filename(goal)
    path = plans_dir() / filename
    path.write_text(content, encoding="utf-8")
    return path


def load_plan(path: Path) -> str:
    """Read and return plan file content."""
    return path.read_text(encoding="utf-8")


def list_plans() -> list[Path]:
    """Return all plan files sorted newest-first by modification time."""
    d = plans_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
