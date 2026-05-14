"""Shell tab-completion setup modal — shown automatically on first run."""
from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..theme import DIM, GOLD, GREEN, SILVER

_FOOT_IDLE  = (
    f"  [bold {SILVER}]↵[/] [{DIM}]install[/]  [{DIM}]·[/]  "
    f"[bold {SILVER}]esc[/] [{DIM}]skip for now[/]"
)
_FOOT_DONE  = f"  [bold {SILVER}]↵[/] [{DIM}]continue[/]  [{DIM}]·[/]  [bold {SILVER}]esc[/] [{DIM}]skip[/]"
_FOOT_BUSY  = f"  [{DIM}]installing…[/]"


class CompletionSetupScreen(ModalScreen[bool]):
    """Asks the user whether to install shell tab completion.

    Dismisses with True if the user installed, False if they skipped.
    Auto-dismisses with False if the shell cannot be detected.
    """

    CSS = f"""
    CompletionSetupScreen {{
        align: center middle;
        background: #000000 40%;
    }}
    #comp-panel {{
        width: 64;
        height: auto;
        background: #0d0d0d;
        border: round #3a3a3a;
    }}
    #comp-title {{
        height: auto;
        padding: 0 2;
        background: #0d0d0d;
        border-bottom: solid #2e2e2e;
    }}
    #comp-body {{
        height: auto;
        padding: 1 2;
    }}
    #comp-foot {{
        height: 2;
        padding: 0 2;
        background: #0d0d0d;
        color: {DIM};
        border-top: solid #2e2e2e;
    }}
    """

    BINDINGS = [
        Binding("escape", "skip",    show=False, priority=True),
        Binding("n",      "skip",    show=False),
        Binding("enter",  "confirm", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._shell: str = ""
        self._state: str = "idle"   # idle | installing | done_ok | done_err

    def compose(self) -> ComposeResult:
        with Vertical(id="comp-panel"):
            yield Static(
                f"[{DIM}]┌─[/] [bold]/setup[/] [{DIM}]— one last thing[/]",
                id="comp-title",
            )
            yield Static("", id="comp-body")
            yield Static(_FOOT_IDLE, id="comp-foot")

    def on_mount(self) -> None:
        try:
            import shellingham
            shell_name, _ = shellingham.detect_shell()
            self._shell = shell_name
        except Exception:
            self.dismiss(False)
            return
        self._render_idle()

    # ── Render helpers ────────────────────────────────────────────────────────

    def _render_idle(self) -> None:
        self.query_one("#comp-body", Static).update(
            f"[{DIM}]shell tab completion[/]\n\n"
            f"  Tab-complete [bold]minion[/bold] commands and slash commands.\n"
            f"  Detected shell:  [{SILVER}]{self._shell}[/]\n\n"
            f"  Install tab completion?"
        )
        self.query_one("#comp-foot", Static).update(_FOOT_IDLE)

    def _render_installing(self) -> None:
        self.query_one("#comp-body", Static).update(
            f"[{DIM}]shell tab completion[/]\n\n"
            f"  [{GOLD}]● installing…[/]"
        )
        self.query_one("#comp-foot", Static).update(_FOOT_BUSY)

    def _render_done_ok(self, comp_path: str) -> None:
        self.query_one("#comp-body", Static).update(
            f"[{DIM}]shell tab completion[/]\n\n"
            f"  [{GREEN}]✓ installed[/]  [{DIM}]→ {comp_path}[/]\n\n"
            f"  [{DIM}]Restart your terminal to activate.[/]"
        )
        self.query_one("#comp-foot", Static).update(_FOOT_DONE)

    def _render_done_err(self) -> None:
        self.query_one("#comp-body", Static).update(
            f"[{DIM}]shell tab completion[/]\n\n"
            f"  [red]Install failed.[/]\n"
            f"  [{DIM}]Run: [bold]minion --install-completion {self._shell}[/][/]"
        )
        self.query_one("#comp-foot", Static).update(_FOOT_DONE)

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_confirm(self) -> None:
        if self._state == "idle":
            self._state = "installing"
            self._render_installing()
            self.run_worker(self._do_install(), exclusive=True)
        elif self._state in ("done_ok", "done_err"):
            self.dismiss(self._state == "done_ok")

    def action_skip(self) -> None:
        if self._state != "installing":
            self.dismiss(False)

    # ── Worker ────────────────────────────────────────────────────────────────

    async def _do_install(self) -> None:
        shell = self._shell
        try:
            def _install():
                from typer._completion_shared import install as _ti
                _, comp_path = _ti(shell=shell, prog_name="minion")
                return str(comp_path)
            comp_path = await asyncio.to_thread(_install)
            self._state = "done_ok"
            self._render_done_ok(comp_path)
        except Exception:
            self._state = "done_err"
            self._render_done_err()
