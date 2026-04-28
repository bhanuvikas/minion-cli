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
from .models import Artifact, Task, TaskStatus

# ─── Module-level shared state (lock-protected) ───────────────────────────────

_tasks: dict[str, Task] = {}
_task_done_events: dict[str, threading.Event] = {}
_tasks_lock = threading.Lock()

# Pending approval requests: task_id → {event, prompt, response}
_pending_approvals: dict[str, dict] = {}
_approvals_lock = threading.Lock()


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


def _make_confirm_callback(task_id: str) -> Callable[[str], bool]:
    """Return a callback that pauses the agent with input-required and waits for approval."""
    def _confirm(prompt: str) -> bool:
        _update_task_status(
            task_id, TaskStatus.INPUT_REQUIRED,
            input_data={"prompt": prompt, "type": "confirm"},
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

        Continuation body: {"task_id": "<id>", "message": "yes"|"no"}
        New task body:     {"message": "<task text>"}
        """
        # ── Task continuation (input-required approval) ────────────────────────
        if "task_id" in body:
            task_id = body["task_id"]
            with _approvals_lock:
                entry = _pending_approvals.pop(task_id, None)
            if entry is None:
                self._send_json(404, {"error": f"task '{task_id}' not awaiting approval"})
                return
            response = body.get("message", "").strip().lower() in ("yes", "y", "true", "1")
            entry["response"] = response
            entry["event"].set()
            _update_task_status(task_id, TaskStatus.WORKING)
            self._send_json(200, {"id": task_id, "status": TaskStatus.WORKING.value})
            return

        # ── New task ──────────────────────────────────────────────────────────
        message = body.get("message", "")
        if not message:
            self._send_json(400, {"error": "request body must include 'message'"})
            return

        task_id = str(uuid.uuid4())
        task = Task(id=task_id, status=TaskStatus.SUBMITTED, input_message=message)
        done_event = _store_task(task)

        remote_addr = self.client_address[0] if self.client_address else ""
        get_tracer().emit(
            "a2a_server_request",
            task_id=task_id,
            task=message,
            remote_addr=remote_addr,
        )

        confirm_cb = _make_confirm_callback(task_id)
        self.__class__._executor.submit(self._run_task, task_id, message, done_event, confirm_cb)
        self._send_json(202, {"id": task_id, "status": TaskStatus.SUBMITTED.value})

    def _handle_get_task(self, task_id: str) -> None:
        """GET /tasks/{id} — return current task state."""
        task = _get_task(task_id)
        if task is None:
            self._send_json(404, {"error": f"task '{task_id}' not found"})
            return
        d = task.to_dict()
        # Include input_data for input-required state
        if task.status == TaskStatus.INPUT_REQUIRED:
            input_data = getattr(task, "input_data", None)
            if input_data:
                d["input"] = input_data
        self._send_json(200, d)

    def _handle_subscribe(self, body: dict) -> None:
        """POST /tasks/sendSubscribe — create task, stream SSE events until completion."""
        message = body.get("message", "")
        if not message:
            self._send_json(400, {"error": "request body must include 'message'"})
            return

        task_id = str(uuid.uuid4())
        task = Task(id=task_id, status=TaskStatus.SUBMITTED, input_message=message)
        done_event = _store_task(task)

        remote_addr = self.client_address[0] if self.client_address else ""
        get_tracer().emit(
            "a2a_server_request",
            task_id=task_id,
            task=message,
            remote_addr=remote_addr,
        )

        # Set response headers for SSE stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        # Send submitted event immediately
        self._write_sse({"id": task_id, "status": TaskStatus.SUBMITTED.value})

        # Transition to working and send working event before submitting to pool
        _update_task_status(task_id, TaskStatus.WORKING)
        self._write_sse({"id": task_id, "status": TaskStatus.WORKING.value})

        # Run agent in thread pool
        confirm_cb = _make_confirm_callback(task_id)
        self.__class__._executor.submit(self._run_task, task_id, message, done_event, confirm_cb)

        # Block until done; periodically flush input-required SSE events
        while not done_event.wait(timeout=1.0):
            task_snap = _get_task(task_id)
            if task_snap and task_snap.status == TaskStatus.INPUT_REQUIRED:
                d = task_snap.to_dict()
                input_data = getattr(task_snap, "input_data", None)
                if input_data:
                    d["input"] = input_data
                self._write_sse(d)

        final_task = _get_task(task_id)
        if final_task is not None:
            self._write_sse(final_task.to_dict())

    # ── Helpers ───────────────────────────────────────────────────────────────

    @classmethod
    def _run_task(cls, task_id: str, message: str, done_event: threading.Event,
                  confirm_cb: Callable[[str], bool]) -> None:
        """Execute the agent for a task and update task state when done."""
        _update_task_status(task_id, TaskStatus.WORKING)
        try:
            result = cls.agent_runner(message, confirm_callback=confirm_cb)
            _update_task_status(task_id, TaskStatus.COMPLETED, artifact_text=result)
        except Exception as e:
            _update_task_status(task_id, TaskStatus.FAILED, error=str(e))
        finally:
            done_event.set()

    def _write_sse(self, payload: dict) -> None:
        line = f"data: {json.dumps(payload)}\n\n"
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
