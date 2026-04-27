"""A2A HTTP client — sends tasks to a single remote A2A agent.

Uses http.client from stdlib (zero new deps) — same approach as the MCP
HTTP client. Reuses SSEParser from minion/mcp/sse.py for event-stream parsing.

Protocol subset implemented:
    GET  /.well-known/agent.json    — fetch Agent Card
    POST /tasks/send                — submit task, poll until done
    POST /tasks/sendSubscribe       — submit task, read SSE stream until done

Task lifecycle: submitted → working → completed / failed

Wire format:
    Request body:   {"message": "<task text>"}
    Poll response:  {"id": "...", "status": "...", "artifacts": [{"text": "..."}]}
    SSE data line:  data: {"id": "...", "status": "...", "artifacts": [...]}
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.parse
from typing import Callable, Optional

from ..mcp.sse import SSEParser
from .models import AgentCard, Artifact, Task, TaskStatus


class A2AError(Exception):
    """Raised on HTTP errors, timeouts, or malformed A2A responses."""


class A2AClient:
    """HTTP client for one named remote A2A agent.

    Stateless between calls — no persistent connection. Each send_task() /
    fetch_agent_card() call opens a fresh connection and closes it when done.
    """

    def __init__(self, name: str, url: str, timeout_seconds: int = 60) -> None:
        self.name = name
        self._timeout = timeout_seconds
        parsed = urllib.parse.urlparse(url)
        self._scheme = parsed.scheme          # "http" or "https"
        self._netloc = parsed.netloc          # "host:port" or "host"
        self._base_path = parsed.path.rstrip("/")  # "" or "/prefix"

    # ─── Connection factory ────────────────────────────────────────────────────

    def _make_connection(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._netloc, timeout=self._timeout)
        return http.client.HTTPConnection(self._netloc, timeout=self._timeout)

    def _path(self, suffix: str) -> str:
        return self._base_path + suffix

    # ─── Agent Card ────────────────────────────────────────────────────────────

    def fetch_agent_card(self) -> Optional[AgentCard]:
        """GET /.well-known/agent.json → AgentCard, or None on failure."""
        try:
            conn = self._make_connection()
            conn.request(
                "GET",
                self._path("/.well-known/agent.json"),
                headers={"Accept": "application/json"},
            )
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
            return AgentCard.from_dict(data)
        except Exception:
            return None

    # ─── Task submission (polling) ─────────────────────────────────────────────

    def send_task(self, task_text: str) -> str:
        """Submit a task and poll GET /tasks/{id} until completed or failed.

        Returns the artifact text on success.
        Raises A2AError on HTTP failure, task failure, or timeout.
        """
        task_id = self._submit_task(task_text)
        task = self._poll_until_done(task_id)

        if task.status == TaskStatus.FAILED:
            raise A2AError(task.error or "Remote task failed with no error message.")

        if task.artifacts:
            return task.artifacts[0].text
        return ""

    def _submit_task(self, task_text: str) -> str:
        """POST /tasks/send → task_id."""
        body = json.dumps({"message": task_text}).encode("utf-8")
        try:
            conn = self._make_connection()
            conn.request(
                "POST",
                self._path("/tasks/send"),
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise A2AError(f"Failed to submit task to '{self.name}': {e}") from e

        if resp.status not in (200, 201, 202):
            raise A2AError(
                f"Task submission failed for '{self.name}': HTTP {resp.status}"
            )

        task_id = data.get("id")
        if not task_id:
            raise A2AError(f"No task ID in response from '{self.name}'")
        return task_id

    def _poll_until_done(self, task_id: str) -> Task:
        """GET /tasks/{id} every 0.5s until status is completed or failed."""
        deadline = time.monotonic() + self._timeout
        while True:
            if time.monotonic() >= deadline:
                raise A2AError(
                    f"Task '{task_id}' from '{self.name}' timed out after {self._timeout}s"
                )
            try:
                conn = self._make_connection()
                conn.request(
                    "GET",
                    self._path(f"/tasks/{task_id}"),
                    headers={"Accept": "application/json"},
                )
                resp = conn.getresponse()
                data = json.loads(resp.read().decode("utf-8"))
            except A2AError:
                raise
            except Exception as e:
                raise A2AError(f"Failed to poll task '{task_id}' from '{self.name}': {e}") from e

            task = Task.from_dict(data)
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                return task
            time.sleep(0.5)

    # ─── Task submission (SSE streaming) ──────────────────────────────────────

    def send_task_streaming(
        self,
        task_text: str,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        """POST /tasks/sendSubscribe and read SSE stream until completion.

        Calls on_status(status_str) for each SSE event received (e.g. "working",
        "completed"). Returns the artifact text on success.
        Raises A2AError on failure or timeout.
        """
        body = json.dumps({"message": task_text}).encode("utf-8")
        try:
            conn = self._make_connection()
            conn.request(
                "POST",
                self._path("/tasks/sendSubscribe"),
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
        except Exception as e:
            raise A2AError(f"Failed to subscribe to task on '{self.name}': {e}") from e

        if resp.status != 200:
            raise A2AError(
                f"Task subscribe failed for '{self.name}': HTTP {resp.status}"
            )

        for event in SSEParser.iter_events(resp):
            try:
                data = json.loads(event.data)
            except json.JSONDecodeError:
                continue

            status = data.get("status", "")
            if on_status is not None:
                on_status(status)

            if status == TaskStatus.COMPLETED.value:
                artifacts = data.get("artifacts", [])
                if artifacts:
                    return artifacts[0].get("text", "")
                return ""

            if status == TaskStatus.FAILED.value:
                raise A2AError(data.get("error") or "Remote task failed.")

        raise A2AError(f"SSE stream from '{self.name}' ended without a final status event.")
