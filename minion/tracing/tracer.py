"""Session-scoped JSONL tracer for Nefario observability.

Usage:
    from minion.tracing import get_tracer, init_tracer

    # At session start (cli.py):
    init_tracer(session_id="abc-123")

    # Anywhere in the codebase:
    get_tracer().emit("user_turn", text="hello")

    # Safe to call without init — returns NullTracer (no-op):
    get_tracer().emit("user_turn", text="hello")  # no-op if not initialized

    # At session end (repl.py):
    get_tracer().finalize()
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TRACES_DIR = Path.home() / ".minion" / "traces"
MAX_SESSIONS = 50

# Module-level singleton — one tracer per process lifetime.
_tracer: Optional["Tracer"] = None


# ─── Truncation helpers ────────────────────────────────────────────────────────

def _truncate(event_type: str, data: dict) -> dict:
    """No-op — all field values are stored in full for debugging visibility."""
    return dict(data)


# ─── No-op tracer ──────────────────────────────────────────────────────────────

class NullTracer:
    """Returned by get_tracer() when tracing is disabled or not initialized.

    All methods are no-ops so instrumentation callsites never need to check
    whether a real tracer exists.
    """

    session_id: Optional[str] = None

    def emit(self, event_type: str, **data) -> None:  # noqa: ARG002
        pass

    def finalize(self) -> None:
        pass


# ─── Real tracer ───────────────────────────────────────────────────────────────

class Tracer:
    """Thread-safe JSONL trace writer for one REPL session.

    Each call to emit() appends one JSON line to the session file. Token
    counters and turn counts are accumulated internally so finalize() can
    emit a complete session_end event without requiring callers to pass
    aggregate state.
    """

    def __init__(self, session_id: str, path: Path) -> None:
        self._session_id = session_id
        self._path = path
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        # Accumulated counters updated inside emit()
        self._turn_count = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_calls = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    def emit(self, event_type: str, **data) -> None:
        """Append one JSONL event to the trace file.

        Truncation is applied here — callers pass raw values and the tracer
        caps them before writing. Counter accumulation also happens here.
        """
        # Update internal counters before truncation changes the keys
        if event_type == "user_turn":
            self._turn_count += 1
        elif event_type == "tool_call":
            self._tool_calls += 1
        elif event_type == "llm_response":
            self._input_tokens += int(data.get("input_tokens", 0))
            self._output_tokens += int(data.get("output_tokens", 0))

        event = {
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self._session_id,
            "data": _truncate(event_type, data),
        }
        line = json.dumps(event) + "\n"
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)

    def finalize(self) -> None:
        """Emit session_end with accumulated session-wide counters."""
        self.emit(
            "session_end",
            total_turns=self._turn_count,
            total_input_tokens=self._input_tokens,
            total_output_tokens=self._output_tokens,
            total_tool_calls=self._tool_calls,
            duration_seconds=round(time.monotonic() - self._started_at, 2),
        )


# ─── Module-level accessors ────────────────────────────────────────────────────

_null_tracer = NullTracer()


def get_tracer() -> "Tracer | NullTracer":
    """Return the active session tracer, or a NullTracer if none is initialized."""
    return _tracer if _tracer is not None else _null_tracer


def init_tracer(
    session_id: str,
    traces_dir: Path = TRACES_DIR,
) -> Tracer:
    """Initialize the module-level tracer for a new REPL session.

    Creates the traces directory if needed, trims old sessions to MAX_SESSIONS,
    and sets the module-level singleton. Called once from cli.py before
    run_repl().
    """
    global _tracer
    traces_dir.mkdir(parents=True, exist_ok=True)
    _trim_sessions(traces_dir)
    path = traces_dir / f"{session_id}.jsonl"
    _tracer = Tracer(session_id=session_id, path=path)
    return _tracer


def _trim_sessions(traces_dir: Path, keep: int = MAX_SESSIONS) -> None:
    """Delete oldest JSONL files when the trace directory exceeds `keep` files."""
    files = sorted(traces_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep]:
        old.unlink(missing_ok=True)
