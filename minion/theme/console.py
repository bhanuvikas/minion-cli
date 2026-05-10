"""Shared Rich Console instance, startup warnings list, and spinner coordination."""

from rich.console import Console

from .palette import MINION_THEME

console = Console(theme=MINION_THEME, highlight=False)

# Startup warnings collected by loaders (skills, agents, etc.) before the banner
# is printed. repl/session.py flushes this list after print_greeting().
startup_warnings: list[str] = []

# Rich Status object set by executor while polling (A2A remote wait).
_active_status = None


def set_active_status(status) -> None:
    global _active_status
    _active_status = status


def pause_spinner() -> None:
    """Stop the active spinner so interactive prompts can render cleanly."""
    if _active_status is not None:
        _active_status.stop()


def resume_spinner() -> None:
    """Restart the active spinner after an interactive prompt completes."""
    if _active_status is not None:
        _active_status.start()
