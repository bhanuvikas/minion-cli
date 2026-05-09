"""A2A HTTP server — exposes minion as a remote A2A agent.

Uses http.server.ThreadingHTTPServer from stdlib (zero new deps). Each incoming
HTTP request gets its own thread. Agent task execution runs in a ThreadPoolExecutor
so the HTTP handler returns immediately for polling mode (POST /tasks/send), or
blocks with SSE streaming for the subscribe mode (POST /tasks/sendSubscribe).

Endpoints:
    GET  /.well-known/agent.json  — Agent Card (capability advertisement)
    POST /tasks/send              — Submit new task; or continue existing (task_id in body)
    GET  /tasks/{id}              — Poll task status + artifact
    POST /tasks/sendSubscribe     — Submit task + SSE stream until completion

Spec-compliant human approval:
    When the agent needs approval for a dangerous tool, the task transitions to
    "input-required" with an {input: {prompt, type}} payload. The orchestrator
    surfaces the prompt to its user, collects the decision, and sends a
    continuation via POST /tasks/send with {task_id, message: "yes"|"no"}.
    The server resumes the paused agent coroutine.

Task state is kept in memory (dict[str, Task] with a threading.Lock).
No persistence across server restarts — Phase 11 scope.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from ..tracing import get_tracer
from .card import generate_agent_card
from .models import Artifact, Task, TaskStatus, _extract_text_from_message

# ─── Module-level shared state (lock-protected) ───────────────────────────────

_tasks: dict[str, Task] = {}
_task_done_events: dict[str, threading.Event] = {}
_tasks_lock = threading.Lock()

# Pending approval requests: task_id → {event, prompt, response}
_pending_approvals: dict[str, dict] = {}
_approvals_lock = threading.Lock()

# Context history: contextId → list of {request, response} turns (capped at 20)
_context_history: dict[str, list[dict]] = {}
_context_lock = threading.Lock()
_MAX_HISTORY_TURNS = 20


def _get_context_history(context_id: str) -> list[dict]:
    with _context_lock:
        return list(_context_history.get(context_id, []))


def _append_context_history(context_id: str, request: str, response: str) -> None:
    with _context_lock:
        turns = _context_history.setdefault(context_id, [])
        turns.append({"request": request, "response": response})
        if len(turns) > _MAX_HISTORY_TURNS:
            turns.pop(0)


def _build_message_with_history(message: str, context_id: Optional[str]) -> str:
    """Prepend prior session turns to the message so the agent has full context."""
    if not context_id:
        return message
    history = _get_context_history(context_id)
    if not history:
        return message
    n = len(history)
    lines = [f"[Session context — {n} prior turn{'s' if n > 1 else ''}:]", ""]
    for i, turn in enumerate(history, 1):
        lines.append(f"Turn {i} — User: {turn['request']}")
        lines.append(f"Turn {i} — Agent: {turn['response']}")
        lines.append("")
    lines += ["--- Current request ---", message]
    return "\n".join(lines)


def _store_task(task: Task) -> threading.Event:
    """Register a new task and return its completion Event."""
    done = threading.Event()
    with _tasks_lock:
        _tasks[task.id] = task
        _task_done_events[task.id] = done
    return done


def _get_task(task_id: str) -> Optional[Task]:
    with _tasks_lock:
        return _tasks.get(task_id)


def _update_task_status(task_id: str, status: TaskStatus,
                        artifact_text: Optional[str] = None,
                        error: Optional[str] = None,
                        input_data: Optional[dict] = None) -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if task is None:
            return
        task.status = status
        if artifact_text is not None:
            task.artifacts = [Artifact(text=artifact_text)]
        if error is not None:
            task.error = error
        if input_data is not None:
            task.input_data = input_data  # type: ignore[attr-defined]


def _make_confirm_callback(task_id: str) -> Callable[[str, str], bool]:
    """Return a callback that pauses the agent with input-required and waits for approval.

    Signature: (question: str, detail: str = "") -> bool
      question — short one-liner for the y/N prompt
      detail   — multi-line context (tool inputs, content preview)

    Both are stored as text parts in input_data so the orchestrator can display them
    as a spec-compliant multi-part A2A Message in the input-required status event.
    """
    def _confirm(question: str, detail: str = "") -> bool:
        parts = [{"type": "text", "text": question}]
        if detail:
            parts.append({"type": "text", "text": detail})
        _update_task_status(
            task_id, TaskStatus.INPUT_REQUIRED,
            input_data={"parts": parts, "type": "confirm"},
        )
        entry: dict = {"event": threading.Event(), "response": None}
        with _approvals_lock:
            _pending_approvals[task_id] = entry

        # Block until orchestrator sends a continuation (or 5-minute timeout)
        entry["event"].wait(timeout=300)
        return bool(entry.get("response"))

    return _confirm


# ─── HTTP handler ─────────────────────────────────────────────────────────────

class _A2AHandler(BaseHTTPRequestHandler):
    """HTTP handler for A2A protocol endpoints.

    The server sets class attributes before starting, so all handler instances
    share them via the class.
    """
    agent_runner: Callable[..., str]     # fn(task_text, *, confirm_callback=None) -> str
    host: str
    port: int
    _executor: ThreadPoolExecutor        # shared thread pool

    # ── Routing ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        if self.path == "/.well-known/agent.json":
            self._serve_agent_card()
        elif self.path.startswith("/tasks/") and len(self.path) > 7:
            task_id = self.path[7:].strip("/")
            self._handle_get_task(task_id)
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if self.path.startswith("/tasks/") and len(self.path) > 7:
            task_id = self.path[7:].strip("/")
            self._handle_cancel_task(task_id)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if self.path == "/tasks/send":
            self._handle_send(body)
        elif self.path == "/tasks/sendSubscribe":
            self._handle_subscribe(body)
        elif self.path == "/tasks/cancel":
            task_id = body.get("id", "")
            if task_id:
                self._handle_cancel_task(task_id)
            else:
                self._send_json(400, {"error": "request body must include 'id'"})
        else:
            self._send_json(404, {"error": "not found"})

    # ── Endpoint implementations ───────────────────────────────────────────────

    def _serve_agent_card(self) -> None:
        card = generate_agent_card(self.__class__.host, self.__class__.port)
        payload = card.to_json().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_send(self, body: dict) -> None:
        """POST /tasks/send — create new task OR continue an existing one.

        Continuation body (spec): {"id": "<task-id>", "message": Message}
        New task body (spec):     {"message": Message}
        where Message = {"role": "user", "parts": [{"type": "text", "text": "..."}]}
        """
        # ── Task continuation (input-required approval) — spec uses "id" field ─
        if "id" in body and _get_task(body["id"]) is not None:
            task_id = body["id"]
            with _approvals_lock:
                entry = _pending_approvals.pop(task_id, None)
            if entry is None:
                self._send_json(404, {"error": f"task '{task_id}' not awaiting approval"})
                return
            raw_msg = body.get("message", "")
            answer_text = _extract_text_from_message(raw_msg).strip().lower()
            entry["response"] = answer_text in ("yes", "y", "true", "1")
            entry["event"].set()
            _update_task_status(task_id, TaskStatus.WORKING)
            task = _get_task(task_id)
            self._send_json(200, task.to_dict() if task else {"id": task_id})
            return

        # ── New task ──────────────────────────────────────────────────────────
        raw_message = body.get("message", "")
        message = _extract_text_from_message(raw_message)
        if not message:
            self._send_json(400, {"error": "request body must include 'message'"})
            return

        context_id: Optional[str] = body.get("contextId") or None
        task_id = str(uuid.uuid4())
        task = Task(id=task_id, status=TaskStatus.SUBMITTED, input_message=message,
                    context_id=context_id)
        done_event = _store_task(task)

        remote_addr = self.client_address[0] if self.client_address else ""
        get_tracer().emit(
            "a2a_server_request",
            task_id=task_id,
            task=message,
            remote_addr=remote_addr,
        )

        # Capture submitted state before thread pool can advance the status
        submitted_dict = task.to_dict()
        confirm_cb = _make_confirm_callback(task_id)
        self.__class__._executor.submit(self._run_task, task_id, message, done_event, confirm_cb, context_id)
        self._send_json(202, submitted_dict)

    def _handle_get_task(self, task_id: str) -> None:
        """GET /tasks/{id} — return current task state."""
        task = _get_task(task_id)
        if task is None:
            self._send_json(404, {"error": f"task '{task_id}' not found"})
            return
        d = task.to_dict()
        # For input-required: embed approval prompt in status.message per spec
        if task.status == TaskStatus.INPUT_REQUIRED:
            input_data = getattr(task, "input_data", None)
            if input_data:
                parts = input_data.get("parts") or [
                    {"type": "text", "text": input_data.get("prompt", "Approve action?")}
                ]
                d["status"]["message"] = {"role": "agent", "parts": parts}
        self._send_json(200, d)

    def _handle_subscribe(self, body: dict) -> None:
        """POST /tasks/sendSubscribe — create task, stream spec SSE events until completion.

        Emits TaskStatusUpdateEvent and TaskArtifactUpdateEvent per A2A spec.
        """
        raw_message = body.get("message", "")
        message = _extract_text_from_message(raw_message)
        if not message:
            self._send_json(400, {"error": "request body must include 'message'"})
            return

        context_id: Optional[str] = body.get("contextId") or None
        task_id = str(uuid.uuid4())
        task = Task(id=task_id, status=TaskStatus.SUBMITTED, input_message=message,
                    context_id=context_id)
        done_event = _store_task(task)

        remote_addr = self.client_address[0] if self.client_address else ""
        get_tracer().emit(
            "a2a_server_request",
            task_id=task_id,
            task=message,
            remote_addr=remote_addr,
        )

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        self._write_sse(task.status_event(final=False), event="task-status-update")

        _update_task_status(task_id, TaskStatus.WORKING)
        working_snap = _get_task(task_id)
        if working_snap:
            self._write_sse(working_snap.status_event(final=False), event="task-status-update")

        confirm_cb = _make_confirm_callback(task_id)
        self.__class__._executor.submit(self._run_task, task_id, message, done_event, confirm_cb, context_id)

        # Poll until done; emit input-required status events as they occur
        while not done_event.wait(timeout=1.0):
            task_snap = _get_task(task_id)
            if task_snap and task_snap.status == TaskStatus.INPUT_REQUIRED:
                ev = task_snap.status_event(final=False)
                input_data = getattr(task_snap, "input_data", None)
                if input_data:
                    parts = input_data.get("parts") or [
                        {"type": "text", "text": input_data.get("prompt", "Approve action?")}
                    ]
                    ev["status"]["message"] = {"role": "agent", "parts": parts}
                self._write_sse(ev, event="task-status-update")

        final_task = _get_task(task_id)
        if final_task is not None:
            # Emit artifact event then final status event
            for artifact in final_task.artifacts:
                self._write_sse(final_task.artifact_event(artifact, final=False), event="task-artifact-update")
            self._write_sse(final_task.status_event(final=True), event="task-status-update")

    def _handle_cancel_task(self, task_id: str) -> None:
        """Cancel a task that is submitted or working."""
        task = _get_task(task_id)
        if task is None:
            self._send_json(404, {"error": f"task '{task_id}' not found"})
            return
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED):
            self._send_json(409, {"error": f"task '{task_id}' is already in terminal state"})
            return
        _update_task_status(task_id, TaskStatus.CANCELED)
        # Release any pending approval so the worker thread unblocks
        with _approvals_lock:
            entry = _pending_approvals.pop(task_id, None)
        if entry:
            entry["response"] = False
            entry["event"].set()
        task = _get_task(task_id)
        self._send_json(200, task.to_dict() if task else {"id": task_id})

    # ── Helpers ───────────────────────────────────────────────────────────────

    @classmethod
    def _run_task(cls, task_id: str, message: str, done_event: threading.Event,
                  confirm_cb: Callable[[str, str], bool],
                  context_id: Optional[str] = None) -> None:
        """Execute the agent for a task and update task state when done.

        Injects prior session history (if contextId is set) into the message
        before calling the agent, then stores the exchange in context history.
        """
        full_message = _build_message_with_history(message, context_id)
        _update_task_status(task_id, TaskStatus.WORKING)
        try:
            result = cls.agent_runner(full_message, confirm_callback=confirm_cb)
            # Guard: don't overwrite CANCELED with COMPLETED if the task was
            # canceled while the agent was still running.
            task = _get_task(task_id)
            if task is not None and task.status != TaskStatus.CANCELED:
                _update_task_status(task_id, TaskStatus.COMPLETED, artifact_text=result)
                if context_id:
                    _append_context_history(context_id, message, result)
        except Exception as e:
            task = _get_task(task_id)
            if task is not None and task.status != TaskStatus.CANCELED:
                _update_task_status(task_id, TaskStatus.FAILED, error=str(e))
        finally:
            done_event.set()

    def _write_sse(self, payload: dict, event: str = "") -> None:
        """Write one SSE event. Emits 'event:' line when event name is given."""
        parts = []
        if event:
            parts.append(f"event: {event}\n")
        parts.append(f"data: {json.dumps(payload)}\n\n")
        line = "".join(parts)
        try:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected early

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass  # suppress default access log — Nefario traces replace it


# ─── Server ───────────────────────────────────────────────────────────────────

class A2AServer:
    """ThreadingHTTPServer that exposes minion as a remote A2A agent.

    Usage:
        server = A2AServer(host="localhost", port=8080, agent_runner=runner)
        server.start()  # blocks until Ctrl+C or server.stop()

    agent_runner signature:
        def runner(task_text: str, *, confirm_callback=None) -> str:
            ...
    """

    def __init__(
        self,
        host: str,
        port: int,
        agent_runner: Callable[..., str],
        max_workers: int = 4,
    ) -> None:
        self.host = host
        self.port = port
        # Inject server-specific state as class attributes on the handler
        _A2AHandler.agent_runner = staticmethod(agent_runner)
        _A2AHandler.host = host
        _A2AHandler.port = port
        _A2AHandler._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._httpd: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        """Start the server (blocks until stop() is called or KeyboardInterrupt)."""
        self._httpd = ThreadingHTTPServer((self.host, self.port), _A2AHandler)
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._httpd.server_close()
            _A2AHandler._executor.shutdown(wait=False)

    def stop(self) -> None:
        """Signal the server to stop (safe to call from another thread)."""
        if self._httpd is not None:
            self._httpd.shutdown()
