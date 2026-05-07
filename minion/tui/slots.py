"""SlotsManager — TUI replacement for AgentLiveDisplay.

Uses the same callback protocol as AgentLiveDisplay.make_callback() so
runner.py can use it transparently. Instead of updating a Rich Live display,
it calls app.invalidate() to trigger a prompt_toolkit redraw.

No pause()/resume() needed — ConfirmationManager in TUI mode routes directly
to the permission panel rather than pausing the display.
"""

import threading
from typing import Callable, ClassVar, Optional

from prompt_toolkit.formatted_text import FormattedText

YELLOW = "#FFD700"
BLUE   = "#1E90FF"
GREEN  = "#4CAF50"


def _format_tool_args(inputs: dict) -> str:
    """Same helper as in display.py — brief key='value' snippet."""
    if not inputs:
        return ""
    for k, v in inputs.items():
        if isinstance(v, str):
            return f"{k}='{v[:40]}'"
        return f"{k}={str(v)[:40]}"
    return ""


class SlotsManager:
    """Thread-safe slot state manager for the TUI slots zone.

    Same public interface as AgentLiveDisplay, minus the Rich Live machinery.
    """

    _SLOT_SKIP_KEYS: ClassVar[dict[str, set[str]]] = {
        "write_file": {"content"},
        "edit_file":  {"old_string", "new_string"},
    }

    def __init__(self, invalidate_fn: Callable) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}
        self._order: list[str] = []
        self._invalidate = invalidate_fn

    # ── Pre-registration (mirrors AgentLiveDisplay.pre_register) ─────────────

    def pre_register(self, slots) -> None:
        """Register slots before the run starts (same API as AgentLiveDisplay)."""
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

    def clear(self) -> None:
        with self._lock:
            self._states.clear()
            self._order.clear()

    # ── Callback factory (mirrors AgentLiveDisplay.make_callback) ────────────

    def make_callback(self, key: str) -> Callable:
        """Return an event callback bound to a specific slot key."""
        def callback(event: str, **data) -> None:
            with self._lock:
                if key not in self._states:
                    return
                state = self._states[key]
                if event == "running":
                    state["status"] = "running"
                elif event == "complete":
                    state.update({
                        "status":     "complete",
                        "latency_ms": data.get("latency_ms", 0),
                        "preview":    data.get("preview", ""),
                    })
                elif event == "error":
                    state.update({
                        "status": "error",
                        "error":  data.get("error", ""),
                    })
                elif event == "tool_call":
                    name   = data.get("name", "")
                    inputs = data.get("inputs", {})
                    state["last_activity"] = f"↳ {name}  {_format_tool_args(inputs)}"
                elif event == "text":
                    buf = state.get("_text_buf", "") + data.get("text", "")
                    state["_text_buf"] = buf[-200:]
                    flat = " ".join(state["_text_buf"].split())
                    if flat:
                        state["last_activity"] = f"· {flat[-80:]}"
                elif event == "parallel_sub_start":
                    state["sub_activities"] = [
                        {
                            "key":  t["key"],
                            "text": f"↳ {t['name']}  {_format_tool_args(t['inputs'])}",
                            "done": False,
                        }
                        for t in data.get("tools", [])
                    ]
                elif event == "parallel_sub_done":
                    done_key = data.get("key")
                    for sa in state.get("sub_activities", []):
                        if sa["key"] == done_key:
                            sa["done"] = True
                elif event == "parallel_sub_clear":
                    state["sub_activities"] = []
            self._invalidate()
        return callback

    # ── Context manager (noop — AgentLiveDisplay compatibility) ─────────────────

    def __enter__(self) -> "SlotsManager":
        return self

    def __exit__(self, *args: object) -> None:
        pass  # slots remain visible; prompt_toolkit redraws on next invalidate

    def render_now(self) -> None:
        self._invalidate()

    def slot_results(self) -> list[dict]:
        """Return a snapshot of all slot states in registration order."""
        with self._lock:
            return [dict(self._states[k]) for k in self._order if k in self._states]

    # ── Visibility ────────────────────────────────────────────────────────────

    @property
    def is_visible(self) -> bool:
        """True if any slots are registered (show the slots zone)."""
        with self._lock:
            return bool(self._states)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def get_formatted_text(self) -> FormattedText:
        """Render all slots as prompt_toolkit FormattedText."""
        with self._lock:
            order  = list(self._order)
            states = {k: dict(v) for k, v in self._states.items()}

        fragments: list[tuple[str, str]] = []
        first = True

        for key in order:
            if not first:
                fragments.append(("", "\n"))
            first = False

            state     = states.get(key, {})
            tool_name = state.get("tool_name", "")
            inputs    = state.get("inputs", {})
            label     = state.get("label")
            status    = state.get("status", "pending")

            if label:
                # ── Subagent slot — 2-line format ─────────────────────────────
                fragments.append(("class:slot-icon", "⏺  "))
                fragments.append(("class:slot-label", f"[{label}]"))
                task = inputs.get("task", "")
                if task:
                    task_clean = task.replace("\n", " ").strip()
                    if len(task_clean) > 58:
                        task_clean = task_clean[:58] + "…"
                    fragments.append(("class:slot-task", f"  {task_clean}"))
                fragments.append(("", "\n  └─  "))

                if status == "pending":
                    fragments.append(("class:slot-running", "waiting…"))
                elif status == "running":
                    sub_activities = state.get("sub_activities", [])
                    if sub_activities:
                        parts = []
                        for sa in sub_activities:
                            parts.append(("✓ " if sa["done"] else "") + sa["text"])
                        activity = "  ".join(parts)
                        fragments.append(("class:slot-running", f"Running · {activity[:80]}"))
                    else:
                        last = state.get("last_activity", "")
                        act  = last.replace("\n", " ").replace("\r", "")[:72]
                        if act:
                            fragments.append(("class:slot-running", f"Running · {act}"))
                        else:
                            fragments.append(("class:slot-running", "Running…"))
                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    fragments.append(("class:slot-done", f"done ({latency:.1f}s)"))
                elif status == "error":
                    error = state.get("error", "")
                    fragments.append(("class:slot-error", f"Error · {error[:72]}"))

            else:
                # ── Generic tool slot — 3-line format ────────────────────────
                fragments.append(("class:slot-icon", "⚙  "))
                fragments.append(("class:slot-label", tool_name))
                skip = self._SLOT_SKIP_KEYS.get(tool_name, set())
                for k, v in inputs.items():
                    if k in skip:
                        continue
                    if isinstance(v, str):
                        v_clean = v.replace("\n", "↵").replace("\r", "")
                        v_disp  = f'"{v_clean[:50]}…"' if len(v_clean) > 50 else f"'{v_clean}'"
                    else:
                        v_disp = repr(v)[:40]
                    fragments.append(("class:slot-detail", f"  {k}="))
                    fragments.append(("", v_disp))

                if status == "pending":
                    fragments.append(("class:slot-running", "\n  ·  waiting…"))
                    fragments.append(("", "\n"))
                elif status == "running":
                    fragments.append(("class:slot-running", "\n  ⚙  running…"))
                    last = state.get("last_activity", "")
                    last_line = last.replace("\n", " ").replace("\r", "")[:90]
                    fragments.append(("class:slot-detail", f"\n     {last_line}"))
                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    fragments.append(("class:slot-done", f"\n  ✓  done ({latency:.1f}s)"))
                    preview = state.get("preview", "")
                    fragments.append(("class:slot-detail", f"\n     └─  {preview[:100]}"))
                elif status == "error":
                    error = state.get("error", "")
                    fragments.append(("class:slot-error", f"\n  ✗  error: {error[:60]}"))
                    fragments.append(("", "\n"))

        return FormattedText(fragments)
