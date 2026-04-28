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
from typing import Callable, NamedTuple, Optional

from rich.live import Live
from rich.text import Text

YELLOW = "#FFD700"
BLUE = "#1E90FF"


def _format_tool_args(inputs: dict) -> str:
    """Return a brief key='value' snippet from tool inputs for the slot detail line."""
    if not inputs:
        return ""
    for k, v in inputs.items():
        if isinstance(v, str):
            return f"{k}='{v[:40]}'"
        return f"{k}={str(v)[:40]}"
    return ""

# ─── Context-variable callback registry ───────────────────────────────────────
# Using ContextVar instead of threading.local so that the callback is correctly
# isolated per asyncio Task (each task copies context on creation) AND per thread
# (threads start with their own context copy). This supports both sync
# ThreadPoolExecutor and async TaskGroup dispatch paths.

_display_callback_var: contextvars.ContextVar[Optional[Callable]] = contextvars.ContextVar(
    "display_callback", default=None
)


def get_agent_display_callback() -> Optional[Callable]:
    """Return the live display callback for the current task/thread, or None."""
    return _display_callback_var.get()


def set_agent_display_callback(callback: Optional[Callable]) -> None:
    """Set (or clear) the live display callback for the current task/thread."""
    _display_callback_var.set(callback)


# ─── Slot specification ───────────────────────────────────────────────────────

class SlotSpec(NamedTuple):
    """Definition for one slot in the parallel live display.

    key       : unique identifier (role name for agents, tool_use id for generic tools)
    tool_name : shown in the header line (e.g. "spawn_agent", "read_file")
    inputs    : tool inputs dict — used to render the header args
    label     : optional [label] shown in status lines; used for agent role names;
                None for generic tools where the header is self-identifying
    """
    key: str
    tool_name: str
    inputs: dict
    label: Optional[str] = None


# ─── Live display ─────────────────────────────────────────────────────────────

class AgentLiveDisplay:
    """Thread-safe live status panel for parallel tool execution.

    Each tool call gets a slot that updates in place — 3 fixed lines per slot:

    For spawn_agent (with label):
        ⚙  spawn_agent  role='researcher'  task="Count the methods in..."
          ⚙  [researcher]  running...
             ↳ read_file  'game.py'

    For generic tools (no label):
        ⚙  read_file  path='/path/to/tetris.py'
          ⚙  running...
             (blank while running)
        ⚙  read_file  path='/path/to/tetris.py'
          ✓  done (0.1s)
             └─  import pygame  +301 more lines

    Usage:
        display = AgentLiveDisplay()
        slots = [SlotSpec(key="researcher", tool_name="spawn_agent", inputs=tb.input, label="researcher")]
        display.pre_register(slots)
        with display:
            callback = display.make_callback("researcher")
            set_agent_display_callback(callback)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}  # key → state dict
        self._order: list[str] = []          # insertion order for stable rendering
        self._live = Live(Text(""), refresh_per_second=8, transient=False)

    def __enter__(self) -> "AgentLiveDisplay":
        self._live.__enter__()
        return self

    def __exit__(self, *args) -> None:
        self._live.__exit__(*args)

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
                        "status": "complete",
                        "latency_ms": data.get("latency_ms", 0),
                        "preview": data.get("preview", ""),
                    })
                elif event == "error":
                    state.update({
                        "status": "error",
                        "error": data.get("error", ""),
                    })
                elif event == "tool_call":
                    name = data.get("name", "")
                    inputs = data.get("inputs", {})
                    state["last_activity"] = f"↳ {name}  {_format_tool_args(inputs)}"
                elif event == "text":
                    buf = state.get("_text_buf", "") + data.get("text", "")
                    state["_text_buf"] = buf[-200:]
                    # Collapse all whitespace (including newlines from LLM lists/paragraphs)
                    # to a single-line snippet — the detail row must stay exactly one line.
                    flat = " ".join(state["_text_buf"].split())
                    if flat:
                        state["last_activity"] = f"· {flat[-80:]}"
                self._live.update(self._render())
        return callback

    def _append_slot_header(self, text: Text, tool_name: str, inputs: dict) -> None:
        """Append the ⚙ tool_name args... header line for a slot (no trailing newline)."""
        text.append("⚙  ", style=f"bold {YELLOW}")
        text.append(tool_name, style="bold")
        for k, v in inputs.items():
            if isinstance(v, str) and len(v) > 50:
                v_display = f'"{v[:50]}…"'
            elif isinstance(v, str):
                v_display = f"'{v}'"
            else:
                v_display = repr(v)[:40]
            text.append(f"  {k}=", style="dim")
            text.append(v_display, style=BLUE)

    def _render(self) -> Text:
        """Build the current live display as a Rich Text object.

        Every slot always occupies exactly 3 lines:
          Line 1: ⚙ tool_name args... (tool call header)
          Line 2: status (running / complete / error / waiting)
          Line 3: detail (last activity, preview, or blank)

        Keeping the height constant prevents Rich from having to resize the live
        area when slots complete and add preview text — eliminating flicker.
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

            # Line 1: tool call header
            self._append_slot_header(text, tool_name, inputs)

            label_part = f"[{label}]  " if label else ""
            label_style = "bold" if label else "dim"

            if status == "pending":
                text.append(f"\n  ·  ", style="dim")
                if label:
                    text.append(f"[{label}]", style="bold")
                    text.append("  waiting...", style="dim")
                else:
                    text.append("waiting...", style="dim")
                text.append("\n")  # blank detail row — holds the slot height

            elif status == "running":
                text.append(f"\n  ⚙  ", style="dim")
                if label:
                    text.append(f"[{label}]", style="bold")
                    text.append("  running...", style="dim")
                else:
                    text.append("running...", style="dim")
                last_activity = state.get("last_activity", "")
                # Always emit the detail line (even if blank) to keep slot height stable.
                # Replace any stray newlines so the row never expands beyond one line.
                last_activity_line = last_activity.replace("\n", " ").replace("\r", "")[:90]
                text.append(f"\n     {last_activity_line}", style="dim")

            elif status == "complete":
                latency = state.get("latency_ms", 0) / 1000
                text.append(f"\n  ✓  ", style="dim")
                if label:
                    text.append(f"[{label}]", style="bold")
                    text.append(f"  complete ({latency:.1f}s)", style="dim")
                else:
                    text.append(f"done ({latency:.1f}s)", style="dim")
                preview = state.get("preview", "")
                text.append(f"\n     └─  {preview[:100]}", style="dim")

            elif status == "error":
                error = state.get("error", "")
                text.append(f"\n  ✗  ", style="dim")
                if label:
                    text.append(f"[{label}]", style="bold")
                    text.append(f"  error: {error[:60]}", style="red")
                else:
                    text.append(f"error: {error[:60]}", style="red")
                text.append("\n")  # blank detail row — holds the slot height

        return text
