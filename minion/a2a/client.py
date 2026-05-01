"""A2A HTTP client — sends tasks to a single remote A2A agent.

Sync path: uses http.client from stdlib (zero new deps).
Async path: uses httpx.AsyncClient (replaces time.sleep with asyncio.sleep).

Both paths handle spec-compliant input-required task state: when the server
needs human approval for a dangerous tool, the client surfaces the prompt and
sends a continuation via POST /tasks/send with {task_id, message: "yes"|"no"}.

Protocol subset implemented:
    GET  /.well-known/agent.json    — fetch Agent Card
    POST /tasks/send                — submit task (or continue existing), poll until done
    POST /tasks/sendSubscribe       — submit task, read SSE stream until done
    POST /tasks/send (continuation) — approve/deny input-required requests

Task lifecycle: submitted → working → [input-required → working →]* completed / failed
"""

from __future__ import annotations

import asyncio
import http.client
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from .models import AgentCard, Artifact, Task, TaskStatus, _make_message


# ── Minimal inline SSE parser ─────────────────────────────────────────────────

@dataclass
class _SSEEvent:
    data: str = ""
    event: str = ""
    id: str = ""


def _iter_sse_events(response: http.client.HTTPResponse) -> Iterator[_SSEEvent]:
    """Yield SSE events from an HTTP response using readline()."""
    current = _SSEEvent()
    while True:
        line_bytes = response.readline()
        if not line_bytes:
            break
        line = line_bytes.rstrip(b"\r\n").decode("utf-8", errors="replace")
        if line == "":
            if current.data:
                yield current
            current = _SSEEvent()
        elif line.startswith("data:"):
            current.data += line[5:].lstrip(" ")
        elif line.startswith("event:"):
            current.event = line[6:].strip()
        elif line.startswith("id:"):
            current.id = line[3:].strip()


class A2AError(Exception):
    """Raised on HTTP errors, timeouts, or malformed A2A responses."""


# ── User approval UI ──────────────────────────────────────────────────────────

def _prompt_user_approval(agent_name: str, question: str, detail: str = "") -> bool:
    """Interactively ask the user to approve/deny an input-required request.

    Pauses any active spinner (e.g. the "waiting for agent..." status) so the
    questionary prompt renders cleanly, then resumes the spinner after the user
    answers so they know polling has continued.
    """
    from ..theme import console, pause_spinner, resume_spinner
    pause_spinner()
    try:
        if detail:
            console.print(f"\n[muted]{detail}[/]\n")
        import questionary
        from ..config import MINION_STYLE
        return bool(
            questionary.confirm(
                f"  [remote: {agent_name}] {question}",
                default=False,
                style=MINION_STYLE,
            ).ask()
        )
    except Exception:
        return False
    finally:
        resume_spinner()


def _extract_approval_parts(status_obj: dict) -> tuple[str, str]:
    """Extract (question, detail) from an A2A status.message for input-required tasks.

    Handles both the multi-part format (parts[0] = question, parts[1:] = context)
    and external A2A servers that may embed everything in a single text part.
    """
    agent_msg = status_obj.get("message", {}) if isinstance(status_obj, dict) else {}
    parts = agent_msg.get("parts", []) if isinstance(agent_msg, dict) else []
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text" and p.get("text")]
    question = text_parts[0] if text_parts else "Allow action?"
    detail = "\n".join(text_parts[1:]) if len(text_parts) > 1 else ""
    return question, detail


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
        self._base_url = url.rstrip("/")
        # One contextId per client instance (i.e., per named remote agent per REPL session).
        # All tasks sent to this agent share the context so the server can inject history.
        import uuid as _uuid
        self._context_id: str = str(_uuid.uuid4())

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

    # ─── Task cancellation ────────────────────────────────────────────────────

    def cancel_task(self, task_id: str) -> None:
        """DELETE /tasks/{task_id} — request cancellation of a running task.

        Best-effort: raises A2AError only on connection failure. A 404 (task not
        found) or 409 (already in terminal state) is silently accepted.
        """
        try:
            conn = self._make_connection()
            conn.request(
                "DELETE",
                self._path(f"/tasks/{task_id}"),
                headers={"Accept": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()  # consume body
        except Exception as e:
            raise A2AError(f"Failed to cancel task '{task_id}' on '{self.name}': {e}") from e

    # ─── Task submission (sync polling) ───────────────────────────────────────

    def send_task(self, task_text: str) -> str:
        """Submit a task and poll GET /tasks/{id} until completed or failed.

        Returns artifact text (all artifacts joined with \\n\\n) on success.
        Raises A2AError on HTTP failure, task failure, or timeout.
        Handles input-required by prompting the user interactively.
        """
        task_id = self._submit_task(task_text)
        task = self._poll_until_done(task_id)

        if task.status == TaskStatus.FAILED:
            raise A2AError(task.error or "Remote task failed with no error message.")

        if task.artifacts:
            return "\n\n".join(a.text for a in task.artifacts if a.text)
        return ""

    def _submit_task(self, task_text: str) -> str:
        """POST /tasks/send → task_id."""
        body = json.dumps({"contextId": self._context_id, "message": _make_message(task_text)}).encode("utf-8")
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

    def _send_continuation(self, task_id: str, answer: str) -> None:
        """POST /tasks/send with task id to continue an input-required task (spec: uses 'id' field)."""
        body = json.dumps({"id": task_id, "message": _make_message(answer)}).encode("utf-8")
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
            conn.getresponse()
        except Exception:
            pass  # best-effort; poll loop will continue regardless

    def _poll_until_done(self, task_id: str) -> Task:
        """GET /tasks/{id} every 0.5s until status is completed or failed.

        Handles input-required by prompting the user and sending a continuation.
        """
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

            if task.status == TaskStatus.INPUT_REQUIRED:
                # Spec: prompt is in status.message (multi-part A2A Message)
                status_obj = data.get("status", {})
                question, detail = _extract_approval_parts(status_obj)
                approved = _prompt_user_approval(self.name, question, detail)
                self._send_continuation(task_id, "yes" if approved else "no")
                time.sleep(0.5)
                continue

            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED):
                return task
            time.sleep(0.5)

    # ─── Task submission (SSE streaming, sync) ────────────────────────────────

    def send_task_streaming(
        self,
        task_text: str,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> str:
        """POST /tasks/sendSubscribe and read SSE stream until completion.

        Handles input-required by prompting the user and sending a continuation,
        then falls back to polling until the task resumes.
        Calls on_status(status_str) for each SSE event received.
        Returns the artifact text on success.
        Raises A2AError on failure or timeout.
        """
        body = json.dumps({"contextId": self._context_id, "message": _make_message(task_text)}).encode("utf-8")
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

        task_id: Optional[str] = None
        artifact_texts: list[str] = []

        for sse_event in _iter_sse_events(resp):
            try:
                data = json.loads(sse_event.data)
            except json.JSONDecodeError:
                continue

            task_id = data.get("id", task_id)

            # Use SSE event type if present; fall back to payload key inspection.
            # Spec event names: "task-artifact-update", "task-status-update"
            event_type = sse_event.event  # "" if server doesn't emit event names

            is_artifact = event_type == "task-artifact-update" or (
                not event_type and "artifact" in data
            )
            is_status = event_type == "task-status-update" or (
                not event_type and "status" in data and "artifact" not in data
            )

            # TaskArtifactUpdateEvent: {"id", "artifact": {parts}, "final"}
            if is_artifact and "artifact" in data:
                artifact = Artifact.from_dict(data["artifact"])
                if artifact.text:
                    artifact_texts.append(artifact.text)
                if data.get("final"):
                    continue  # wait for final status event

            if not is_status:
                continue

            # TaskStatusUpdateEvent: {"id", "status": {state, timestamp}, "final"}
            status_obj = data.get("status", {})
            state = status_obj.get("state", "") if isinstance(status_obj, dict) else ""

            if on_status is not None and state:
                on_status(state)

            if state == TaskStatus.INPUT_REQUIRED.value and task_id:
                # Spec: prompt is in status.message (multi-part A2A Message)
                question, detail = _extract_approval_parts(status_obj)
                approved = _prompt_user_approval(self.name, question, detail)
                self._send_continuation(task_id, "yes" if approved else "no")
                task = self._poll_until_done(task_id)
                if task.status == TaskStatus.FAILED:
                    raise A2AError(task.error or "Remote task failed.")
                if task.artifacts:
                    return "\n\n".join(a.text for a in task.artifacts if a.text)
                return ""

            if state == TaskStatus.COMPLETED.value and data.get("final"):
                return "\n\n".join(artifact_texts) if artifact_texts else ""

            if state == TaskStatus.FAILED.value:
                raise A2AError(data.get("error") or "Remote task failed.")

            if state == TaskStatus.CANCELED.value:
                raise A2AError(f"Task was canceled by '{self.name}'.")

        raise A2AError(f"SSE stream from '{self.name}' ended without a final status event.")

    # ─── Task submission (async with httpx) ───────────────────────────────────

    async def send_task_async(self, task_text: str) -> str:
        """Submit a task using httpx.AsyncClient with async polling.

        Uses asyncio.sleep instead of time.sleep so the event loop stays responsive.
        Handles input-required by prompting the user in a thread.
        """
        try:
            import httpx
        except ImportError:
            raise A2AError("httpx is required for async A2A. Install it: pip install httpx") from None

        async with httpx.AsyncClient(timeout=self._timeout, base_url=self._base_url) as client:
            # Submit task with spec Message format
            try:
                resp = await client.post("/tasks/send", json={"contextId": self._context_id, "message": _make_message(task_text)})
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as e:
                raise A2AError(f"Failed to submit task to '{self.name}': {e}") from e

            task_id = data.get("id")
            if not task_id:
                raise A2AError(f"No task ID in response from '{self.name}'")

            # Async poll loop
            deadline = time.monotonic() + self._timeout
            while True:
                if time.monotonic() >= deadline:
                    raise A2AError(
                        f"Task '{task_id}' from '{self.name}' timed out after {self._timeout}s"
                    )
                try:
                    poll_resp = await client.get(f"/tasks/{task_id}")
                    poll_resp.raise_for_status()
                    task_data = poll_resp.json()
                except httpx.HTTPError as e:
                    raise A2AError(f"Failed to poll task '{task_id}': {e}") from e

                task = Task.from_dict(task_data)

                if task.status == TaskStatus.INPUT_REQUIRED:
                    # Spec: prompt is in status.message (multi-part A2A Message)
                    status_obj = task_data.get("status", {})
                    question, detail = _extract_approval_parts(status_obj)
                    approved = await asyncio.to_thread(_prompt_user_approval, self.name, question, detail)
                    answer = "yes" if approved else "no"
                    try:
                        await client.post("/tasks/send", json={"id": task_id, "message": _make_message(answer)})
                    except httpx.HTTPError:
                        pass  # best-effort
                    await asyncio.sleep(0.5)
                    continue

                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELED):
                    break
                await asyncio.sleep(0.5)

        if task.status == TaskStatus.FAILED:
            raise A2AError(task.error or "Remote task failed.")
        if task.status == TaskStatus.CANCELED:
            raise A2AError(f"Task was canceled by '{self.name}'.")
        if task.artifacts:
            return "\n\n".join(a.text for a in task.artifacts if a.text)
        return ""
