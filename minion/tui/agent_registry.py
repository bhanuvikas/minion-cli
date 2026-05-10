"""SubagentRegistry — thread-safe state store for the subagent inspection panel.

Updated by a callback wrapper around the display callback in _execute_parallel_agents_async.
The inspector panel reads from this registry to render per-agent transcripts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .messages import InspectorUpdated


@dataclass
class SubagentState:
    id: str
    label: str
    task: str
    role: str
    status: str = "pending"          # pending | running | complete | error
    messages: list[dict] = field(default_factory=list)   # snapshots from turn_end
    output_tokens: int = 0
    latency_ms: int = 0
    preview: str = ""
    error: str = ""


class SubagentRegistry:
    """Thread-safe registry of active/recent subagents."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, SubagentState] = {}
        self._order: list[str] = []
        self._post_message_fn: Optional[Callable] = None

    def set_post_message(self, fn: Callable) -> None:
        """Wire the Textual app's post_message() for thread-safe UI updates."""
        self._post_message_fn = fn

    # Keep old name as alias so any existing callers (tests, etc.) still work.
    def set_invalidate(self, fn: Callable) -> None:
        self.set_post_message(fn)

    def register(self, id: str, label: str, task: str, role: str) -> None:
        with self._lock:
            if id not in self._states:
                self._states[id] = SubagentState(id=id, label=label, task=task, role=role)
                self._order.append(id)

    def update(self, id: str, event: str, **data) -> None:
        with self._lock:
            if id not in self._states:
                return
            state = self._states[id]
            if event == "running":
                state.status = "running"
            elif event == "turn_end":
                state.messages = data.get("messages", state.messages)
            elif event == "complete":
                state.status = "complete"
                state.latency_ms = data.get("latency_ms", 0)
                state.preview = data.get("preview", "")
            elif event == "error":
                state.status = "error"
                state.error = data.get("error", "")
        if self._post_message_fn is not None:
            self._post_message_fn(InspectorUpdated())

    def clear(self) -> None:
        with self._lock:
            self._states.clear()
            self._order.clear()

    def get_all(self) -> list[SubagentState]:
        with self._lock:
            return [SubagentState(**vars(self._states[k])) for k in self._order if k in self._states]

    def get(self, id: str) -> Optional[SubagentState]:
        with self._lock:
            s = self._states.get(id)
            return SubagentState(**vars(s)) if s else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)


_registry = SubagentRegistry()


def get_registry() -> SubagentRegistry:
    return _registry
