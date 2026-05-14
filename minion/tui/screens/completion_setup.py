"""Shell tab-completion setup modal — shown automatically on first run."""
from __future__ import annotations

import asyncio
import datetime

from rich.console import Group as RichGroup
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from ..theme import BLUE, DIM, GOLD, GREEN, SILVER


# ── Shell helpers ──────────────────────────────────────────────────────────────

def _rc_file(shell: str) -> str:
    return {
        "zsh":  "~/.zshrc",
        "bash": "~/.bashrc",
        "fish": "~/.config/fish/config.fish",
    }.get(shell, f"~/.{shell}rc")


# ── Rich renderables ───────────────────────────────────────────────────────────

def _title_table(shell: str) -> Table:
    table = Table.grid(expand=True, padding=0)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, justify="right")
    left = Text.from_markup(
        f"[{DIM}]┌─[/] [bold]/setup[/] [{DIM}]— shell tab completion[/]"
    )
    right = Text.from_markup(
        f"[bold {BLUE} on #1e1e1e] {shell} [/][{DIM} on #1e1e1e]detected [/]  [{DIM}]┐[/]"
    ) if shell else Text.from_markup(f"[{DIM}]┐[/]")
    table.add_row(left, right)
    return table


def _hero() -> Text:
    """Headline + subtitle block."""
    return Text.assemble(
        Text.from_markup("[bold]Let me finish your sentences?[/bold]\n\n"),
        Text.from_markup(
            f"[{DIM}]Tab-completes [/][bold {GOLD}]minion[/] [{DIM}]commands and slash commands "
            f"when you press [/][{DIM} on #252525] tab [/] [{DIM}]in your shell.[/]"
        ),
    )


# ── Footer markup ──────────────────────────────────────────────────────────────

_FOOT_IDLE = (
    f"  [bold {SILVER}]↵[/] [{DIM}]install[/]  [{DIM}]·[/]  "
    f"[bold {SILVER}]esc[/] [{DIM}]skip for now[/]"
)
_FOOT_BUSY = f"  [{DIM}]… installing[/]"
_FOOT_DONE = (
    f"  [bold {SILVER}]↵[/] [{DIM}]continue[/]  [{DIM}]·[/]  "
    f"[bold {SILVER}]esc[/] [{DIM}]dismiss[/]"
)


# ── Screen ─────────────────────────────────────────────────────────────────────

class CompletionSetupScreen(ModalScreen[bool]):
    """Asks the user whether to install shell tab completion.

    Dismisses with True if installed, False if skipped.
    Auto-dismisses with False if the shell cannot be detected.
    """

    CSS = f"""
    CompletionSetupScreen {{
        align: center middle;
        background: #000000 40%;
    }}
    #comp-panel {{
        width: 110;
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
    #comp-hero {{
        height: auto;
        padding: 1 2;
    }}
    #comp-code-card {{
        height: auto;
        margin: 0 2 1 2;
        padding: 1 2;
        border: solid #2a2a2a;
        background: #0f0f0f;
    }}
    #comp-code-label {{
        height: 1;
    }}
    #comp-code-content {{
        height: auto;
        margin-top: 1;
    }}
    #comp-disc {{
        height: auto;
        padding: 0 2 1 2;
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
            yield Static("", id="comp-title")
            yield Static("", id="comp-hero")
            with Vertical(id="comp-code-card"):
                yield Static("", id="comp-code-label")
                yield Static("", id="comp-code-content")
            yield Static("", id="comp-disc")
            yield Static(_FOOT_IDLE, id="comp-foot")

    def on_mount(self) -> None:
        try:
            import shellingham
            shell_name, _ = shellingham.detect_shell()
            self._shell = shell_name
        except Exception:
            self.dismiss(False)
            return
        self.query_one("#comp-title", Static).update(_title_table(self._shell))
        self._render_idle()

    # ── Render helpers ─────────────────────────────────────────────────────────

    def _render_idle(self) -> None:
        today = datetime.date.today().strftime("%Y%m%d")
        rc    = _rc_file(self._shell)

        self.query_one("#comp-hero", Static).update(_hero())

        self.query_one("#comp-code-card").display = True
        self.query_one("#comp-code-label", Static).update(
            Text.from_markup(f"[{DIM}]WILL ADD TO  [/][bold {BLUE}]{rc}[/]")
        )
        self.query_one("#comp-code-content", Static).update(
            Text.assemble(
                Text.from_markup(f"[{DIM}]# >>> minion completion >>>[/]"),
                "\n",
                Text.from_markup(
                    f"[{GREEN}]+ source <(minion --print-completion {self._shell})[/]"
                ),
                "\n",
                Text.from_markup(f"[{DIM}]# <<< minion completion <<<[/]"),
            )
        )

        self.query_one("#comp-disc").display = True
        self.query_one("#comp-disc", Static).update(
            Text.from_markup(
                f"[{DIM}]Two lines, easy to remove later. We'll back up your existing "
                f"{rc} to [bold {BLUE}]{rc}.bak.{today}[/] first.[/]"
            )
        )
        self.query_one("#comp-foot", Static).update(_FOOT_IDLE)

    def _render_installing(self) -> None:
        today = datetime.date.today().strftime("%Y%m%d")
        rc    = _rc_file(self._shell)
        self.query_one("#comp-hero", Static).update(
            Text.from_markup(
                f"[bold {GOLD}]● installing…[/]  "
                f"[{DIM}]backup → {rc}.bak.{today}  ·  writing 3 lines[/]"
            )
        )
        self.query_one("#comp-code-card").display = False
        self.query_one("#comp-disc").display = False
        self.query_one("#comp-foot", Static).update(_FOOT_BUSY)

    def _render_done_ok(self, comp_path: str) -> None:
        rc = _rc_file(self._shell)
        self.query_one("#comp-hero", Static).update(
            RichGroup(
                Text.from_markup(
                    f"[bold {GREEN}]✓ installed[/]  [{DIM}]·[/]  "
                    f"[{DIM}]restart your shell or run [/][bold {BLUE}]source {rc}[/]"
                ),
                "",
                Text.from_markup(
                    f"[{DIM}]Remove any time: [bold]minion --install-completion {self._shell}[/][/]"
                ),
            )
        )
        self.query_one("#comp-code-card").display = False
        self.query_one("#comp-disc").display = False
        self.query_one("#comp-foot", Static).update(_FOOT_DONE)

    def _render_done_err(self) -> None:
        self.query_one("#comp-hero", Static).update(
            Text.from_markup(
                f"[red]Install failed.[/]\n"
                f"[{DIM}]Run manually: [bold]minion --install-completion {self._shell}[/][/]"
            )
        )
        self.query_one("#comp-code-card").display = False
        self.query_one("#comp-disc").display = False
        self.query_one("#comp-foot", Static).update(_FOOT_DONE)

    # ── Actions ────────────────────────────────────────────────────────────────

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

    # ── Worker ─────────────────────────────────────────────────────────────────

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
