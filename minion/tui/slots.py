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
from ..theme import GREEN
from .messages import SlotsUpdated


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

        result = Text()
        first = True

        for key in order:
            if not first:
                result.append("\n")
            first = False

            state       = states.get(key, {})
            tool_name   = state.get("tool_name", "")
            inputs      = state.get("inputs", {})
            label       = state.get("label")
            status      = state.get("status", "pending")
            diff_markup = state.get("diff_markup", "")

            if label:
                # ── Subagent slot — 2-line format ─────────────────────────────
                result.append("⏺  ", style=f"bold {GREEN}")
                result.append(f"[{label}]", style="bold")
                task = inputs.get("task", "")
                if task:
                    task_clean = task.replace("\n", " ").strip()
                    if len(task_clean) > 58:
                        task_clean = task_clean[:58] + "…"
                    result.append(f"  {task_clean}", style="dim")
                result.append("\n   └─  ")

                if status == "pending":
                    result.append("waiting…", style="dim")
                elif status == "running":
                    sub_activities = state.get("sub_activities", [])
                    if sub_activities:
                        parts = []
                        for sa in sub_activities:
                            parts.append(("✓ " if sa["done"] else "") + sa["text"])
                        activity = "  ".join(parts)
                        result.append(f"running · {activity[:80]}", style="dim")
                    else:
                        last = state.get("last_activity", "")
                        act  = last.replace("\n", " ").replace("\r", "")[:72]
                        result.append(f"running · {act}" if act else "running…", style="dim")
                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    result.append(f"done ({latency:.1f}s)", style=f"bold {GREEN}")
                    preview = state.get("preview", "")
                    if preview:
                        result.append(f"\n       {preview[:100]}", style="dim")
                elif status == "error":
                    error = state.get("error", "")
                    result.append(f"Error · {error[:72]}", style="red")

            else:
                # ── Generic tool slot — header + optional diff + status ────────
                for style, text in tool_slot_header_frags(tool_name, inputs):
                    result.append(text, style=style)

                # Diff shown between header and status, indented to match status lines
                if diff_markup and status != "pending":
                    indented = "   " + diff_markup.rstrip("\n").replace("\n", "\n   ")
                    result.append("\n")
                    result.append_text(Text.from_markup(indented))

                if status == "pending":
                    result.append("\n   ○  waiting…", style="dim")
                    result.append("\n")
                elif status == "running":
                    result.append("\n   ○  running…", style="dim")
                    last = state.get("last_activity", "")
                    last_line = last.replace("\n", " ").replace("\r", "")[:90]
                    result.append(f"\n   {last_line}", style="dim")
                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    result.append(f"\n   ✓  done ({latency:.1f}s)", style=f"bold {GREEN}")
                    preview = state.get("preview", "")
                    result.append(f"\n   └─  {preview[:100]}", style="dim")
                elif status == "error":
                    error = state.get("error", "")
                    result.append(f"\n   ✗  error: {error[:60]}", style="red")
                    result.append("\n")

        return result
