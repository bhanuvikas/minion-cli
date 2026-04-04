"""Nefario CLI — standalone trace viewer for minion sessions.

Entry point: nefario

Usage:
    nefario                          list recent sessions
    nefario trace                    list recent sessions
    nefario trace <session-id>       open specific session in browser
    nefario trace --latest           open most recent session
    nefario trace --latest --port N  use a different port
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from ..theme import console

_PST = timezone(timedelta(hours=-8))   # UTC-8; Python stdlib has no IANA tz support
_PDT = timezone(timedelta(hours=-7))   # UTC-7 during daylight saving

def _to_pst(utc_str: str) -> str:
    """Convert a UTC ISO timestamp string to PST/PDT wall-clock string."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        # Rough DST: second Sunday in March → first Sunday in November
        # Good enough for display purposes
        year = dt.year
        dst_start = datetime(year, 3,  8, 2, tzinfo=timezone.utc)  # on or after Mar 8
        dst_start += timedelta(days=(6 - dst_start.weekday()) % 7)  # next Sunday
        dst_end   = datetime(year, 11, 1, 2, tzinfo=timezone.utc)   # on or after Nov 1
        dst_end  += timedelta(days=(6 - dst_end.weekday()) % 7)
        tz = _PDT if dst_start <= dt < dst_end else _PST
        local = dt.astimezone(tz)
        label = "PDT" if tz is _PDT else "PST"
        return local.strftime(f"%Y-%m-%d %H:%M:%S {label}")
    except Exception:
        return utc_str[:19].replace("T", " ")

TRACES_DIR = Path.home() / ".minion" / "traces"

app = typer.Typer(
    name="nefario",
    help="🔍 Nefario — trace viewer for minion sessions.",
    add_completion=False,
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)


def _list_sessions(traces_dir: Path) -> None:
    """Print a table of recent trace sessions."""
    files = sorted(traces_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        console.print("[muted]No trace sessions found in ~/.minion/traces/[/]")
        return

    table = Table(title="Recent trace sessions", show_lines=False)
    table.add_column("Session ID", style="bold")
    table.add_column("Started", style="dim")
    table.add_column("Turns", justify="right")
    table.add_column("Tokens in/out", justify="right")

    for f in files[:20]:
        session_id = f.stem
        turns = 0
        started = ""
        tokens_in = 0
        tokens_out = 0
        try:
            lines = f.read_text(encoding="utf-8").strip().splitlines()
            for line in lines:
                try:
                    ev = json.loads(line)
                    if ev.get("event_type") == "session_start" and not started:
                        started = _to_pst(ev.get("timestamp", ""))
                    elif ev.get("event_type") == "user_turn":
                        turns += 1
                    elif ev.get("event_type") == "session_end":
                        d = ev.get("data", {})
                        turns = d.get("total_turns", turns)
                        tokens_in = d.get("total_input_tokens", 0)
                        tokens_out = d.get("total_output_tokens", 0)
                except (json.JSONDecodeError, KeyError):
                    continue
        except OSError:
            continue

        token_str = f"{tokens_in:,} / {tokens_out:,}" if tokens_in or tokens_out else "—"
        table.add_row(session_id, started, str(turns), token_str)

    console.print(table)


def _resolve_session(session_id: Optional[str], traces_dir: Path, latest: bool) -> str:
    """Return the session ID to view, resolving --latest or validating explicit ID."""
    if latest or session_id is None:
        files = sorted(traces_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            console.print("[red]No trace sessions found.[/]")
            raise typer.Exit(code=1)
        return files[-1].stem

    path = traces_dir / f"{session_id}.jsonl"
    if not path.exists():
        console.print(f"[red]Session not found:[/] {session_id}")
        raise typer.Exit(code=1)
    return session_id


def _run(session_id: Optional[str], latest: bool, port: int) -> None:
    """Shared logic for both the default callback and the trace subcommand."""
    if not TRACES_DIR.exists():
        console.print("[muted]No traces directory found. Run minion first.[/]")
        raise typer.Exit()

    if session_id is None and not latest:
        _list_sessions(TRACES_DIR)
        return

    target = _resolve_session(session_id, TRACES_DIR, latest)
    console.print(f"[bold]Opening trace:[/] {target}")
    from .server import start_viewer
    start_viewer(target, TRACES_DIR, port=port)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    latest: bool = typer.Option(False, "--latest", help="Open the most recent session."),
    port: int   = typer.Option(7331,  "--port",   help="Port for the trace viewer server."),
) -> None:
    """🔍 [bold]Nefario[/bold] — trace viewer for minion sessions.

    Run without arguments to list recent sessions.
    Pass a session ID or --latest to open the viewer.
    """
    if ctx.invoked_subcommand is not None:
        return
    session_id = ctx.args[0] if ctx.args else None
    _run(session_id, latest, port)


@app.command("trace")
def trace_cmd(
    session_id: Optional[str] = typer.Argument(None, help="Session ID to view."),
    latest: bool = typer.Option(False, "--latest", help="Open the most recent session."),
    port: int    = typer.Option(7331,  "--port",   help="Port for the trace viewer server."),
) -> None:
    """List or open a trace session.

    With no arguments: lists recent sessions.
    With a session ID or --latest: starts the viewer at http://localhost:PORT.
    """
    _run(session_id, latest, port)
