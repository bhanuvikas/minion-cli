"""Live grouped status display for parallel agent execution.

When multiple spawn_agent calls run concurrently, each agent gets its own
slot in a live-updating terminal section rather than interleaving output.

Thread-local callback mechanism: _execute_parallel_agents sets a callback
on each worker thread before starting. run_agent reads it and routes status
output through the callback instead of console.print(). This requires zero
changes to run_prompt() or ToolExecutor — only run_agent is aware.
"""

import threading
from typing import Callable, Optional

from rich.live import Live
from rich.text import Text


def _format_tool_args(inputs: dict) -> str:
    """Return a brief key='value' snippet from tool inputs for the slot detail line."""
    if not inputs:
        return ""
    for k, v in inputs.items():
        if isinstance(v, str):
            return f"{k}='{v[:40]}'"
        return f"{k}={str(v)[:40]}"
    return ""

# ─── Thread-local callback registry ──────────────────────────────────────────

_thread_local = threading.local()


def get_agent_display_callback() -> Optional[Callable]:
    """Return the live display callback for the current thread, or None."""
    return getattr(_thread_local, "display_callback", None)


def set_agent_display_callback(callback: Optional[Callable]) -> None:
    """Set (or clear) the live display callback for the current thread."""
    _thread_local.display_callback = callback


# ─── Live display ─────────────────────────────────────────────────────────────

class AgentLiveDisplay:
    """Thread-safe live status panel for parallel agent execution.

    Each agent gets a slot that updates in place:

        ⚙  [researcher]  running...
        ✓  [researcher]  complete (16.4s)
           └─  preview of result...

        ⚙  [reviewer]  running...
        ✓  [reviewer]  complete (41.6s)
           └─  preview of result...

    Usage:
        display = AgentLiveDisplay()
        with display:
            callback = display.make_callback("researcher")
            # pass callback to run_agent via set_agent_display_callback()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, dict] = {}  # role → state dict
        self._order: list[str] = []          # insertion order for stable rendering
        self._live = Live(Text(""), refresh_per_second=8, transient=False)

    def __enter__(self) -> "AgentLiveDisplay":
        self._live.__enter__()
        return self

    def __exit__(self, *args) -> None:
        self._live.__exit__(*args)

    def pre_register(self, roles: list[str]) -> None:
        """Register all agent slots as 'pending' before the Live context starts.

        Call this BEFORE entering the `with display:` context so the display
        has a fixed height from the first render — Rich Live never needs to
        resize the live area, eliminating flicker as agents complete.
        """
        with self._lock:
            for role in roles:
                if role not in self._states:
                    self._states[role] = {"status": "pending"}
                    self._order.append(role)

    def make_callback(self, role: str) -> Callable:
        """Return an event callback bound to a specific role's display slot."""
        def callback(event: str, **data) -> None:
            with self._lock:
                if role not in self._states:
                    self._states[role] = {}
                    self._order.append(role)
                state = self._states[role]
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

    def _render(self) -> Text:
        """Build the current live display as a Rich Text object.

        Every slot always occupies exactly 2 lines (status row + detail row).
        Keeping the height constant prevents Rich from having to resize the live
        area when agents complete and add preview text — eliminating flicker.
        """
        text = Text()
        first = True
        for role in self._order:
            if not first:
                text.append("\n")
            first = False
            state = self._states.get(role, {})
            status = state.get("status", "pending")

            if status == "pending":
                text.append("  ·  ", style="dim")
                text.append(f"[{role}]", style="bold")
                text.append("  waiting...", style="dim")
                text.append("\n")  # blank detail row — holds the slot height

            elif status == "running":
                text.append("  ⚙  ", style="dim")
                text.append(f"[{role}]", style="bold")
                text.append("  running...", style="dim")
                last_activity = state.get("last_activity", "")
                # Always emit the detail line (even if blank) to keep slot height stable.
                # Replace any stray newlines so the row never expands beyond one line.
                last_activity_line = last_activity.replace("\n", " ").replace("\r", "")[:90]
                text.append(f"\n     {last_activity_line}", style="dim")

            elif status == "complete":
                latency = state.get("latency_ms", 0) / 1000
                text.append("  ✓  ", style="dim")
                text.append(f"[{role}]", style="bold")
                text.append(f"  complete ({latency:.1f}s)", style="dim")
                preview = state.get("preview", "")
                text.append(f"\n     └─  {preview[:100]}", style="dim")

            elif status == "error":
                error = state.get("error", "")
                text.append("  ✗  ", style="dim")
                text.append(f"[{role}]", style="bold")
                text.append(f"  error: {error[:60]}", style="red")
                text.append("\n")  # blank detail row — holds the slot height

        return text
