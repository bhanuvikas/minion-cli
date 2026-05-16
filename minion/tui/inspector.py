"""InspectorScreen — Textual ModalScreen for subagent transcript viewer.

Two-pane layout: agent list on the left, detail + transcript on the right.
Opened via ctrl+o; pushed as a modal overlay over the main screen.
"""

from __future__ import annotations

import re
from typing import Literal

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll, Vertical
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static

from rich.console import Group as RichGroup
from rich.markdown import Markdown as RichMarkdown

from .agent_registry import SubagentRegistry, SubagentState
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


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _frags_to_text(frags: list[tuple[str, str]]) -> Text:
    """Convert (style, text) fragment tuples from display_utils to a Rich Text."""
    t = Text()
    for style, chunk in frags:
        t.append(chunk, style=style or "")
    return t


# Inline Markdown patterns (bold, italic, inline code) for single-paragraph rendering.
_INLINE_MD = re.compile(r'\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|`([^`\n]+)`')

_AGENT_LABEL_STYLE = "bold #E8E8E8"


def _has_block_content(txt: str) -> bool:
    """True when the text has block-level Markdown that needs its own render context."""
    if "\n\n" in txt:
        return True
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("```") or s.startswith("#") or s.startswith("- ") \
                or s.startswith("* ") or (len(s) > 2 and s[0].isdigit() and s[1] in ". )"):
            return True
    return False


def _inline_md_append(txt: str, t: Text) -> None:
    """Append txt to t, converting inline Markdown to Rich styles."""
    pos = 0
    for m in _INLINE_MD.finditer(txt):
        if m.start() > pos:
            t.append(txt[pos:m.start()])
        if m.group(1) is not None:
            t.append(m.group(1), style="bold")
        elif m.group(2) is not None:
            t.append(m.group(2), style="italic")
        elif m.group(3) is not None:
            t.append(m.group(3), style="bold #85C1E9")
        pos = m.end()
    if pos < len(txt):
        t.append(txt[pos:])



def _render_agent_transcript(state: SubagentState) -> list:
    """Return a list of Rich renderables for the agent's conversation.

    Simple single-paragraph assistant text renders inline (prefix + content on
    one line) with basic Markdown formatting. Complex content (code fences,
    headers, lists, multiple paragraphs) uses Group(prefix, Markdown) so block
    elements render correctly.
    """
    from ..output.display_utils import tool_slot_header_frags

    if not state.messages:
        if state.status in ("pending", "running"):
            return [Text("● running…", style=SILVER)]
        if state.status == "error":
            return [Text(f"✗  Error: {state.error or 'unknown'}", style="bold red")]
        return [Text("(no transcript available)", style=DIM)]

    result: list = []

    def _append_assistant_text(txt: str) -> None:
        txt = txt.strip()
        if not txt:
            return
        if _has_block_content(txt):
            # Block-level content: prefix on its own line, Markdown below.
            prefix = Text()
            prefix.append("▌", style=_AGENT_LABEL_STYLE)
            prefix.append(" ")
            prefix.append(state.label, style=_AGENT_LABEL_STYLE)
            prefix.append(" › ", style=DIM)
            result.append(RichGroup(prefix, RichMarkdown(txt)))
        else:
            # Single paragraph: render prefix + content inline on one line.
            t = Text()
            t.append("▌", style=_AGENT_LABEL_STYLE)
            t.append(" ")
            t.append(state.label, style=_AGENT_LABEL_STYLE)
            t.append(" › ", style=DIM)
            _inline_md_append(txt.replace("\n", " "), t)
            result.append(t)
        result.append(Text())

    for msg in state.messages:
        role = msg.get("role", "")

        if role == "user" and msg.get("type") == "text":
            t = Text()
            t.append("▌", style=f"bold {BLUE}")
            t.append(" ")
            t.append("minion", style=f"bold {BLUE}")
            t.append(" › ", style=DIM)
            t.append(msg["text"].strip())
            result.append(t)
            result.append(Text())

        elif role == "assistant" and msg.get("type") == "blocks":
            for blk in msg.get("blocks", []):
                if blk.get("type") == "text":
                    _append_assistant_text(blk.get("text", ""))
                elif blk.get("type") == "tool_use":
                    name = blk.get("name", "")
                    inp  = blk.get("input", {})
                    frags = tool_slot_header_frags(name, inp)
                    result.append(_frags_to_text(list(frags)))
                    from ..output.display_utils import tool_diff_markup
                    diff_markup = tool_diff_markup(name, inp)
                    if diff_markup:
                        from .render import render_rich
                        indented = "   " + diff_markup.rstrip("\n").replace("\n", "\n   ")
                        result.append(render_rich(indented))

        elif role == "assistant" and msg.get("type") == "text":
            _append_assistant_text(msg.get("text", ""))

        elif role == "user" and msg.get("type") == "blocks":
            for blk in msg.get("blocks", []):
                if blk.get("type") == "tool_result":
                    content = blk.get("content", "")
                    done_t = Text()
                    done_t.append("   ✓  done", style=f"bold {GREEN}")
                    result.append(done_t)
                    first_line = content.split("\n")[0].strip() if content else ""
                    if first_line:
                        out_t = Text()
                        out_t.append("   └─  ", style=DIM)
                        out_t.append(first_line, style=DIM)
                        result.append(out_t)
            result.append(Text())

    if state.status == "error" and state.error:
        result.append(Text(f"✗  Error: {state.error}", style="bold red"))

    return result


# ── Widgets ───────────────────────────────────────────────────────────────────

class AgentListRow(Static):
    """Clickable two-line row in the agent list sidebar."""

    class Selected(Message):
        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    def __init__(self, state: SubagentState, index: int, selected: bool) -> None:
        self._state    = state
        self._index    = index
        self._selected = selected
        super().__init__(self._make_text())

    def _make_text(self) -> Text:
        s = self._state
        t = Text(overflow="ellipsis")

        icon  = _STATUS_ICON.get(s.status, "?")
        color = _STATUS_COLOR.get(s.status, SILVER)
        t.append(icon, style=f"bold {color}")
        t.append(" ")

        label_style = f"bold {GOLD}" if self._selected else f"bold {BLUE}"
        t.append(s.label, style=label_style)

        if s.latency_ms > 0:
            t.append(f"  {s.latency_ms / 1000:.1f}s", style=DIM)
        elif s.status == "running":
            t.append("  …", style=DIM)

        if s.output_tokens > 0:
            t.append(f"  {_fmt_tokens(s.output_tokens)}", style=DIM)

        tool_count = sum(
            1 for m in s.messages
            if m.get("role") == "assistant" and m.get("type") == "blocks"
            for b in m.get("blocks", [])
            if b.get("type") == "tool_use"
        )
        if tool_count > 0:
            t.append(f"  ⚙{tool_count}", style=DIM)

        note = (s.task or "").replace("\n", " ")
        if note:
            if len(note) > 24:
                note = note[:21] + "…"
            t.append(f"\n  {note}", style=DIM)

        return t

    def on_mount(self) -> None:
        if self._selected:
            self.add_class("selected")

    def on_click(self) -> None:
        self.post_message(AgentListRow.Selected(self._index))


class TranscriptArea(VerticalScroll):
    """Scrollable area for a subagent's conversation transcript."""


# ── Modal Screen ──────────────────────────────────────────────────────────────

class InspectorScreen(ModalScreen):
    """Full-screen two-pane modal inspector for subagent transcripts."""

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
    }

    #inspector-header {
        height: auto;
        padding: 1 2;
        background: #141414;
        text-align: center;
        content-align: center middle;
    }

    #inspector-body {
        height: 1fr;
        border-bottom: solid #2a2a26;
    }

    /* ── Left pane ── */
    #agent-list-pane {
        width: 34;
        border-right: solid #2a2a26;
        background: #0d0d0d;
    }

    #agents-section-head {
        height: auto;
        padding: 1 2;
        color: #555550;
        background: #0d0d0d;
        text-align: center;
    }

    #agents-section-head.pane-active {
        color: #c98a30;
    }

    #agent-list {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-background: #111111;
        scrollbar-color: #2a2a2a;
        scrollbar-color-hover: #3a3a3a;
        scrollbar-color-active: #555555;
    }

    #totals-section {
        height: auto;
        padding: 1 2;
        border-top: solid #2a2a26;
    }

    AgentListRow {
        height: auto;
        padding: 1 2;
        border-left: solid transparent;
    }

    AgentListRow:hover {
        background: #1a1a18;
    }

    AgentListRow.selected {
        background: #2a1f0e;
        border-left: solid #c98a30;
    }

    /* ── Right pane ── */
    #detail-pane {
        width: 1fr;
        background: #0d0d0d;
    }

    #detail-header {
        height: auto;
        min-height: 2;
        padding: 1 2;
        border-bottom: solid #2a2a26;
    }

    #transcript-heading {
        height: 1;
        padding: 0 2;
        color: #555550;
        background: #0d0d0d;
    }

    #transcript-heading.pane-active {
        color: #c98a30;
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
        height: auto;
        padding: 0 2;
        background: #0a0a0a;
    }
    """

    BINDINGS = [
        Binding("escape", "close_inspector", "Close", show=False),
        Binding("ctrl+o", "close_inspector", "Close", show=False),
    ]

    def __init__(self, registry: SubagentRegistry) -> None:
        self._registry = registry
        self._sel_idx  = 0
        self._focus: Literal["left", "right"] = "left"
        super().__init__()

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        states = self._registry.get_all()
        sel    = self._selected_state()
        with Vertical(id="inspector-panel"):
            yield Static(self._build_header(states), id="inspector-header")
            with Horizontal(id="inspector-body"):
                with Vertical(id="agent-list-pane"):
                    yield Static(self._build_section_head("subagents"), id="agents-section-head")
                    with VerticalScroll(id="agent-list"):
                        for i, s in enumerate(states):
                            yield AgentListRow(s, i, i == self._sel_idx)
                    yield Static(self._build_totals_text(states), id="totals-section")
                with Vertical(id="detail-pane"):
                    yield Static(self._build_detail_header(sel), id="detail-header")
                    yield Static(self._build_section_head("conversation"), id="transcript-heading")
                    with TranscriptArea(id="transcript-area"):
                        for line in self._transcript_lines():
                            yield Static(line)
            yield Static(self._build_footer(states), id="inspector-footer")

    def on_mount(self) -> None:
        self._update_focus_indicators()

    # ── Key handling ──────────────────────────────────────────────────────────

    def on_key(self, event: Key) -> None:
        key = event.key

        if key in ("left", "right", "up", "down", "tab"):
            event.stop()

        if key == "left":
            self._set_pane_focus("left")
        elif key == "right":
            self._set_pane_focus("right")
        elif key == "tab":
            self._set_pane_focus("right" if self._focus == "left" else "left")
        elif key == "up":
            if self._focus == "left":
                self._nav_agent(-1)
            else:
                self.query_one("#transcript-area", TranscriptArea).scroll_up(animate=False)
        elif key == "down":
            if self._focus == "left":
                self._nav_agent(1)
            else:
                self.query_one("#transcript-area", TranscriptArea).scroll_down(animate=False)
        elif key == "pageup":
            if self._focus == "right":
                event.stop()
                self.query_one("#transcript-area", TranscriptArea).scroll_page_up(animate=False)
        elif key == "pagedown":
            if self._focus == "right":
                event.stop()
                self.query_one("#transcript-area", TranscriptArea).scroll_page_down(animate=False)

    def _nav_agent(self, delta: int) -> None:
        n = len(self._registry.get_all())
        if n:
            self._sel_idx = (self._sel_idx + delta) % n
            self._rebuild()

    def _set_pane_focus(self, pane: Literal["left", "right"]) -> None:
        self._focus = pane
        self._update_focus_indicators()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_close_inspector(self) -> None:
        self.dismiss()

    # ── Message handlers ──────────────────────────────────────────────────────

    def on_agent_list_row_selected(self, message: AgentListRow.Selected) -> None:
        self._sel_idx = message.index
        self._rebuild()

    # ── Public API ────────────────────────────────────────────────────────────

    def refresh_from_registry(self) -> None:
        states = self._registry.get_all()
        if states:
            self._sel_idx = min(self._sel_idx, len(states) - 1)
        self._rebuild()

    # ── Focus indicator helpers ───────────────────────────────────────────────

    def _update_focus_indicators(self) -> None:
        """Update section headings and footer to reflect active pane — no full rebuild."""
        agents_head = self.query_one("#agents-section-head", Static)
        tx_head     = self.query_one("#transcript-heading",  Static)
        if self._focus == "left":
            agents_head.add_class("pane-active")
            tx_head.remove_class("pane-active")
        else:
            agents_head.remove_class("pane-active")
            tx_head.add_class("pane-active")
        self.query_one("#inspector-footer", Static).update(
            self._build_footer(self._registry.get_all())
        )

    # ── Rendering helpers ─────────────────────────────────────────────────────

    def _selected_state(self) -> SubagentState | None:
        states = self._registry.get_all()
        if not states:
            return None
        return states[min(self._sel_idx, len(states) - 1)]

    def _build_section_head(self, label: str) -> Text:
        t = Text()
        t.append(label.upper(), style=f"{DIM} bold")
        return t

    def _build_header(self, states: list[SubagentState]) -> Text:
        n       = len(states)
        running = sum(1 for s in states if s.status == "running")
        failed  = sum(1 for s in states if s.status == "error")
        t = Text(justify="center")
        t.append("Subagent Inspector\n \n", style="bold #D0D0D0")
        t.append(f"{n} agent{'s' if n != 1 else ''}", style=DIM)
        if running:
            t.append(f"  ·  {running} running", style=SILVER)
        if failed:
            t.append(f"  ·  {failed} failed", style="bold red")
        return t

    @staticmethod
    def _count_tools(messages: list[dict]) -> int:
        return sum(
            1 for m in messages
            if m.get("role") == "assistant" and m.get("type") == "blocks"
            for b in m.get("blocks", [])
            if b.get("type") == "tool_use"
        )

    def _build_totals_text(self, states: list[SubagentState]) -> Text:
        if not states:
            return Text(" ")
        elapsed_ms  = max((s.latency_ms for s in states if s.latency_ms > 0), default=0)
        total_tok   = sum(s.output_tokens for s in states)
        total_tools = sum(self._count_tools(s.messages) for s in states)
        t = Text()
        t.append("TOTALS\n \n", style=f"{DIM} bold")
        t.append("elapsed  ", style=DIM)
        t.append(f"{elapsed_ms / 1000:.1f}s" if elapsed_ms > 0 else "—", style="#C0C0C0")
        if total_tok > 0:
            t.append("\ntokens   ", style=DIM)
            t.append(_fmt_tokens(total_tok), style="#C0C0C0")
        if total_tools > 0:
            t.append("\ntools    ", style=DIM)
            t.append(str(total_tools), style="#C0C0C0")
        return t

    def _build_detail_header(self, state: SubagentState | None) -> Text:
        if state is None:
            return Text(" ")
        icon  = _STATUS_ICON.get(state.status, "?")
        color = _STATUS_COLOR.get(state.status, SILVER)
        t = Text()
        t.append(state.label, style=f"bold {BLUE}")
        t.append("   ")
        t.append(icon, style=f"bold {color}")
        t.append(f"  {state.status}", style=color)
        if state.latency_ms > 0:
            t.append(f"  ·  {state.latency_ms / 1000:.1f}s", style=DIM)
        if state.output_tokens > 0:
            t.append(f"  ·  {_fmt_tokens(state.output_tokens)} tok", style=DIM)
        tool_count = self._count_tools(state.messages)
        if tool_count > 0:
            t.append(f"  ·  {tool_count} tool{'s' if tool_count != 1 else ''}", style=DIM)
        if state.task:
            task = state.task.replace("\n", " ")
            if len(task) > 70:
                task = task[:67] + "…"
            t.append(f"\n{task}", style=DIM)
        return t

    def _build_footer(self, states: list[SubagentState]) -> Text:
        if self._focus == "left":
            parts = ["↑↓ agent", "→ view transcript", "tab switch"]
        else:
            parts = ["↑↓ scroll", "← agent list", "tab switch"]
        parts.append("ctrl+o / esc close")
        t = Text()
        t.append("  ·  ".join(parts), style=DIM)
        return t

    def _transcript_lines(self) -> list:
        s = self._selected_state()
        if s is None:
            return [Text("  (no agents)", style=DIM)]
        return _render_agent_transcript(s)

    def _rebuild(self) -> None:
        states = self._registry.get_all()
        sel    = self._selected_state()

        self.query_one("#inspector-header", Static).update(self._build_header(states))

        agent_list = self.query_one("#agent-list", VerticalScroll)
        agent_list.remove_children()
        for i, s in enumerate(states):
            agent_list.mount(AgentListRow(s, i, i == self._sel_idx))

        self.query_one("#totals-section", Static).update(self._build_totals_text(states))
        self.query_one("#detail-header",  Static).update(self._build_detail_header(sel))

        area = self.query_one("#transcript-area", TranscriptArea)
        area.remove_children()
        for line in self._transcript_lines():
            area.mount(Static(line))
        area.scroll_home(animate=False)

        self.query_one("#inspector-footer", Static).update(self._build_footer(states))
        self._update_focus_indicators()
