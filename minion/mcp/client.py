"""MCPClient — stdio transport for MCP (subprocess + background reader thread).

Implements MCPClientBase using a subprocess and a daemon reader thread.
The thread reads stdout continuously:
  - Messages with "id" → routed to the per-request queue in _response_queues
  - Messages without "id" (notifications) → dispatched to _handle_notification()

Race safety: _send_request() registers its queue under _state_lock before
writing, so the thread can never set _thread_exited (and miss the queue) between
the "is thread alive?" check and the queue registration.

Backward-compatible re-exports: MCPTool, MCPResource, MCPPromptArg, MCPPrompt
are defined in base.py but re-exported here so existing imports continue to work.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from typing import Optional

from .base import (
    MCPClientBase,
    MCPTool,         # noqa: F401 — re-exported for backward compat
    MCPResource,     # noqa: F401
    MCPPromptArg,    # noqa: F401
    MCPPrompt,       # noqa: F401
)
from .config import MCPServerConfig


class MCPClient(MCPClientBase):
    """MCP client over stdio (subprocess + background reader thread).

    Lifecycle:
        client = MCPClient("notes", config)
        client.notification_callback = some_fn   # optional
        client.connect()
        result = client.call_tool("create_note", {...})
        client.shutdown()
    """

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        super().__init__(name, config)
        self._process: Optional[subprocess.Popen] = None
        self._read_thread: Optional[threading.Thread] = None
        self._response_queues: dict[int, queue.Queue] = {}
        self._thread_exited: bool = False
        self._state_lock = threading.Lock()

    # ── MCPClientBase abstract methods ────────────────────────────────────────

    def _initialize_transport(self) -> None:
        """Spawn server subprocess and start background reader thread."""
        env = {**os.environ, **self.config.env}
        try:
            self._process = subprocess.Popen(
                self.config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise RuntimeError(f"failed to start '{self.config.command[0]}': {e}") from e

        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.name = f"mcp-reader-{self.name}"
        self._read_thread.start()

    def _after_handshake(self) -> None:
        """No-op for stdio: the reader thread is already running."""

    def _send_request(self, method: str, params: dict | None = None) -> dict:
        """Register a response queue, write request, block until thread delivers."""
        req_id = self._next_id
        self._next_id += 1
        payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        q: queue.Queue = queue.Queue()

        with self._state_lock:
            if self._thread_exited:
                raise IOError("server closed stdout unexpectedly")
            self._response_queues[req_id] = q

        self._write(payload)
        try:
            response = q.get(timeout=30)
        finally:
            self._response_queues.pop(req_id, None)

        if response is None:
            raise IOError("server closed stdout unexpectedly")
        if "error" in response:
            err = response["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"server error: {msg}")
        return response

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Write a JSON-RPC notification to the server's stdin."""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def shutdown(self) -> None:
        """Terminate subprocess and join reader thread. Never raises."""
        if self._process is None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.terminate()
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
        except Exception:
            pass
        finally:
            self._process = None
        if self._read_thread is not None:
            self._read_thread.join(timeout=2)
            self._read_thread = None

    # ── Background reader thread ──────────────────────────────────────────────

    def _read_loop(self) -> None:
        """Daemon thread: read stdout, route responses to queues, notifications to callback."""
        while True:
            if self._process is None or self._process.stdout is None:
                break
            try:
                line = self._process.stdout.readline()
            except Exception:
                break
            if not line:
                break  # EOF — subprocess exited
            try:
                parsed = json.loads(line.decode())
            except json.JSONDecodeError:
                continue

            msg_id = parsed.get("id")
            if msg_id is not None:
                q = self._response_queues.get(msg_id)
                if q is not None:
                    q.put(parsed)
            else:
                self._handle_notification(parsed)

        # Signal all waiting requests that the server is gone
        with self._state_lock:
            self._thread_exited = True
            queues_to_notify = list(self._response_queues.values())
        for q in queues_to_notify:
            q.put(None)

    def _write(self, obj: dict) -> None:
        if self._process is None or self._process.stdin is None:
            raise IOError("server process is not running")
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        self._process.stdin.write(line.encode())
        self._process.stdin.flush()
