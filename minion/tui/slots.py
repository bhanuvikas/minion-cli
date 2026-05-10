"""SlotsManager — TUI parallel display (satisfies ParallelDisplayProtocol).

Uses the same callback interface as ParallelDisplay (console) so runner.py
can treat both transparently. Instead of updating a Rich Live display,
it posts a SlotsUpdated message to the Textual app (thread-safe).

needs_scrollback_flush=True: after a parallel run, the caller must commit
completed slot states to the conversation buffer and then call clear().
"""

import threading
from typing import Callable

from rich.text import Text

from ..output.display_utils import apply_slot_event, tool_slot_header_frags
from .messages import SlotsUpdated


def _frags_to_rich_text(frags: list[tuple[str, str]]) -> Text:
    """Convert (style, text) fragment list to a Rich Text object."""
    t = Text()
    for style, chunk in frags:
        # Strip class: prefix — these were prompt_toolkit class names
        if style.startswith("class:"):
            style = _CLASS_TO_RICH.get(style[6:], "")
        t.append(chunk, style=style or "")
    return t


# Mapping from old prompt_toolkit class names → Rich style strings
_CLASS_TO_RICH: dict[str, str] = {
    "slot-icon":    "#888888",
    "slot-label":   "bold",
    "slot-task":    "#C0C0C0",
    "slot-running": "#C0C0C0",
    "slot-done":    "bold #4CAF50",
    "slot-error":   "bold red",
    "slot-detail":  "#C0C0C0",
}


class SlotsManager:
    """Thread-safe slot state manager for the TUI slots zone.

    Satisfies ParallelDisplayProtocol. needs_scrollback_flush=True because
    completed slot states must be flushed to the conversation buffer manually.
    """

    needs_scrollback_flush: bool = True

    def __init__(self, post_message_fn: Callable) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}
        self._order: list[str] = []
        self._post_message = post_message_fn

    # ── Pre-registration ──────────────────────────────────────────────────────

    def pre_register(self, slots) -> None:
        with self._lock:
            for slot in slots:
                if slot.key not in self._states:
                    self._states[slot.key] = {
                        "status":    "pending",
                        "tool_name": slot.tool_name,
                        "inputs":    slot.inputs,
                        "label":     slot.label,
                    }
                    self._order.append(slot.key)

    async def pre_register_async(self, slots) -> None:
        self.pre_register(slots)

    def clear(self) -> None:
        with self._lock:
            self._states.clear()
            self._order.clear()
        self._post_message(SlotsUpdated())

    # ── Callback factory ──────────────────────────────────────────────────────

    def make_callback(self, key: str) -> Callable:
        def callback(event: str, **data) -> None:
            with self._lock:
                if key not in self._states:
                    return
                apply_slot_event(self._states[key], event, **data)
            self._post_message(SlotsUpdated())
        return callback

    # ── Context manager (noop) ────────────────────────────────────────────────

    def __enter__(self) -> "SlotsManager":
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def render_now(self) -> None:
        self._post_message(SlotsUpdated())

    def slot_results(self) -> list[dict]:
        with self._lock:
            return [dict(self._states[k]) for k in self._order if k in self._states]

    # ── Visibility ────────────────────────────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        with self._lock:
            return bool(self._states)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def get_rich_text(self) -> Text:
        """Render all slots as a Rich Text object."""
        with self._lock:
            order  = list(self._order)
            states = {k: dict(v) for k, v in self._states.items()}

        frags: list[tuple[str, str]] = []
        first = True

        for key in order:
            if not first:
                frags.append(("", "\n"))
            first = False

            state     = states.get(key, {})
            tool_name = state.get("tool_name", "")
            inputs    = state.get("inputs", {})
            label     = state.get("label")
            status    = state.get("status", "pending")

            if label:
                # ── Subagent slot — 2-line format ─────────────────────────────
                frags.append(("class:slot-icon", "⏺  "))
                frags.append(("class:slot-label", f"[{label}]"))
                task = inputs.get("task", "")
                if task:
                    task_clean = task.replace("\n", " ").strip()
                    if len(task_clean) > 58:
                        task_clean = task_clean[:58] + "…"
                    frags.append(("class:slot-task", f"  {task_clean}"))
                frags.append(("", "\n   └─  "))

                if status == "pending":
                    frags.append(("class:slot-running", "waiting…"))
                elif status == "running":
                    sub_activities = state.get("sub_activities", [])
                    if sub_activities:
                        parts = []
                        for sa in sub_activities:
                            parts.append(("✓ " if sa["done"] else "") + sa["text"])
                        activity = "  ".join(parts)
                        frags.append(("class:slot-running", f"running · {activity[:80]}"))
                    else:
                        last = state.get("last_activity", "")
                        act  = last.replace("\n", " ").replace("\r", "")[:72]
                        frags.append(("class:slot-running", f"running · {act}" if act else "running…"))
                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    frags.append(("class:slot-done", f"done ({latency:.1f}s)"))
                    preview = state.get("preview", "")
                    if preview:
                        frags.append(("class:slot-detail", f"\n       {preview[:100]}"))
                elif status == "error":
                    error = state.get("error", "")
                    frags.append(("class:slot-error", f"Error · {error[:72]}"))

            else:
                # ── Generic tool slot — 3-line format ────────────────────────
                frags.extend(tool_slot_header_frags(tool_name, inputs))

                if status == "pending":
                    frags.append(("class:slot-running", "\n   ○  waiting…"))
                    frags.append(("", "\n"))
                elif status == "running":
                    frags.append(("class:slot-running", "\n   ○  running…"))
                    last = state.get("last_activity", "")
                    last_line = last.replace("\n", " ").replace("\r", "")[:90]
                    frags.append(("class:slot-detail", f"\n   {last_line}"))
                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    frags.append(("class:slot-done", f"\n   ✓  done ({latency:.1f}s)"))
                    preview = state.get("preview", "")
                    frags.append(("class:slot-detail", f"\n   └─  {preview[:100]}"))
                elif status == "error":
                    error = state.get("error", "")
                    frags.append(("class:slot-error", f"\n   ✗  error: {error[:60]}"))
                    frags.append(("", "\n"))

        return _frags_to_rich_text(frags)
