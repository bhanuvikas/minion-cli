"""Live grouped status display for parallel tool execution.

When multiple tool calls run concurrently (spawn_agent or any other tool),
each gets its own slot in a live-updating terminal section rather than
interleaving output from racing threads.

Thread-local callback mechanism: the parallel execution helper sets a callback
on each worker thread before starting. ToolExecutor reads it and routes its
display output through the callback instead of console.print(). run_agent does
the same for subagent status updates.
"""

import contextvars
import threading
from typing import Callable, ClassVar, Optional

from rich.live import Live
from rich.text import Text

from ..output.display_utils import apply_slot_event, tool_slot_header_frags
from ..output.base import SlotSpec
from ..theme import GREEN, YELLOW

# ─── Context-variable callback registry ───────────────────────────────────────
# Using ContextVar instead of threading.local so that the callback is correctly
# isolated per asyncio Task (each task copies context on creation) AND per thread
# (threads start with their own context copy). This supports both sync
# ThreadPoolExecutor and async TaskGroup dispatch paths.

_display_callback_var: contextvars.ContextVar[Optional[Callable]] = contextvars.ContextVar(
    "display_callback", default=None
)

_active_live_display_var: contextvars.ContextVar[Optional["ParallelDisplay"]] = (
    contextvars.ContextVar("active_live_display", default=None)
)


def get_agent_display_callback() -> Optional[Callable]:
    """Return the live display callback for the current task/thread, or None."""
    return _display_callback_var.get()


def set_agent_display_callback(callback: Optional[Callable]) -> None:
    """Set (or clear) the live display callback for the current task/thread."""
    _display_callback_var.set(callback)


def get_active_live_display() -> Optional["ParallelDisplay"]:
    """Return the ParallelDisplay active in this task context, or None."""
    return _active_live_display_var.get()


def set_active_live_display(display: Optional["ParallelDisplay"]) -> None:
    """Register (or clear) the active ParallelDisplay for this task context."""
    _active_live_display_var.set(display)


# ─── Live display ─────────────────────────────────────────────────────────────

class ParallelDisplay:
    """Thread-safe live status panel for parallel tool execution (console mode).

    Satisfies ParallelDisplayProtocol. needs_scrollback_flush=False because
    Rich Live's __exit__ prints the final Done state permanently to the terminal.

    Subagent slots (label set) use a compact 2-line format:
        ⏺  [researcher]  Count the methods in game.py…
          └─  Running · ↳ read_file  path='game.py'

    Generic tool slots (no label) use a 3-line format:
        ⚙  read_file  path='/path/to/tetris.py'
          ✓  done (0.1s)
             └─  import pygame  +301 more lines

    Usage:
        display = ParallelDisplay()
        slots = [SlotSpec(key="researcher", tool_name="spawn_agent", inputs=tb.input, label="researcher")]
        display.pre_register(slots)
        with display:
            callback = display.make_callback("researcher")
            set_agent_display_callback(callback)
    """

    needs_scrollback_flush: bool = False  # Rich Live __exit__ prints final state

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}  # key → state dict
        self._order: list[str] = []          # insertion order for stable rendering
        self._paused = False
        # transient=True: stop() erases the live area so the permission UI can render
        # in its place without leaving a frozen "Running" duplicate. pause()/resume()
        # bracket each confirmation; __exit__ prints the final Done state permanently.
        self._live = Live(Text(""), refresh_per_second=8, transient=True)

    def __enter__(self) -> "ParallelDisplay":
        self._live.__enter__()
        return self

    def __exit__(self, *args) -> None:
        self._live.__exit__(*args)
        # transient=True erases the live area on exit. If the live was already stopped
        # (paused for questionary), stop() is a no-op and the cursor stays where
        # questionary left it (start of a new line). Either way, print the final state
        # permanently here — this is the single Done frame the user sees.
        self._live.console.print(self._render())

    def pause(self) -> None:
        """Freeze the live area in place so questionary owns a clean terminal below it."""
        if self._paused:
            return
        self._paused = True
        self._live.stop()

    def resume(self) -> None:
        """Start a new live area below the questionary output."""
        if not self._paused:
            return
        self._paused = False
        self._live.start()

    def render_now(self) -> None:
        """Force an immediate render without waiting for the 8-FPS refresh cycle."""
        if not self._paused:
            self._live.refresh()

    def prompt_user_confirmation(self, message: str) -> str:
        """Print a confirmation prompt above the live area and read one line of input.

        Uses the Live's internal console so the print is correctly coordinated
        with the refresh thread — the message appears permanently above the live
        area without stopping or restarting the display.  The live area continues
        updating while the user types, which keeps other parallel agents visible.

        Returns the raw input string, or "" on KeyboardInterrupt / EOF.
        """
        self._live.console.print(message, end="")
        try:
            return input()
        except (KeyboardInterrupt, EOFError):
            return ""

    def pre_register(self, slots: list[SlotSpec]) -> None:
        """Register all slots as 'pending' before the Live context starts.

        Call this BEFORE entering the `with display:` context so the display
        has a fixed height from the first render — Rich Live never needs to
        resize the live area, eliminating flicker as slots complete.
        """
        with self._lock:
            for slot in slots:
                if slot.key not in self._states:
                    self._states[slot.key] = {
                        "status": "pending",
                        "tool_name": slot.tool_name,
                        "inputs": slot.inputs,
                        "label": slot.label,
                    }
                    self._order.append(slot.key)

    async def pre_register_async(self, slots: list[SlotSpec]) -> None:
        """Async variant of pre_register with a brief transition pause.

        The 300 ms pause lets the terminal render any in-progress thinking
        animation before the slot rows appear.  This is a display timing
        concern that belongs here, not in the caller (runner.py).
        """
        import asyncio as _asyncio
        await _asyncio.sleep(0.3)
        self.pre_register(slots)

    def make_callback(self, key: str) -> Callable:
        """Return an event callback bound to a specific slot key."""
        def callback(event: str, **data) -> None:
            with self._lock:
                if key not in self._states:
                    return
                apply_slot_event(self._states[key], event, **data)
                self._live.update(self._render())
        return callback

    def _append_slot_header(self, text: Text, tool_name: str, inputs: dict) -> None:
        """Append the ⚙ tool_name args... header line for a slot (no trailing newline)."""
        for style, content in tool_slot_header_frags(tool_name, inputs):
            text.append(content, style=style)

    def _render(self) -> Text:
        """Build the current live display as a Rich Text object.

        Subagent slots (label set): 2-line compact format.
        Generic tool slots (no label): 3-line format with a dedicated detail row.
        """
        text = Text()
        first = True
        for key in self._order:
            if not first:
                text.append("\n")
            first = False
            state = self._states.get(key, {})
            tool_name = state.get("tool_name", "")
            inputs = state.get("inputs", {})
            label = state.get("label")
            status = state.get("status", "pending")

            if label:
                # ── Subagent slot — 2-line Claude-Code-style ──────────────────
                # Line 1: ⏺  [role]  task description…
                text.append("⏺  ", style=f"bold {YELLOW}")
                text.append(f"[{label}]", style="bold")
                task = inputs.get("task", "")
                if task:
                    task_clean = task.replace("\n", " ").strip()
                    if len(task_clean) > 58:
                        task_clean = task_clean[:58] + "…"
                    text.append(f"  {task_clean}", style="dim")

                # Line 2: └─  status
                text.append("\n  └─  ", style="dim")

                if status == "pending":
                    text.append("waiting…", style="dim")

                elif status == "running":
                    sub_activities = state.get("sub_activities", [])
                    if sub_activities:
                        parts = []
                        for sa in sub_activities:
                            parts.append(("✓ " if sa["done"] else "") + sa["text"])
                        activity = "  ".join(parts)
                        text.append(f"running · {activity[:80]}", style="dim")
                    else:
                        last_activity = state.get("last_activity", "")
                        activity = last_activity.replace("\n", " ").replace("\r", "")[:72]
                        if activity:
                            text.append(f"running · {activity}", style="dim")
                        else:
                            text.append("running…", style="dim")

                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    text.append(f"done ({latency:.1f}s)", style=f"bold {GREEN}")

                elif status == "error":
                    error = state.get("error", "")
                    text.append(f"Error · {error[:72]}", style="red")

            else:
                # ── Generic tool slot — 3-line format ────────────────────────
                # Line 1: ⚙  tool_name  key='value'…
                self._append_slot_header(text, tool_name, inputs)

                if status == "pending":
                    text.append("\n  ·  waiting…", style="dim")
                    text.append("\n")  # blank detail row — holds slot height

                elif status == "running":
                    text.append("\n  ⚙  running…", style="dim")
                    last_activity = state.get("last_activity", "")
                    last_activity_line = last_activity.replace("\n", " ").replace("\r", "")[:90]
                    text.append(f"\n     {last_activity_line}", style="dim")

                elif status == "complete":
                    latency = state.get("latency_ms", 0) / 1000
                    text.append(f"\n  ✓  done ({latency:.1f}s)", style="dim")
                    preview = state.get("preview", "")
                    text.append(f"\n     └─  {preview[:100]}", style="dim")

                elif status == "error":
                    error = state.get("error", "")
                    text.append(f"\n  ✗  error: {error[:60]}", style="red")
                    text.append("\n")  # blank detail row — holds slot height

        return text


# Backward-compat alias — remove once all call sites are updated.
AgentLiveDisplay = ParallelDisplay
