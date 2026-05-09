"""minion memory — browse and manage persistent memory."""

from pathlib import Path
from typing import Optional

import typer

from ..theme import YELLOW, console, print_error

app = typer.Typer(name="memory", help="Browse and manage persistent memory.", add_completion=False)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """List all memories. Run without subcommand to show all stored memories."""
    if ctx.invoked_subcommand is None:
        _recall(query=None)


@app.command("recall")
def memory_recall(
    query: Optional[str] = typer.Argument(None, help="Optional search query"),
) -> None:
    """Show stored memories, optionally filtered by a search query."""
    _recall(query)


@app.command("add")
def memory_add(
    text: str = typer.Argument(..., help="Text to remember"),
    global_scope: bool = typer.Option(False, "--global", "-g", help="Store globally (not project-scoped)"),
    category: str = typer.Option(
        "project", "--category", "-c",
        help="Category: identity, preference, project, event",
    ),
) -> None:
    """Store a new memory."""
    import uuid
    from datetime import datetime, timezone

    from ..memory.config import MemoryConfig
    from ..memory.record import MemoryRecord
    from ..memory.store import MemoryStore

    if category not in ("identity", "preference", "project", "event"):
        print_error("--category must be one of: identity, preference, project, event")
        raise typer.Exit(code=1)

    scope = "global" if global_scope else "project"
    project_path: Optional[str] = None if global_scope else str(Path.cwd())
    store = MemoryStore(config=MemoryConfig(), project_cwd=Path.cwd())
    record = MemoryRecord(
        id=str(uuid.uuid4()),
        content=text.strip("\"'"),
        type="semantic",
        scope=scope,
        project_path=project_path,
        tags=[],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        superseded_by=None,
        category=category,
    )
    store.store(record)
    console.print(f"[bold {YELLOW}]Remembered[/] [muted]({scope}·{category}):[/] {text}")


@app.command("forget")
def memory_forget(
    query: str = typer.Argument(..., help="Memory ID (first 8 chars) or substring of the content"),
) -> None:
    """Delete memories matching an ID or content substring."""
    from ..memory.config import MemoryConfig
    from ..memory.store import MemoryStore

    store = MemoryStore(config=MemoryConfig(), project_cwd=Path.cwd())
    count = store.delete(query.strip("\"'"))
    if count:
        console.print(f"[bold {YELLOW}]Forgotten.[/] [muted]({count} memory removed)[/]")
    else:
        console.print("[muted]No matching memory found.[/]")


def _recall(query: Optional[str]) -> None:
    from datetime import datetime, timezone

    from ..memory.config import MemoryConfig
    from ..memory.store import MemoryStore

    store = MemoryStore(config=MemoryConfig(), project_cwd=Path.cwd())
    memories = store.list_all(query=query or None)
    if not memories:
        console.print("[muted]No memories stored yet.[/]")
        return
    for m in memories:
        try:
            dt = datetime.fromisoformat(m.created_at)
            delta = datetime.now(timezone.utc) - dt
            age = f"{delta.days}d ago" if delta.days > 0 else "today"
        except Exception:
            age = m.created_at
        console.print(
            f"  [{YELLOW}]{m.id[:8]}[/] [{m.category}·{m.scope}] "
            f"{m.content} [muted]({age})[/]"
        )
