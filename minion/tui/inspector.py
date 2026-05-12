"""InspectorScreen — Textual ModalScreen for subagent transcript viewer.

Opened via ctrl+o; pushed as a modal overlay over the main screen.
Provides clickable agent tabs and a scrollable transcript styled to match
the main ConversationArea conventions.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from .agent_registry import SubagentRegistry, SubagentState
from .render import render_message_blocks
from .theme import GOLD, BLUE, GREEN, SILVER, DIM

_STATUS_ICON: dict[str, str] = {
    "pending":  "○",
    "running":  "●",
    "complete": "✓",
    "error":    "✗",
}
_STATUS_COLOR: dict[str, str] = {
    "pending":  DIM,
    "running":  SILVER,
    "complete": GREEN,
    "error":    "red",
}

# Maps prompt_toolkit class: names (from render_message_blocks) to Rich styles.
_CLASS_TO_RICH: dict[str, str] = {
    "minion-prefix":   f"bold {BLUE}",
    "conv-text":       "",
    "inspector-agent": f"bold #E8E8E8",
    "slot-detail":     DIM,
    "slot-done":       GREEN,
    "slot-error":      "bold red",
    "slot-running":    SILVER,
    "inspector-hint":  "#444444",
}


def _frags_to_text(frags: list[tuple[str, str]]) -> Text:
    t = Text()
    for style, chunk in frags:
        if style.startswith("class:"):
            style = _CLASS_TO_RICH.get(style[6:], "")
        t.append(chunk, style=style or "")
    return t


def _render_agent_transcript(state: SubagentState) -> list[Text]:
    """Convert a SubagentState's messages to Rich Text lines for mounting."""
    if not state.messages:
        if state.status in ("pending", "running"):
            return [Text("  ● running…", style=SILVER)]
        if state.status == "error":
            return [Text(f"  ✗  Error: {state.error or 'unknown'}", style="bold red")]
        return [Text("  (no transcript available)", style=DIM)]

    lines = render_message_blocks(state.messages, state.label, expanded=False)
    result: list[Text] = []
    for frags in lines:
        result.append(_frags_to_text(frags) if frags else Text(" "))

    if state.status == "error" and state.error:
        result.append(Text(f"  ✗  Error: {state.error}", style="bold red"))

    return result


# ── Widgets ───────────────────────────────────────────────────────────────────

class InspectorTab(Static):
    """A single clickable tab button in the inspector's tab bar."""

    class Selected(Message):
        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    def __init__(self, label: str, status: str, index: int, selected: bool) -> None:
        self._tab_label    = label
        self._tab_status   = status
        self._tab_index    = index
        self._tab_selected = selected
        # Pass rendered text upfront so the widget shows immediately on mount.
        super().__init__(self._make_text())

    def _make_text(self) -> Text:
        icon  = _STATUS_ICON.get(self._tab_status, "?")
        color = _STATUS_COLOR.get(self._tab_status, SILVER)
        t = Text()
        if self._tab_selected:
            t.append(f" {self._tab_label} ", style=f"bold {GOLD}")
            t.append(icon, style=f"bold {color}")
        else:
            t.append(f" {self._tab_label} ", style="#888888")
            t.append(icon, style=color)
        return t

    def on_mount(self) -> None:
        if self._tab_selected:
            self.add_class("selected")

    def _redraw(self) -> None:
        self.update(self._make_text())
        if self._tab_selected:
            self.add_class("selected")
        else:
            self.remove_class("selected")

    def on_click(self) -> None:
        self.post_message(InspectorTab.Selected(self._tab_index))


class TranscriptArea(VerticalScroll):
    """Scrollable area for a subagent's conversation transcript."""


# ── Modal Screen ──────────────────────────────────────────────────────────────

class InspectorScreen(ModalScreen):
    """Full-screen modal inspector for subagent transcripts.

    Pushed onto the screen stack by MinionApp when the user presses ctrl+o.
    Dismissed by ctrl+o, ctrl+w, or Escape.
    """

    CSS = """
    InspectorScreen {
        align: center middle;
        background: #000000 40%;
    }

    #inspector-panel {
        width: 90%;
        height: 88%;
        background: #0d0d0d;
        border: round #3a3a3a;
        border-title-align: left;
        border-title-color: #C0C0C0;
        border-title-style: bold;
    }

    #tab-bar {
        height: auto;
        min-height: 1;
        background: #141414;
        padding: 0 1;
        align: left middle;
    }

    InspectorTab {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-right: 1;
    }

    InspectorTab:hover {
        background: #1e1e1e;
    }

    InspectorTab.selected {
        background: #2a1f00;
    }

    #agent-info {
        height: 1;
        padding: 0 2;
        background: #0a0a0a;
    }

    TranscriptArea {
        height: 1fr;
        padding: 1 2;
        scrollbar-gutter: stable;
        scrollbar-size-vertical: 1;
        scrollbar-background: #111111;
        scrollbar-color: #2a2a2a;
        scrollbar-color-hover: #444444;
        scrollbar-color-active: #666666;
    }

    TranscriptArea > Static {
        height: auto;
        width: 1fr;
    }

    #inspector-footer {
        height: 1;
        padding: 0 2;
        background: #0a0a0a;
    }
    """

    BINDINGS = [
        Binding("escape", "close_inspector", "Close", show=False),
        Binding("ctrl+o", "close_inspector", "Close", show=False),
        Binding("left",   "prev_agent",      "Prev",  show=False),
        Binding("right",  "next_agent",      "Next",  show=False),
    ]

    def __init__(self, registry: SubagentRegistry) -> None:
        self._registry = registry
        self._sel_idx  = 0
        super().__init__()

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="inspector-panel"):
            with Horizontal(id="tab-bar"):
                for i, s in enumerate(self._registry.get_all()):
                    yield InspectorTab(s.label, s.status, i, i == self._sel_idx)
            yield Static(self._build_info(), id="agent-info")
            with TranscriptArea(id="transcript-area"):
                for line in self._transcript_lines():
                    yield Static(line)
            yield Static(self._build_footer(), id="inspector-footer")

    def on_mount(self) -> None:
        self.query_one("#inspector-panel").border_title = " Inspector "

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_close_inspector(self) -> None:
        self.dismiss()

    def action_prev_agent(self) -> None:
        n = len(self._registry.get_all())
        if n:
            self._sel_idx = (self._sel_idx - 1) % n
            self._rebuild()

    def action_next_agent(self) -> None:
        n = len(self._registry.get_all())
        if n:
            self._sel_idx = (self._sel_idx + 1) % n
            self._rebuild()

    # ── Message handlers ──────────────────────────────────────────────────────

    def on_inspector_tab_selected(self, message: InspectorTab.Selected) -> None:
        self._sel_idx = message.index
        self._rebuild()

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh_from_registry(self) -> None:
        """Called by MinionApp when registry state changes while screen is open."""
        states = self._registry.get_all()
        if states:
            self._sel_idx = min(self._sel_idx, len(states) - 1)
        self._rebuild()

    # ── Rendering helpers ─────────────────────────────────────────────────────

    def _selected_state(self) -> SubagentState | None:
        states = self._registry.get_all()
        if not states:
            return None
        return states[min(self._sel_idx, len(states) - 1)]

    def _build_info(self) -> Text:
        s = self._selected_state()
        if s is None:
            return Text()
        icon  = _STATUS_ICON.get(s.status, "?")
        color = _STATUS_COLOR.get(s.status, SILVER)
        t = Text()
        t.append(f" {s.label}", style=f"bold {BLUE}")
        t.append("  ")
        t.append(icon, style=f"bold {color}")
        t.append(f"  {s.status}", style=color)
        if s.latency_ms > 0:
            t.append(f"  ({s.latency_ms / 1000:.1f}s)", style=DIM)
        if s.task:
            task = s.task[:80] + "…" if len(s.task) > 80 else s.task
            t.append(f"  ·  {task}", style=DIM)
        return t

    def _build_footer(self) -> Text:
        states = self._registry.get_all()
        parts: list[str] = []
        if len(states) > 1:
            parts.append("←→ switch")
        parts += ["↑↓ scroll", "ctrl+o / esc  close"]
        t = Text()
        t.append("  ·  ".join(parts), style=DIM)
        return t

    def _transcript_lines(self) -> list[Text]:
        s = self._selected_state()
        if s is None:
            return [Text("  (no agents)", style=DIM)]
        return _render_agent_transcript(s)

    def _rebuild(self) -> None:
        """Rebuild tabs, info bar, and transcript for the current selection."""
        states = self._registry.get_all()

        tab_bar = self.query_one("#tab-bar", Horizontal)
        tab_bar.remove_children()
        for i, s in enumerate(states):
            tab_bar.mount(InspectorTab(s.label, s.status, i, i == self._sel_idx))

        self.query_one("#agent-info", Static).update(self._build_info())

        area = self.query_one("#transcript-area", TranscriptArea)
        area.remove_children()
        for line in self._transcript_lines():
            area.mount(Static(line))
        area.scroll_home(animate=False)

        self.query_one("#inspector-footer", Static).update(self._build_footer())
