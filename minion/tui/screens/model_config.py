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

from rich.style import Style as _RichStyle
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

_KeySub = Literal["empty", "typing", "validating", "success", "error", "confirm-skip"]

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
ModelCard {{
    border: round #3a3a3a;
    padding: 0 1;
    margin: 0 0 1 0;
    height: auto;
    width: 40%;
}}
ModelCard.model-focused {{
    border: round {GOLD};
    background: #1a1200;
}}
.model-content-row {{
    height: auto;
}}
.model-left {{
    height: auto;
    width: 1fr;
}}
.model-right {{
    height: auto;
    width: auto;
    padding: 0 0 0 2;
}}
.model-name-row {{
    height: 1;
}}
.model-tag-row {{
    height: 1;
    color: #888888;
    margin-top: 1;
}}
.model-bar-row {{
    height: 1;
}}

/* ── Step 3 ─────────────────────────────────────────────────────────── */
#step3-wrapper {{
    width: 65%;
    height: auto;
}}
#step3-heading {{
    height: auto;
    margin: 0 0 1 0;
}}
SwitchingToCard {{
    border: solid #3a3a3a;
    padding: 1 2;
    margin: 0 0 1 0;
    height: auto;
    width: 100%;
}}
#switching-inner {{
    height: auto;
}}
.switching-row1 {{
    height: 1;
}}
.switch-prefix {{
    width: auto;
    height: 1;
}}
.switching-right {{
    width: 1fr;
    height: 1;
    content-align: right middle;
}}

#key-label {{
    height: 1;
    margin: 1 0 0 0;
}}
#key-input {{
    margin-bottom: 0;
}}
#key-status {{
    height: auto;
    margin: 0;
}}
KeyScopeRow {{
    height: auto;
    margin: 1 0 0 0;
}}
KeyScopeRow.section-focused #scope-header {{
    color: {GOLD};
}}
#scope-header {{
    height: 1;
    margin: 0;
}}
#scope-options {{
    height: auto;
}}
.scope-btn {{
    height: auto;
    width: 1fr;
    padding: 1 2;
    margin-right: 1;
    border: solid #2a2a2a;
    background: #0f0f0f;
}}
.scope-selected {{
    border: solid {GOLD};
    background: #1a1200;
}}
#scope-project {{
    margin-right: 0;
}}
TestConnectionRow {{
    height: auto;
    padding: 1 2;
    margin: 1 0 0 0;
    border: solid #2a2a2a;
    background: #0f0f0f;
}}
TestConnectionRow.section-focused {{
    border: solid {GOLD};
    background: #1a1200;
}}
#test-conn-inner {{
    height: auto;
}}
.test-label {{
    height: 1;
    width: 1fr;
}}
.test-btn {{
    height: 1;
    width: auto;
    padding: 0 1;
    margin-left: 1;
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


# ── ModelCard widget ──────────────────────────────────────────────────────────

class ModelCard(Widget):
    """Focusable, clickable model selection card for Step 2."""

    can_focus = False  # navigation managed by screen priority bindings

    class Selected(Message):
        def __init__(self, idx: int) -> None:
            super().__init__()
            self.idx = idx

    def __init__(self, model: dict, provider: dict, idx: int, is_focused: bool) -> None:
        super().__init__(id=f"model-card-{idx}")
        self._model     = model
        self._provider  = provider
        self._idx       = idx
        self._is_focused = is_focused

    def _name_row_markup(self) -> str:
        m         = self._model
        ctx       = fmt_ctx(m["ctx"])
        name_part = (
            f"[bold {GOLD}]{m['id']}[/]" if self._is_focused
            else f"[{SILVER}]{m['id']}[/]"
        )
        ctx_tag   = f"[{DIM} on #1c1c1c] ctx {ctx} [/]"
        price_tag = f"[{DIM} on #1c1c1c] {fmt_price(m['in_price'])} / {fmt_price(m['out_price'])} per Mtok [/]"
        return f"{name_part}  {ctx_tag}  {price_tag}"

    def _bar_markup(self, label: str, value: int) -> str:
        bar_fg    = GOLD if self._is_focused else "#888888"
        bar_empty = "#2a2a2a"
        filled    = f"[{bar_fg}]{'█' * value}[/]"
        empty     = f"[{bar_empty}]{'░' * (5 - value)}[/]"
        return f"[{DIM}]{label}[/] {filled}{empty}"

    def compose(self) -> ComposeResult:
        m = self._model
        with Horizontal(classes="model-content-row"):
            with Vertical(classes="model-left"):
                yield Static(self._name_row_markup(), classes="model-name-row")
                yield Static(f"[{DIM}]{m['tag']}[/]", classes="model-tag-row")
            with Vertical(classes="model-right"):
                yield Static(self._bar_markup("SPD", m["speed"]), classes="model-bar-row")
                yield Static(self._bar_markup("IQ ", m["intel"]), classes="model-bar-row")

    def set_focused(self, focused: bool) -> None:
        self._is_focused = focused
        self.set_class(focused, "model-focused")
        m = self._model
        name_statics = self.query(".model-name-row")
        if name_statics:
            name_statics.first(Static).update(self._name_row_markup())
        bar_statics = self.query(".model-bar-row")
        bars = list(bar_statics)
        if len(bars) >= 2:
            bars[0].update(self._bar_markup("SPD", m["speed"]))  # type: ignore[arg-type]
            bars[1].update(self._bar_markup("IQ ", m["intel"]))  # type: ignore[arg-type]

    def on_click(self) -> None:
        self.post_message(ModelCard.Selected(self._idx))


# ── Step 3 helper widgets ─────────────────────────────────────────────────────


class SwitchingToCard(Widget):
    """Step 3 recap card — shows provider › model, docs link, and API key input."""

    def __init__(self, provider: dict, model: dict) -> None:
        super().__init__(id="switching-to-card")
        self._provider = provider
        self._model    = model

    def compose(self) -> ComposeResult:
        p     = self._provider
        m     = self._model
        color = p.get("color", SILVER)
        badge = f"[bold {color} on #1e1900]  {p['mark']}  [/]"
        with Vertical(id="switching-inner"):
            with Horizontal(classes="switching-row1"):
                yield Static(
                    f"[{DIM}]SWITCHING TO[/]  {badge}  "
                    f"[bold {color}]{p['name']}[/]  [{DIM}]›[/]  [bold {GOLD}]{m['id']}[/]",
                    classes="switch-prefix",
                )
                link_text = Text("Where do I get one? ›",
                                 style=_RichStyle(color=color, underline=True, link=p['docs_url']))
                yield Static(link_text, classes="switching-right")
            yield Static(f"[{DIM}]API KEY  ·  {p['key_env']}[/]", id="key-label")
            yield Input(
                password=True,
                id="key-input",
                placeholder=f"{p.get('key_prefix', 'sk-')}…",
            )
            yield Static("", id="key-status")


class _TestBtn(Static):
    """Internal clickable label for TestConnectionRow."""

    def __init__(self, validate: bool, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._validate = validate

    def on_click(self) -> None:
        self.post_message(TestConnectionRow.Toggled(self._validate))


class TestConnectionRow(Widget):
    """Row with yes/no toggle — test the connection before saving."""

    can_focus = True

    class Toggled(Message):
        def __init__(self, validate: bool) -> None:
            super().__init__()
            self.validate = validate

    def __init__(self) -> None:
        super().__init__(id="test-conn-row")
        self._validate: bool = True

    @property
    def value(self) -> bool:
        return self._validate

    def toggle(self) -> None:
        self._validate = not self._validate
        self._refresh_buttons()

    def compose(self) -> ComposeResult:
        with Horizontal(id="test-conn-inner"):
            yield Static(
                f"[green]●[/] [bold]Test connection before saving[/] [{DIM}](recommended)[/]",
                classes="test-label",
            )
            yield _TestBtn(True,  classes="test-btn", id="test-yes")
            yield _TestBtn(False, classes="test-btn", id="test-no")

    def on_mount(self) -> None:
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        try:
            yes = self.query_one("#test-yes", _TestBtn)
            no  = self.query_one("#test-no",  _TestBtn)
            if self._validate:
                yes.update(f"[bold {GOLD}]◉  yes[/]")
                no.update(f"[{DIM}]○  no[/]")
            else:
                yes.update(f"[{DIM}]○  yes[/]")
                no.update(f"[bold {GOLD}]◉  no[/]")
        except Exception:
            pass

    def on_test_connection_row_toggled(self, message: "TestConnectionRow.Toggled") -> None:
        self._validate = message.validate
        self._refresh_buttons()


class _ScopeBtn(Static):
    """Clickable scope option inside KeyScopeRow."""

    def __init__(self, scope: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._scope = scope

    def on_click(self) -> None:
        self.post_message(KeyScopeRow.ScopeChanged(self._scope))


class KeyScopeRow(Widget):
    """Two-card selector: save key globally (~/.minion/.env) or just this project."""

    can_focus = True

    class ScopeChanged(Message):
        def __init__(self, scope: str) -> None:
            super().__init__()
            self.scope = scope

    _LABELS: dict[str, tuple[str, str]] = {
        "global":  ("All projects",  "~/.minion/.env"),
        "project": ("This project",  ".minion/.env"),
    }

    def __init__(self) -> None:
        super().__init__(id="key-scope-row")
        self._scope = "global"

    def compose(self) -> ComposeResult:
        yield Static(f"[{DIM}]SCOPE[/]", id="scope-header")
        with Horizontal(id="scope-options"):
            yield _ScopeBtn("global",  classes="scope-btn", id="scope-global")
            yield _ScopeBtn("project", classes="scope-btn", id="scope-project")

    def on_mount(self) -> None:
        self._refresh_options()

    def _refresh_options(self) -> None:
        for key, (label, path) in self._LABELS.items():
            try:
                btn = self.query_one(f"#scope-{key}", _ScopeBtn)
                if key == self._scope:
                    btn.update(f"[bold {GOLD}]●  {label}[/]\n[{DIM}]{path}[/]")
                    btn.set_class(True, "scope-selected")
                else:
                    btn.update(f"[{DIM}]○  {label}\n{path}[/]")
                    btn.set_class(False, "scope-selected")
            except Exception:
                pass

    def on_key_scope_row_scope_changed(self, message: "KeyScopeRow.ScopeChanged") -> None:
        self._scope = message.scope
        self._refresh_options()



# ── ModelConfigScreen ─────────────────────────────────────────────────────────

class ModelConfigScreen(ModalScreen):  # type: ignore[type-arg]
    """Full 3-step /model wizard modal."""

    CSS = WIZARD_CSS + _CARD_CSS

    BINDINGS = [
        Binding("escape", "cancel",    show=False, priority=True),
        Binding("up",     "nav_up",    show=False, priority=True),
        Binding("down",   "nav_down",  show=False, priority=True),
        # left/right: NOT priority so Input cursor movement is preserved;
        # fires when a non-input section (scope/test) has focus.
        Binding("left",   "nav_left",  show=False),
        Binding("right",  "nav_right", show=False),
        # enter: normal priority — Input in Step 3 handles it first via
        # on_input_submitted; in Steps 1&2 it bubbles up to action_confirm.
        Binding("enter",  "confirm",   show=False),
        # Tab / Shift+Tab handled in on_key for full-wizard navigation.
    ]

    def __init__(self, provider: str, model_id: str, *, first_run: bool = False) -> None:
        super().__init__()
        self._orig_provider = provider
        self._orig_model    = model_id
        self._first_run     = first_run

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
        self._key_scope: str     = "project"
        self._step3_focus: str   = "input"   # "input" | "scope" | "test"

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        p = PROVIDERS[self._provider_idx]
        m = p["models"][self._model_idx]
        with Vertical(id="wizard-panel"):
            yield Static(build_title_bar(1), id="wizard-title")
            if not self._first_run:
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
            p     = PROVIDERS[self._provider_idx]
            color = p.get("color", SILVER)
            heading = (
                f"[bold]Great, [{color}]{p['name']}[/] it is. Which model?[/]\n"
                f"[{DIM}]Bigger isn't always better — switch any time with [bold]/model[/].[/]\n"
            )
            await body.mount(Static(heading, id="step2-heading"))
            for i, m in enumerate(p["models"]):
                await body.mount(ModelCard(m, p, i, i == self._model_idx))
            self._update_model_focus()

        elif step == 3:
            self._key_scope   = "global"
            self._step3_focus = "scope"
            p     = PROVIDERS[self._provider_idx]
            m     = p["models"][self._model_idx]
            color = p.get("color", SILVER)
            wrapper = Vertical(id="step3-wrapper")
            await body.mount(wrapper)
            await wrapper.mount(Static(
                f"[bold]One more thing — your [{color}]{p['name']}[/] key.[/]\n"
                f"[{DIM}]Paste it once. We'll test it then stash it — you won't see this screen again.[/]",
                id="step3-heading",
            ))
            await wrapper.mount(KeyScopeRow())
            await wrapper.mount(TestConnectionRow())
            await wrapper.mount(SwitchingToCard(p, m))
            self._refresh_key_status()
            self._set_step3_focus("scope")

        self.query_one("#wizard-title", Static).update(build_title_bar(step))
        # Step 3 footer is owned by _set_step3_focus() / _refresh_step3_footer()
        # which already ran above — don't overwrite with the generic paste hint.
        if step != 3:
            self.query_one("#wizard-foot", Static).update(build_footer_markup(step))

    def _update_card_focus(self) -> None:
        """Add .card-focused to the selected card, remove from all others."""
        for i in range(len(PROVIDERS)):
            try:
                card = self.query_one(f"#provider-card-{i}", ProviderCard)
                card.set_class(i == self._provider_idx, "card-focused")
            except Exception:
                pass

    def _update_model_focus(self) -> None:
        """Update .model-focused class on all ModelCards for the current selection."""
        p = PROVIDERS[self._provider_idx]
        for i in range(len(p["models"])):
            try:
                card = self.query_one(f"#model-card-{i}", ModelCard)
                card.set_focused(i == self._model_idx)
            except Exception:
                pass

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

    # ── Step 3 — API key entry ────────────────────────────────────────────────

    def on_key_scope_row_scope_changed(self, message: KeyScopeRow.ScopeChanged) -> None:
        self._key_scope = message.scope

    def on_test_connection_row_toggled(self, _: TestConnectionRow.Toggled) -> None:
        if self._key_sub == "confirm-skip":
            self._key_sub = "typing"
        self._refresh_key_status()

    def _key_status_markup(self) -> str:
        sub = self._key_sub
        test_validate = True
        try:
            test_validate = self.query_one("#test-conn-row", TestConnectionRow).value
        except Exception:
            pass
        if sub == "empty":
            if not test_validate:
                return f"[{DIM}]Paste · won't be echoed · enter to save.[/{DIM}]"
            return f"[{DIM}]Paste · won't be echoed · we'll test it before saving.[/{DIM}]"
        if sub == "typing":
            n = len(self._typed_key)
            if not test_validate:
                return f"[{DIM}]{n} chars · enter to save without testing.[/{DIM}]"
            return f"[{DIM}]{n} chars · keep going…[/{DIM}]"
        if sub == "confirm-skip":
            return (
                f"[{GOLD}]● skipping test — are you sure?[/]  "
                f"[{DIM}]↵ again to save[/{DIM}]"
            )
        if sub == "validating":
            p = PROVIDERS[self._provider_idx]
            return f"[{GOLD}]● pinging {p['name']}…[/] [{DIM}]testing connection[/{DIM}]"
        if sub == "success":
            return f"[green]● valid · provider replied OK[/] [{DIM}]· enter to save[/{DIM}]"
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
        except Exception:
            pass
        self._refresh_step3_footer()

    # ── Key interception ─────────────────────────────────────────────────────

    async def on_key(self, event) -> None:
        # Intercept Tab/Shift+Tab before Textual's focus-cycling.
        # Tab = forward through the wizard; Shift+Tab = backward.
        if event.key not in ("tab", "shift+tab"):
            return
        event.prevent_default()
        event.stop()
        if event.key == "tab":
            await self._tab_forward()
        else:
            await self._tab_backward()

    async def _tab_forward(self) -> None:
        if self._step == 1:
            await self._go_to_step(2)
        elif self._step == 2:
            p = PROVIDERS[self._provider_idx]
            if not has_key(p["id"]):
                await self._go_to_step(3)
            else:
                self.action_save()
        elif self._step == 3:
            if self._step3_focus == "scope":
                self._set_step3_focus("test")
            elif self._step3_focus == "test":
                self._set_step3_focus("input")
            # at "input": Tab is a no-op (end of form)

    async def _tab_backward(self) -> None:
        if self._step == 1:
            pass  # already at the start
        elif self._step == 2:
            await self._go_to_step(1)
        elif self._step == 3:
            if self._step3_focus == "input":
                self._set_step3_focus("test")
            elif self._step3_focus == "test":
                self._set_step3_focus("scope")
            else:
                # focus is on scope — go back to step 2
                self._key_sub       = "empty"
                self._typed_key     = ""
                self._validated_key = ""
                await self._go_to_step(2)

    # ── Actions ───────────────────────────────────────────────────────────────

    def on_model_card_selected(self, message: ModelCard.Selected) -> None:
        self._model_idx = message.idx
        self._update_model_focus()

    def _set_step3_focus(self, section: str) -> None:
        self._step3_focus = section
        if section == "input":
            try:
                self.query_one("#key-input", Input).focus()
            except Exception:
                pass
        elif section == "scope":
            try:
                self.query_one("#key-scope-row", KeyScopeRow).focus()
            except Exception:
                pass
        elif section == "test":
            try:
                self.query_one("#test-conn-row", TestConnectionRow).focus()
            except Exception:
                pass
        # section highlight classes
        try:
            self.query_one("#key-scope-row", KeyScopeRow).set_class(
                section == "scope", "section-focused"
            )
        except Exception:
            pass
        try:
            self.query_one("#test-conn-row", TestConnectionRow).set_class(
                section == "test", "section-focused"
            )
        except Exception:
            pass
        self._refresh_step3_footer()

    def _refresh_step3_footer(self) -> None:
        if self._step != 3:
            return
        if self._step3_focus == "scope":
            sub = "scope"
        elif self._step3_focus == "test":
            sub = "test"
        elif self._key_sub in ("validating", "success", "error", "confirm-skip"):
            sub = self._key_sub
        else:
            sub = "input"
        try:
            self.query_one("#wizard-foot", Static).update(
                build_footer_markup(3, sub=sub)
            )
        except Exception:
            pass

    def action_nav_left(self) -> None:
        if self._step == 3:
            if self._step3_focus == "scope":
                try:
                    scope_row = self.query_one("#key-scope-row", KeyScopeRow)
                    new = "project" if scope_row._scope == "global" else "global"
                    scope_row._scope = new
                    scope_row._refresh_options()
                    self._key_scope = new
                except Exception:
                    pass
            elif self._step3_focus == "test":
                try:
                    self.query_one("#test-conn-row", TestConnectionRow).toggle()
                    self._refresh_key_status()
                except Exception:
                    pass

    def action_nav_right(self) -> None:
        # same as left — only 2 options in each section
        self.action_nav_left()

    def action_nav_up(self) -> None:
        if self._step == 1:
            self._provider_idx = (self._provider_idx - 1) % len(PROVIDERS)
            self._model_idx    = 0
            self._update_card_focus()
        elif self._step == 2:
            self._model_idx = max(0, self._model_idx - 1)
            self._update_model_focus()
        elif self._step == 3:
            # Vertical section order: scope → test → input (↑ = go up)
            if self._step3_focus == "test":
                self._set_step3_focus("scope")
            elif self._step3_focus == "input":
                self._set_step3_focus("test")

    def action_nav_down(self) -> None:
        if self._step == 1:
            self._provider_idx = (self._provider_idx + 1) % len(PROVIDERS)
            self._model_idx    = 0
            self._update_card_focus()
        elif self._step == 2:
            models = PROVIDERS[self._provider_idx]["models"]
            self._model_idx = min(len(models) - 1, self._model_idx + 1)
            self._update_model_focus()
        elif self._step == 3:
            # Vertical section order: scope → test → input (↓ = go down)
            if self._step3_focus == "scope":
                self._set_step3_focus("test")
            elif self._step3_focus == "test":
                self._set_step3_focus("input")

    async def action_confirm(self) -> None:
        if self._step == 1:
            await self._go_to_step(2)
        elif self._step == 2:
            p = PROVIDERS[self._provider_idx]
            if not has_key(p["id"]):
                await self._go_to_step(3)
            else:
                self.action_save()
        elif self._step == 3:
            await self._step3_finish()

    async def _step3_finish(self) -> None:
        """Shared finish logic: validate or save depending on toggle state."""
        if self._key_sub == "success":
            self.action_save()
        elif self._key_sub == "confirm-skip":
            self.action_save()
        elif self._typed_key and self._key_sub != "validating":
            should_validate = True
            try:
                row = self.query_one("#test-conn-row", TestConnectionRow)
                should_validate = row.value
            except Exception:
                pass
            if should_validate:
                self._run_validation()
            else:
                self._key_sub = "confirm-skip"
                self._refresh_key_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "key-input":
            return
        self._typed_key = event.value
        self._key_sub   = "typing" if event.value else "empty"
        self._refresh_key_status()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "key-input":
            return
        self.run_worker(self._step3_finish())

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
        from pathlib import Path
        from ...config.interactive import update_env_values

        p = PROVIDERS[self._provider_idx]
        m = p["models"][self._model_idx]

        updates: dict[str, str] = {}
        if p["id"] != self._orig_provider:
            updates["MINION_PROVIDER"] = p["id"]
        if m["id"] != self._orig_model:
            updates["MINION_MODEL"] = m["id"]
        key_to_save = self._validated_key or self._typed_key
        if key_to_save:
            updates[p["key_env"]] = key_to_save

        if updates:
            if self._key_scope == "global":
                target = Path.home() / ".minion" / ".env"
            else:
                target = Path(".minion") / ".env"
            update_env_values(updates, target=target)

        self.dismiss(updates)
