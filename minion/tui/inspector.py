"""InspectorPanel — read-only subagent transcript viewer (Ctrl+O).

Renders a bordered box using Rich Text:

  ┌─ Inspector ────────────────────────────────────────────┐
  │   [coder ✓]    coder ●                                 │
  │                                                         │
  │   ── Turn 1 ──────────────────────────────────         │
  │      I'll create hello.py for you.                      │
  │      ⚙  write_file  path='hello.py'                     │
  │         ✓  Wrote 24 chars (1 lines) to 'hello.py'.     │
  │                                                         │
  │   ←/→ switch  ·  ↑↓ scroll  ·  ctrl+e expand  ·  ctrl+o close  │
  └──────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from rich.text import Text

from .agent_registry import SubagentRegistry, SubagentState
from .render import render_message_blocks

_STATUS_ICON = {"pending": "○", "running": "●", "complete": "✓", "error": "✗"}
_STATUS_STYLE_RICH = {
    "pending": "#C0C0C0",
    "running": "#C0C0C0",
    "complete": "#4CAF50",
    "error":    "red",
}

_DEFAULT_WIDTH  = 120
_DEFAULT_HEIGHT = 40


def _frags_len(frags: list[tuple[str, str]]) -> int:
    return sum(len(t) for _, t in frags)


def _frags_to_text(frags: list[tuple[str, str]]) -> Text:
    """Convert (class:name, text) fragment list to Rich Text."""
    t = Text()
    for style, chunk in frags:
        if style.startswith("class:"):
            style = _CLASS_TO_RICH.get(style[6:], "")
        t.append(chunk, style=style or "")
    return t


_CLASS_TO_RICH: dict[str, str] = {
    "slot-detail":       "#C0C0C0",
    "slot-done":         "#4CAF50",
    "slot-error":        "bold red",
    "slot-running":      "#C0C0C0",
    "inspector-title":   "bold",
    "inspector-tab-sel": "bold #FFD700 on #2a1f00",
    "inspector-tab":     "#555555",
    "inspector-hint":    "#444444",
    "inspector-agent":   "bold #E8E8E8",
}


class InspectorPanel:
    """Reads from SubagentRegistry; renders a bordered box as Rich Text."""

    def __init__(self, registry: SubagentRegistry) -> None:
        self._reg      = registry
        self._visible  = False
        self._sel_idx  = 0
        self._scroll   = 0
        self._expanded = False
        self._app_ref  = None   # set by MinionApp after construction

    def set_app(self, app) -> None:
        self._app_ref = app

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        return self._visible

    def toggle(self) -> None:
        self.close() if self._visible else self.open()

    def open(self) -> None:
        if not self._reg.get_all():
            return
        self._sel_idx = min(self._sel_idx, len(self._reg.get_all()) - 1)
        self._scroll  = 0
        self._visible = True

    def close(self) -> None:
        self._visible = False

    def move_agent(self, delta: int) -> None:
        states = self._reg.get_all()
        if not states:
            return
        self._sel_idx = (self._sel_idx + delta) % len(states)
        self._scroll  = 0

    def scroll(self, delta: int) -> None:
        self._scroll = max(0, self._scroll + delta)

    def toggle_expanded(self) -> None:
        self._expanded = not self._expanded

    def hint(self) -> str:
        states = self._reg.get_all()
        if not states:
            return ""
        label = states[min(self._sel_idx, len(states) - 1)].label
        parts = [f"Viewing [{label}]", "ctrl+o close"]
        if len(states) > 1:
            parts.append("←→ switch")
        return "  ·  ".join(parts)

    # ── Top-level render ─────────────────────────────────────────────────────

    def get_rich_text(self) -> Text:
        """Return a Rich Text object rendering the inspector box."""
        states = self._reg.get_all()
        if not states:
            self._visible = False
            return Text()

        if self._app_ref is not None:
            try:
                term_w = self._app_ref.size.width
                term_h = self._app_ref.size.height
            except Exception:
                term_w, term_h = _DEFAULT_WIDTH, _DEFAULT_HEIGHT
        else:
            term_w, term_h = _DEFAULT_WIDTH, _DEFAULT_HEIGHT

        idx      = min(self._sel_idx, len(states) - 1)
        selected = states[idx]

        box_w        = term_w
        inner_w      = box_w - 3
        target_h     = max(14, (term_h * 3) // 4)
        transcript_h = max(4, target_h - 2 - 4)

        all_lines = self._render_transcript(selected)
        total     = len(all_lines)
        end       = min(total, max(total - self._scroll, transcript_h))
        start     = max(0, end - transcript_h)
        visible   = all_lines[start:end]

        inner: list[list[tuple[str, str]]] = []
        inner.append(self._tab_row(states, idx))
        inner.append([])

        for lf in visible:
            inner.append(lf)

        if total > transcript_h and start > 0:
            inner.append([("class:inspector-hint",
                           f" ↑ {start} more lines above  (↑ to scroll)")])

        target_inner = 2 + transcript_h
        while len(inner) < target_inner:
            inner.append([])

        inner.append([])
        inner.append(self._hint_row(states))

        # ── Box rendering ─────────────────────────────────────────────────────
        result = Text()

        title    = "─ Inspector "
        top_fill = max(0, box_w - 2 - len(title))
        result.append("┌" + title + "─" * top_fill + "┐\n", style="#C0C0C0")

        for row in inner:
            text_len = _frags_len(row)
            pad      = max(0, inner_w - text_len)
            result.append("│ ", style="#C0C0C0")
            result.append_text(_frags_to_text(row))
            result.append(" " * pad)
            result.append("│\n", style="#C0C0C0")

        result.append("└" + "─" * (box_w - 2) + "┘", style="#C0C0C0")
        return result

    # ── Tab row ───────────────────────────────────────────────────────────────

    def _tab_row(self, states: list[SubagentState], sel_idx: int) -> list[tuple[str, str]]:
        frags: list[tuple[str, str]] = [("class:inspector-title", " Inspector")]
        for i, s in enumerate(states):
            icon = _STATUS_ICON.get(s.status, "?")
            frags.append(("", "    "))
            if i == sel_idx:
                frags.append(("class:inspector-tab-sel", f" {s.label}  {icon} "))
            else:
                frags.append(("class:inspector-tab", f"{s.label}  "))
                frags.append((_STATUS_STYLE_RICH.get(s.status, "class:inspector-tab"), icon))
        return frags

    # ── Hint row ──────────────────────────────────────────────────────────────

    def _hint_row(self, states: list[SubagentState]) -> list[tuple[str, str]]:
        sep   = ("class:inspector-hint", "  ·  ")
        parts: list[tuple[str, str]] = []
        if len(states) > 1:
            parts += [("class:inspector-hint", "←/→ switch agents"), sep]
        parts += [("class:inspector-hint", "↑↓ scroll"), sep]
        parts += [("class:inspector-hint",
                   "ctrl+e collapse" if self._expanded else "ctrl+e expand"), sep]
        parts += [("class:inspector-hint", "ctrl+o close")]
        return parts

    # ── Transcript ────────────────────────────────────────────────────────────

    def _render_transcript(self, state: SubagentState) -> list[list[tuple[str, str]]]:
        if not state.messages:
            if state.status in ("pending", "running"):
                return [[("class:slot-running", " running…")]]
            if state.status == "error":
                return [[("class:slot-error", f" Error: {state.error}")]]
            return []

        lines = render_message_blocks(state.messages, state.label, expanded=self._expanded)
        if state.status == "error" and state.error:
            lines.append([("class:slot-error", f" ✗  Error: {state.error}")])
        return lines
