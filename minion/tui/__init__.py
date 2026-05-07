"""minion/tui — prompt_toolkit Application for the full TUI experience.

Active only when stdout is a TTY and MINION_NO_TUI is not set.
Non-TTY fallback (tests, pipes, CI) uses the existing Rich/questionary path.
"""

import contextvars
from typing import TYPE_CHECKING, Optional

from .app import MinionApp

if TYPE_CHECKING:
    pass


def tui_print(renderable) -> None:
    """Print a Rich markup string or renderable through the active TUI (run_in_terminal).

    Falls back to a plain Rich console when no TUI is active, so callers
    don't need to branch — just replace console.print() with tui_print().
    """
    app = _tui_app_var.get()
    if app is not None:
        app.print_renderable(renderable)
    else:
        from rich.console import Console as _C
        _C(highlight=False).print(renderable)

_tui_app_var: contextvars.ContextVar[Optional["MinionApp"]] = contextvars.ContextVar(
    "tui_app", default=None
)


def get_tui_app() -> Optional["MinionApp"]:
    """Return the active MinionApp for this context, or None."""
    return _tui_app_var.get()


def set_tui_app(app: Optional["MinionApp"]) -> None:
    """Register (or clear) the active MinionApp for this context."""
    _tui_app_var.set(app)


def is_tui_active() -> bool:
    """True when a MinionApp is running in this context."""
    return _tui_app_var.get() is not None
