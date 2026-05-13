"""ModelConfigScreen — 3-step /model wizard.

Step 1: Provider picker  (↑↓ navigate, click, Enter select)
Step 2: Model picker     (↑↓ navigate, Enter select / Shift+Tab back)
Step 3: API key entry    (paste, Enter validates live, Shift+Tab back)

All three steps live in a single ModalScreen; the frame, title rail, and
"currently using" strip stay fixed while only #wizard-body is swapped.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from ...config.model_catalog import PROVIDERS, fmt_ctx, fmt_price, has_key
from ..theme import DIM, GOLD, SILVER
from .base import (
    WIZARD_CSS,
    build_currently_using,
    build_footer_markup,
    build_title_bar,
)

_KeySub = Literal["empty", "typing", "validating", "success", "error"]

# ── Provider card CSS ─────────────────────────────────────────────────────────

_CARD_CSS = f"""
ProviderCard {{
    border: round #3a3a3a;
    padding: 0 2;
    margin: 0 0 1 0;
    height: auto;
    width: 40%;
}}
ProviderCard.card-focused {{
    border: round {GOLD};
    background: #1a1200;
}}
.card-content-row {{
    height: auto;
}}
.card-badge {{
    width: 7;
    height: 3;
    text-align: center;
}}
.card-right {{
    height: auto;
    padding: 0 0 0 2;
}}
.card-name {{
    height: 1;
    width: auto;
}}
.card-tagline {{
    height: 1;
    color: #888888;
    margin-top: 1;
}}
.card-meta {{
    height: 1;
}}
"""


# ── ProviderCard widget ───────────────────────────────────────────────────────

class ProviderCard(Widget):
    """Focusable, clickable provider selection card for Step 1."""

    can_focus = True

    class Selected(Message):
        def __init__(self, idx: int) -> None:
            super().__init__()
            self.idx = idx

    def __init__(self, provider: dict, idx: int, is_active: bool) -> None:
        super().__init__(id=f"provider-card-{idx}")
        self._provider  = provider
        self._idx       = idx
        self._is_active = is_active

    def compose(self) -> ComposeResult:
        p     = self._provider
        color = p.get("color", SILVER)
        name_markup = f"[bold {color}]{p['name']}[/]"
        if self._is_active:
            name_markup += f"  [{DIM} on #252525] active [/]"
        n        = len(p["models"])
        key_part = (
            f"[#44C76A]✓ key on file[/]"
            if has_key(p["id"])
            else f"[#FF8C00]● needs key[/]"
        )
        with Horizontal(classes="card-content-row"):
            yield Static(f"[bold {color}]{p['mark']}[/]", classes="card-badge")
            with Vertical(classes="card-right"):
                yield Static(name_markup, classes="card-name")
                yield Static(p["tagline"], classes="card-tagline")
                yield Static(f"[{DIM}]{n} models  ·  [/]{key_part}", classes="card-meta")

    def on_mount(self) -> None:
        color = self._provider.get("color", SILVER)
        self.query_one(".card-badge").styles.border = ("solid", color)

    def on_click(self) -> None:
        self.post_message(ProviderCard.Selected(self._idx))


# ── ModelConfigScreen ─────────────────────────────────────────────────────────

class ModelConfigScreen(ModalScreen):  # type: ignore[type-arg]
    """Full 3-step /model wizard modal."""

    CSS = WIZARD_CSS + _CARD_CSS

    BINDINGS = [
        # priority=True fires BEFORE any child widget can consume the key.
        Binding("escape",    "cancel",   show=False, priority=True),
        Binding("up",        "nav_up",   show=False, priority=True),
        Binding("down",      "nav_down", show=False, priority=True),
        Binding("shift+tab", "back",     show=False, priority=True),
        # enter: normal priority — Input in Step 3 handles it first via
        # on_input_submitted; in Steps 1&2 it bubbles up to action_confirm.
        Binding("enter",     "confirm",  show=False),
    ]

    def __init__(self, provider: str, model_id: str) -> None:
        super().__init__()
        self._orig_provider = provider
        self._orig_model    = model_id

        self._provider_idx: int = next(
            (i for i, p in enumerate(PROVIDERS) if p["id"] == provider), 0
        )
        sel = PROVIDERS[self._provider_idx]
        self._model_idx: int = next(
            (i for i, m in enumerate(sel["models"]) if m["id"] == model_id), 0
        )

        self._step: int          = 1
        self._key_sub: _KeySub   = "empty"
        self._key_error: str     = ""
        self._typed_key: str     = ""
        self._validated_key: str = ""

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        p = PROVIDERS[self._provider_idx]
        m = p["models"][self._model_idx]
        with Vertical(id="wizard-panel"):
            yield Static(build_title_bar(1), id="wizard-title")
            yield Static(build_currently_using(p, m), id="currently-using")
            with VerticalScroll(id="wizard-body"):
                pass  # populated in on_mount
            yield Static(build_footer_markup(1), id="wizard-foot")

    async def on_mount(self) -> None:
        self.query_one("#wizard-body", VerticalScroll).can_focus = False
        await self._go_to_step(1)

    # ── Step transitions ──────────────────────────────────────────────────────

    async def _go_to_step(self, step: int) -> None:
        self._step = step
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()

        if step == 1:
            await body.mount(Static(self._build_step1_heading(), id="step1-heading"))
            for i, p in enumerate(PROVIDERS):
                await body.mount(ProviderCard(p, i, p["id"] == self._orig_provider))
            self._update_card_focus()
            self.query_one(f"#provider-card-{self._provider_idx}", ProviderCard).focus(scroll_visible=False)

        elif step == 2:
            await body.mount(Static(self._build_step2(), id="step-content"))

        elif step == 3:
            await body.mount(Static(self._build_step3_top(), id="step3-top"))
            await body.mount(Input(
                password=True,
                id="key-input",
                placeholder="paste API key…",
            ))
            await body.mount(Static(self._key_status_markup(), id="key-status"))
            self.query_one("#key-input", Input).focus()

        self.query_one("#wizard-title", Static).update(build_title_bar(step))
        self.query_one("#wizard-foot", Static).update(build_footer_markup(step))

    def _update_card_focus(self) -> None:
        """Add .card-focused to the selected card, remove from all others."""
        for i in range(len(PROVIDERS)):
            try:
                card = self.query_one(f"#provider-card-{i}", ProviderCard)
                card.set_class(i == self._provider_idx, "card-focused")
            except Exception:
                pass

    def _refresh_body(self) -> None:
        """Redraw step 2 content; step 1 is handled by .card-focused class."""
        if self._step == 2:
            self.query_one("#step-content", Static).update(self._build_step2())

    # ── Step 1 — Provider picker ──────────────────────────────────────────────

    def _build_step1_heading(self) -> Text:
        out = Text()
        out.append("Bello! Who should do the ", style="bold")
        out.append("thinking", style=f"bold {GOLD}")
        out.append("?\n", style="bold")
        out.append("\n")
        out.append("Pick a provider.\n", style=SILVER)
        out.append("\n")
        out.append("    ")
        out.append("✓", style="#44C76A")
        out.append(" means we already have a key on file — no setup needed.\n", style=DIM)
        out.append("    ")
        out.append("●", style="#FF8C00")
        out.append(" means you'll be asked to paste one.\n", style=DIM)
        out.append("\n")
        return out

    def on_provider_card_selected(self, message: ProviderCard.Selected) -> None:
        if message.idx != self._provider_idx:
            self._model_idx = 0
        self._provider_idx = message.idx
        self._update_card_focus()

    # ── Step 2 — Model picker ─────────────────────────────────────────────────

    _CARD_W = 60

    def _build_step2(self) -> str:
        w     = self._CARD_W
        p     = PROVIDERS[self._provider_idx]
        color = p.get("color", SILVER)
        lines: list[str] = [
            f"[bold]Great, [{color}]{p['name']}[/] it is. Which model?[/]",
            f"[{DIM}]Bigger isn't always better. Switch any time with [bold]/model[/].[/{DIM}]",
            "",
        ]
        for i, m in enumerate(p["models"]):
            focused   = i == self._model_idx
            ctx_str   = fmt_ctx(m["ctx"])
            price     = f"{fmt_price(m['in_price'])}/{fmt_price(m['out_price'])} per Mtok"
            bar_fg    = GOLD if focused else "#888888"
            bar_empty = "#2a2a2a"
            spd_bar   = f"[{bar_fg}]{'█' * m['speed']}[/][{bar_empty}]{'░' * (5 - m['speed'])}[/]"
            iq_bar    = f"[{bar_fg}]{'█' * m['intel']}[/][{bar_empty}]{'░' * (5 - m['intel'])}[/]"

            if focused:
                name = f"[bold {GOLD}]{m['id']}[/]"
                lines.append(f"[{GOLD}] ╭{'─' * w}╮[/]")
                lines.append(
                    f"[{GOLD}] │[/]  [bold {GOLD}]›[/]"
                    f"  {name}"
                    f"  [{DIM}]ctx {ctx_str}  ·  {price}[/]"
                )
                lines.append(
                    f"[{GOLD}] │[/]"
                    f"     [{DIM}]{m['tag']}[/]"
                    f"   [{DIM}]spd[/] {spd_bar}"
                    f"  [{DIM}]iq[/] {iq_bar}"
                )
                lines.append(f"[{GOLD}] ╰{'─' * w}╯[/]")
            else:
                name = f"[{SILVER}]{m['id']}[/]"
                lines.append(
                    f"     {name}"
                    f"  [{DIM}]ctx {ctx_str}  ·  {price}[/]"
                )
                lines.append(
                    f"     [{DIM}]{m['tag']}[/]"
                    f"   [{DIM}]spd[/] {spd_bar}"
                    f"  [{DIM}]iq[/] {iq_bar}"
                )
            lines.append("")
        return "\n".join(lines)

    # ── Step 3 — API key entry ────────────────────────────────────────────────

    def _build_step3_top(self) -> str:
        p      = PROVIDERS[self._provider_idx]
        m      = p["models"][self._model_idx]
        color  = p.get("color", SILVER)
        dashes = "─" * 52
        return "\n".join([
            f"[bold]One more thing — your [{color}]{p['name']}[/] key.[/]",
            f"[{DIM}]Paste it once. Stashed in [{SILVER}]~/.minion/.env[/{SILVER}] — you won't see this screen again.[/]",
            "",
            f"[{DIM}] ╭{dashes}╮[/]",
            (
                f"[{DIM}] │[/]  [{DIM}]switching to[/]"
                f"  [{color}]{p['name']}[/]"
                f"  [{DIM}]›[/]"
                f"  [bold {GOLD}]{m['id']}[/]"
                f"  [{DIM}]{p['key_env']}[/]"
            ),
            f"[{DIM}] ╰{dashes}╯[/]",
            "",
        ])

    def _key_status_markup(self) -> str:
        sub = self._key_sub
        if sub == "empty":
            return f"[{DIM}]Paste · won't be echoed · we'll test it before saving.[/{DIM}]"
        if sub == "typing":
            n = len(self._typed_key)
            return f"[{DIM}]{n} chars · keep going…[/{DIM}]"
        if sub == "validating":
            p = PROVIDERS[self._provider_idx]
            return f"[{GOLD}]● pinging {p['name']}…[/] [{DIM}]testing connection[/{DIM}]"
        if sub == "success":
            return f"[green]● valid · provider replied OK[/] [{DIM}]ready to save[/{DIM}]"
        if sub == "error":
            from rich.markup import escape as _esc
            return (
                f"[red]● {_esc(self._key_error)}[/]\n\n"
                f"[{DIM}]Backspace to edit · Enter to re-test.[/{DIM}]"
            )
        return ""

    def _refresh_key_status(self, sub: str = "") -> None:
        try:
            self.query_one("#key-status", Static).update(self._key_status_markup())
            foot_sub = sub or self._key_sub
            self.query_one("#wizard-foot", Static).update(
                build_footer_markup(3, sub=foot_sub if foot_sub in ("validating", "success", "error") else "")
            )
        except Exception:
            pass

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_nav_up(self) -> None:
        if self._step == 1:
            self._provider_idx = (self._provider_idx - 1) % len(PROVIDERS)
            self._model_idx    = 0
            self._update_card_focus()
        elif self._step == 2:
            self._model_idx = max(0, self._model_idx - 1)
            self._refresh_body()

    def action_nav_down(self) -> None:
        if self._step == 1:
            self._provider_idx = (self._provider_idx + 1) % len(PROVIDERS)
            self._model_idx    = 0
            self._update_card_focus()
        elif self._step == 2:
            models = PROVIDERS[self._provider_idx]["models"]
            self._model_idx = min(len(models) - 1, self._model_idx + 1)
            self._refresh_body()

    async def action_back(self) -> None:
        if self._step == 2:
            await self._go_to_step(1)
        elif self._step == 3:
            self._key_sub       = "empty"
            self._typed_key     = ""
            self._validated_key = ""
            await self._go_to_step(2)

    async def action_confirm(self) -> None:
        if self._step == 1:
            await self._go_to_step(2)
        elif self._step == 2:
            p = PROVIDERS[self._provider_idx]
            if p["id"] != self._orig_provider and not has_key(p["id"]):
                await self._go_to_step(3)
            else:
                self.action_save()
        elif self._step == 3:
            if self._key_sub == "success":
                self.action_save()
            elif self._typed_key and self._key_sub != "validating":
                self._run_validation()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "key-input":
            return
        self._typed_key = event.value
        self._key_sub   = "typing" if event.value else "empty"
        self._refresh_key_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "key-input":
            return
        if self._key_sub == "success":
            self.action_save()
        elif self._typed_key:
            self._run_validation()

    # ── Async key validation ──────────────────────────────────────────────────

    def _run_validation(self) -> None:
        self.run_worker(self._validate_worker(), exclusive=True)

    async def _validate_worker(self) -> None:
        from ...llm.validate import test_connection

        self._key_sub = "validating"
        self._refresh_key_status(sub="validating")

        p      = PROVIDERS[self._provider_idx]
        m      = p["models"][self._model_idx]
        ok, msg = await asyncio.to_thread(
            test_connection, p["id"], self._typed_key, m["id"]
        )

        if ok:
            self._key_sub       = "success"
            self._validated_key = self._typed_key
        else:
            self._key_sub   = "error"
            self._key_error = msg
        self._refresh_key_status()

    def action_cancel(self) -> None:
        self.dismiss({})

    def action_save(self) -> None:
        from ...config.interactive import update_env_values

        p = PROVIDERS[self._provider_idx]
        m = p["models"][self._model_idx]

        updates: dict[str, str] = {}
        if p["id"] != self._orig_provider:
            updates["MINION_PROVIDER"] = p["id"]
        if m["id"] != self._orig_model:
            updates["MINION_MODEL"] = m["id"]
        if self._validated_key:
            updates[p["key_env"]] = self._validated_key

        if updates:
            update_env_values(updates)

        self.dismiss(updates)
