"""InspectorPanel — read-only subagent transcript viewer (Ctrl+O).

Renders a full-width bordered box at 3/4 terminal height:

  ┌─ Inspector ──────────────────────────────────────────────────────────────────┐
  │   [coder ✓]    coder ●                                                       │
  │                                                                               │
  │   ── Turn 1 ────────────────────────────────────────────────────────────     │
  │      I'll create hello.py for you.                                            │
  │      ⚙  write_file  path='hello.py'                                           │
  │         ✓  Wrote 24 chars (1 lines) to 'hello.py'.                           │
  │                                                                               │
  │   ←/→ switch agents  ·  ↑↓ scroll  ·  ctrl+e expand  ·  ctrl+o close        │
  └───────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText

from .agent_registry import SubagentRegistry, SubagentState

_STATUS_ICON = {"pending": "○", "running": "●", "complete": "✓", "error": "✗"}
_STATUS_STYLE = {
    "pending": "class:slot-running",
    "running": "class:slot-running",
    "complete": "class:slot-done",
    "error":    "class:slot-error",
}

# Fallback dimensions when the app is not yet running
_DEFAULT_WIDTH  = 120
_DEFAULT_HEIGHT = 40


def _trunc(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def _frags_len(frags: list[tuple[str, str]]) -> int:
    return sum(len(t) for _, t in frags)


class InspectorPanel:
    """Reads from SubagentRegistry; renders a bordered box as FormattedText."""

    def __init__(self, registry: SubagentRegistry) -> None:
        self._reg      = registry
        self._visible  = False
        self._sel_idx  = 0
        self._scroll   = 0
        self._expanded = False

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

    def get_formatted_text(self) -> FormattedText:
        states = self._reg.get_all()
        if not states:
            self._visible = False
            return FormattedText([])

        try:
            from prompt_toolkit.application.current import get_app
            size   = get_app().output.get_size()
            term_w = size.columns
            term_h = size.rows
        except Exception:
            term_w = _DEFAULT_WIDTH
            term_h = _DEFAULT_HEIGHT

        idx      = min(self._sel_idx, len(states) - 1)
        selected = states[idx]

        # Box geometry
        box_w   = term_w
        inner_w = box_w - 3        # "│ " (2) + content + " │" (2) — use 3 so pad by 1 minimum
        # 3/4 of terminal, minimum 14 lines
        target_h     = max(14, (term_h * 3) // 4)
        # Fixed inner rows: tab_row + blank + blank_before_hint + hint_row = 4
        transcript_h = max(4, target_h - 2 - 4)   # subtract top+bottom borders + 4 fixed

        # Transcript content
        all_lines = self._render_transcript(selected)
        total     = len(all_lines)
        end       = min(total, max(total - self._scroll, transcript_h))
        start     = max(0, end - transcript_h)
        visible   = all_lines[start:end]

        # Assemble inner rows (plain fragment lists, no box borders yet)
        inner: list[list[tuple[str, str]]] = []

        inner.append(self._tab_row(states, idx))   # tab pills
        inner.append([])                            # blank after tabs

        for lf in visible:
            inner.append(lf)

        if total > transcript_h and start > 0:
            inner.append([("class:inspector-hint",
                            f" ↑ {start} more lines above  (↑ to scroll)")])

        # Pad to fill transcript area
        target_inner = 2 + transcript_h  # tab + blank + transcript lines
        while len(inner) < target_inner:
            inner.append([])

        inner.append([])                            # blank before hint
        inner.append(self._hint_row(states))        # key hint

        # ── Box rendering ─────────────────────────────────────────────────────
        frags: list[tuple[str, str]] = []

        # Top border with embedded title
        title    = "─ Inspector "
        top_fill = max(0, box_w - 2 - len(title))
        frags.append(("class:slot-detail", "┌" + title + "─" * top_fill + "┐\n"))

        for row in inner:
            text_len = _frags_len(row)
            pad      = max(0, inner_w - text_len)
            frags.append(("class:slot-detail", "│ "))
            frags.extend(row)
            frags.append(("", " " * pad))
            frags.append(("class:slot-detail", "│\n"))

        # Bottom border
        frags.append(("class:slot-detail", "└" + "─" * (box_w - 2) + "┘"))

        return FormattedText(frags)

    # ── Tab row ───────────────────────────────────────────────────────────────

    def _tab_row(
        self, states: list[SubagentState], sel_idx: int
    ) -> list[tuple[str, str]]:
        frags: list[tuple[str, str]] = [("class:inspector-title", " Inspector")]

        for i, s in enumerate(states):
            icon  = _STATUS_ICON.get(s.status, "?")
            frags.append(("", "    "))
            if i == sel_idx:
                frags.append(("class:inspector-tab-sel", f" {s.label}  {icon} "))
            else:
                frags.append(("class:inspector-tab", f"{s.label}  "))
                frags.append((_STATUS_STYLE.get(s.status, "class:inspector-tab"), icon))

        return frags

    # ── Hint row ──────────────────────────────────────────────────────────────

    def _hint_row(
        self, states: list[SubagentState]
    ) -> list[tuple[str, str]]:
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

    def _render_transcript(
        self, state: SubagentState
    ) -> list[list[tuple[str, str]]]:
        lines: list[list[tuple[str, str]]] = []

        def _line(*frags: tuple[str, str]) -> None:
            lines.append(list(frags))

        if not state.messages:
            if state.status in ("pending", "running"):
                _line(("class:slot-running", " running…"))
            elif state.status == "error":
                _line(("class:slot-error", f" Error: {state.error}"))
            return lines

        turns = self._group_into_turns(state.messages)
        for turn_idx, turn in enumerate(turns, 1):
            is_current = (turn_idx == len(turns) and state.status == "running")
            lbl = f" ── Turn {turn_idx}" + (" (current)" if is_current else "") + " "
            lbl += "─" * max(0, 56 - len(lbl))
            _line(("class:slot-detail", lbl))

            for msg in turn:
                role = msg.get("role", "")

                if role == "user" and msg.get("type") == "text" and turn_idx == 1:
                    text = msg["text"].replace("\n", " ").strip()
                    limit = 400 if self._expanded else 90
                    _line(("class:slot-detail", f"   you › {_trunc(text, limit)}"))

                elif role == "assistant" and msg.get("type") == "blocks":
                    for blk in msg.get("blocks", []):
                        if blk["type"] == "text" and blk.get("text"):
                            txt   = blk["text"].replace("\n", " ").strip()
                            limit = 400 if self._expanded else 93
                            _line(("", f"   {_trunc(txt, limit)}"))
                        elif blk["type"] == "tool_use":
                            args = _format_tool_args(blk.get("input", {}), self._expanded)
                            _line(
                                ("class:slot-icon",   "   ⚙  "),
                                ("bold",              blk.get("name", "")),
                                ("class:slot-detail", f"  {args}"),
                            )

                elif role == "user" and msg.get("type") == "blocks":
                    for blk in msg.get("blocks", []):
                        if blk["type"] == "tool_result":
                            content = blk.get("content", "").replace("\n", " ").strip()
                            limit   = 400 if self._expanded else 87
                            _line(
                                ("class:slot-done",   "      ✓  "),
                                ("class:slot-detail", _trunc(content, limit)),
                            )

            _line(("", ""))  # blank between turns

        if state.status == "error" and state.error:
            _line(("class:slot-error", f" ✗  Error: {state.error}"))

        return lines

    def _group_into_turns(self, messages: list[dict]) -> list[list[dict]]:
        turns:   list[list[dict]] = []
        current: list[dict]       = []

        for msg in messages:
            role = msg.get("role", "")
            if role == "assistant":
                if current:
                    turns.append(current)
                current = [msg]
            elif role == "user" and msg.get("type") == "text":
                if not turns and not current:
                    current = [msg]
                else:
                    current.append(msg)
            else:
                current.append(msg)

        if current:
            turns.append(current)

        if len(turns) >= 2 and all(m.get("role") == "user" for m in turns[0]):
            turns[1] = turns[0] + turns[1]
            turns.pop(0)

        return turns if turns else [[]]


def _format_tool_args(inputs: dict, expanded: bool = False) -> str:
    if not inputs:
        return ""
    parts = []
    skip  = {"content", "old_string", "new_string"}
    for k, v in inputs.items():
        if k in skip and not expanded:
            continue
        if isinstance(v, str):
            v_clean = v.replace("\n", "↵")
            limit   = 200 if expanded else 45
            v_disp  = f"'{_trunc(v_clean, limit)}'"
        else:
            v_disp = repr(v)[:40]
        parts.append(f"{k}={v_disp}")
    return "  ".join(parts[:3])
