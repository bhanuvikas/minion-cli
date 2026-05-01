"""Tests for A2AServer (endpoint tests) and the echo agent example.

Testing strategy:
  - TestA2AServer: start a real A2AServer in a background thread bound to a
    free port; make actual HTTP calls via http.client; tear down after each test.
  - TestEchoAgent: import and run the standalone echo agent from examples/ to
    verify the protocol contract without any minion dependency.

The server uses module-level state (_tasks, _task_done_events). Each test class
uses setUp/tearDown to start a fresh server instance, keeping tests isolated.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from minion.a2a.server import A2AServer


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    """Find a free local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str) -> tuple[int, dict]:
    """Make a GET request and return (status, parsed_json)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Accept": "application/json"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    try:
        return resp.status, json.loads(body)
    except json.JSONDecodeError:
        return resp.status, {"raw": body}


def _post(port: int, path: str, body: dict, accept: str = "application/json") -> tuple[int, dict | str]:
    """Make a POST request and return (status, parsed_json_or_raw_body)."""
    payload = json.dumps(body).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=payload, headers={
        "Content-Type": "application/json",
        "Accept": accept,
        "Content-Length": str(len(payload)),
    })
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    if accept == "application/json":
        try:
            return resp.status, json.loads(raw)
        except json.JSONDecodeError:
            return resp.status, {"raw": raw}
    return resp.status, raw


def _task_state(data: dict) -> str:
    """Extract task state from spec {state, timestamp} object or legacy string."""
    status = data.get("status", "")
    if isinstance(status, dict):
        return status.get("state", "")
    return status


def _wait_for_status(port: int, task_id: str, target: str, timeout: float = 5.0) -> dict:
    """Poll GET /tasks/{id} until status.state reaches target or timeout."""
    deadline = time.monotonic() + timeout
    while True:
        status, data = _get(port, f"/tasks/{task_id}")
        if status == 200 and _task_state(data) == target:
            return data
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Task {task_id} did not reach '{target}' within {timeout}s. Last: {data}")
        time.sleep(0.1)


# ── TestA2AServer ───────────────────────────────────────────────────────────────

class TestA2AServer(unittest.TestCase):
    """Integration tests for A2AServer endpoints.

    A real server listens on a random port; tests make real HTTP calls.
    The agent_runner is a simple lambda to avoid LLM dependencies.
    """

    def setUp(self):
        self.port = _free_port()
        self.server = A2AServer(
            host="127.0.0.1",
            port=self.port,
            agent_runner=lambda task, **kw: f"Echo: {task}",
        )
        self._thread = threading.Thread(target=self.server.start, daemon=True)
        self._thread.start()
        # Wait for server to accept connections
        deadline = time.monotonic() + 3.0
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.request("GET", "/")
                conn.getresponse()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server did not start in time")
                time.sleep(0.05)

    def tearDown(self):
        self.server.stop()
        self._thread.join(timeout=5)

    def test_agent_card_endpoint_returns_json(self):
        status, data = _get(self.port, "/.well-known/agent.json")
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "minion")
        self.assertIn("url", data)
        self.assertIn("skills", data)
        self.assertIsInstance(data["skills"], list)
        self.assertTrue(data["capabilities"].get("streaming"))

    def test_send_task_returns_submitted_status(self):
        status, data = _post(self.port, "/tasks/send", {"message": "hello"})
        self.assertIn(status, (200, 201, 202))
        self.assertIn("id", data)
        # Spec: status is {state, timestamp} object
        self.assertEqual(_task_state(data), "submitted")

    def test_get_task_returns_submitted_when_pending(self):
        _, submit_data = _post(self.port, "/tasks/send", {"message": "fast task"})
        task_id = submit_data["id"]
        status, data = _get(self.port, f"/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertIn(_task_state(data), ["submitted", "working", "completed"])

    def test_get_task_returns_completed_with_artifact(self):
        _, submit_data = _post(self.port, "/tasks/send", {"message": "solve me"})
        task_id = submit_data["id"]
        completed = _wait_for_status(self.port, task_id, "completed")
        self.assertEqual(_task_state(completed), "completed")
        self.assertIn("artifacts", completed)
        # Spec: artifact is {artifactId, parts: [{type, text}]}
        artifact_text = completed["artifacts"][0]["parts"][0]["text"]
        self.assertIn("Echo:", artifact_text)
        self.assertIn("solve me", artifact_text)

    def test_get_task_returns_failed_on_error(self):
        # Use an agent_runner that raises to test failure path
        error_port = _free_port()
        error_server = A2AServer(
            host="127.0.0.1",
            port=error_port,
            agent_runner=lambda task, **kw: (_ for _ in ()).throw(RuntimeError("intentional fail")),
        )
        t = threading.Thread(target=error_server.start, daemon=True)
        t.start()
        time.sleep(0.3)

        try:
            _, submit_data = _post(error_port, "/tasks/send", {"message": "fail please"})
            task_id = submit_data["id"]
            failed = _wait_for_status(error_port, task_id, "failed")
            self.assertEqual(_task_state(failed), "failed")
            self.assertIn("error", failed)
        finally:
            error_server.stop()
            t.join(timeout=5)

    def test_unknown_route_returns_404(self):
        status, _ = _get(self.port, "/not/a/real/path")
        self.assertEqual(status, 404)

    def test_send_task_missing_message_returns_400(self):
        status, data = _post(self.port, "/tasks/send", {})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_get_nonexistent_task_returns_404(self):
        status, _ = _get(self.port, "/tasks/no-such-task-id")
        self.assertEqual(status, 404)

    def test_send_subscribe_streams_sse_events(self):
        """POST /tasks/sendSubscribe should return SSE text/event-stream."""
        payload = json.dumps({"message": "stream me"}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks/sendSubscribe", body=payload, headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Content-Length": str(len(payload)),
        })
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        content_type = resp.getheader("Content-Type", "")
        self.assertIn("text/event-stream", content_type)

        body = resp.read().decode("utf-8")
        events = []
        for line in body.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        # Spec: SSE events are TaskStatusUpdateEvent and TaskArtifactUpdateEvent
        states = [e.get("status", {}).get("state") for e in events if "status" in e]
        self.assertIn("submitted", states)
        self.assertIn("completed", states)
        # Artifacts arrive as separate artifact events, not inside status events
        artifact_events = [e for e in events if "artifact" in e]
        self.assertTrue(len(artifact_events) > 0)

    def test_concurrent_tasks_handled_independently(self):
        """Two concurrent tasks should both complete with correct results."""
        results = {}

        def run_task(name: str):
            _, submit = _post(self.port, "/tasks/send", {"message": name})
            task_id = submit["id"]
            done = _wait_for_status(self.port, task_id, "completed")
            results[name] = done

        t1 = threading.Thread(target=run_task, args=("task-alpha",))
        t2 = threading.Thread(target=run_task, args=("task-beta",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertIn("task-alpha", results)
        self.assertIn("task-beta", results)
        # Spec: artifact text is in parts[0]["text"]
        self.assertIn("task-alpha", results["task-alpha"]["artifacts"][0]["parts"][0]["text"])
        self.assertIn("task-beta", results["task-beta"]["artifacts"][0]["parts"][0]["text"])

    def test_server_stop_stops_accepting_requests(self):
        """After stop(), the port should no longer be listening."""
        self.server.stop()
        self._thread.join(timeout=5)
        # Try to connect — should fail (connection refused)
        with self.assertRaises(Exception):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
            conn.request("GET", "/")
            conn.getresponse()

    def test_cancel_task_via_delete(self):
        """DELETE /tasks/{id} cancels a task before it completes."""
        # Use a slow agent so the task is still working when we cancel
        slow_port = _free_port()
        ready = threading.Event()
        canceled = threading.Event()

        def slow_runner(task, **kw):
            ready.set()
            canceled.wait(timeout=5)
            return "done"

        slow_server = A2AServer("127.0.0.1", slow_port, agent_runner=slow_runner)
        t = threading.Thread(target=slow_server.start, daemon=True)
        t.start()
        deadline = time.monotonic() + 3.0
        while True:
            try:
                c = http.client.HTTPConnection("127.0.0.1", slow_port, timeout=1)
                c.request("GET", "/")
                c.getresponse()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server didn't start")
                time.sleep(0.05)

        try:
            _, submit_data = _post(slow_port, "/tasks/send", {"message": "slow task"})
            task_id = submit_data["id"]
            ready.wait(timeout=3)  # ensure task is running

            conn = http.client.HTTPConnection("127.0.0.1", slow_port, timeout=5)
            conn.request("DELETE", f"/tasks/{task_id}", headers={"Accept": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read())

            self.assertEqual(resp.status, 200)
            self.assertEqual(_task_state(data), "canceled")
            canceled.set()
        finally:
            slow_server.stop()
            t.join(timeout=5)

    def test_cancel_nonexistent_task_returns_404(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("DELETE", "/tasks/no-such-task", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)

    def test_cancel_via_post_tasks_cancel(self):
        """POST /tasks/cancel with {id} also cancels a task."""
        status, data = _post(self.port, "/tasks/cancel", {"id": "no-such-task"})
        self.assertEqual(status, 404)

    def test_cancel_completed_task_returns_409(self):
        """Canceling a completed task returns 409 Conflict."""
        _, submit_data = _post(self.port, "/tasks/send", {"message": "quick task"})
        task_id = submit_data["id"]
        _wait_for_status(self.port, task_id, "completed")

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("DELETE", f"/tasks/{task_id}", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 409)

    def test_sse_emits_event_names(self):
        """SSE stream includes 'event: task-status-update' and 'event: task-artifact-update' lines."""
        payload = json.dumps({"message": "emit events"}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks/sendSubscribe", body=payload, headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Content-Length": str(len(payload)),
        })
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = resp.read().decode("utf-8")

        event_names = [
            line[len("event: "):].strip()
            for line in body.splitlines()
            if line.startswith("event: ")
        ]
        self.assertIn("task-status-update", event_names)
        self.assertIn("task-artifact-update", event_names)

    def test_task_without_context_id_works_normally(self):
        """Tasks without contextId still work — backward compat."""
        _, data = _post(self.port, "/tasks/send", {"message": "no context"})
        task_id = data["id"]
        self.assertNotIn("contextId", data)
        completed = _wait_for_status(self.port, task_id, "completed")
        self.assertEqual(_task_state(completed), "completed")

    def test_task_response_includes_context_id_when_provided(self):
        ctx = str(uuid.uuid4())
        _, data = _post(self.port, "/tasks/send", {"message": "with context", "contextId": ctx})
        self.assertEqual(data.get("contextId"), ctx)

    def test_second_task_in_same_context_receives_history(self):
        """When two tasks share a contextId the second call's agent receives history from turn 1."""
        received = []

        def capturing_runner(task, **kw):
            received.append(task)
            return f"Answer for: {task[:40]}"

        port = _free_port()
        server = A2AServer("127.0.0.1", port, agent_runner=capturing_runner)
        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        deadline = time.monotonic() + 3.0
        while True:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/")
                c.getresponse()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server didn't start")
                time.sleep(0.05)

        try:
            ctx = str(uuid.uuid4())

            _, d1 = _post(port, "/tasks/send", {"message": "first request", "contextId": ctx})
            _wait_for_status(port, d1["id"], "completed")

            _, d2 = _post(port, "/tasks/send", {"message": "second request", "contextId": ctx})
            _wait_for_status(port, d2["id"], "completed")

            self.assertEqual(len(received), 2)
            # First call: no history prefix
            self.assertNotIn("Session context", received[0])
            self.assertEqual(received[0], "first request")
            # Second call: history injected
            self.assertIn("Session context", received[1])
            self.assertIn("first request", received[1])
            self.assertIn("second request", received[1])
        finally:
            server.stop()
            t.join(timeout=5)

    def test_different_context_ids_have_separate_histories(self):
        """Tasks with different contextIds don't bleed history into each other."""
        received = []

        def capturing_runner(task, **kw):
            received.append(task)
            return "ok"

        port = _free_port()
        server = A2AServer("127.0.0.1", port, agent_runner=capturing_runner)
        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        deadline = time.monotonic() + 3.0
        while True:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/")
                c.getresponse()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server didn't start")
                time.sleep(0.05)

        try:
            ctx_a = str(uuid.uuid4())
            ctx_b = str(uuid.uuid4())

            _, d1 = _post(port, "/tasks/send", {"message": "context A task 1", "contextId": ctx_a})
            _wait_for_status(port, d1["id"], "completed")

            # ctx_b gets its own fresh history — should NOT see ctx_a's task
            _, d2 = _post(port, "/tasks/send", {"message": "context B task 1", "contextId": ctx_b})
            _wait_for_status(port, d2["id"], "completed")

            self.assertEqual(len(received), 2)
            # ctx_b's first task has no history (different context)
            self.assertNotIn("Session context", received[1])
            self.assertNotIn("context A", received[1])
        finally:
            server.stop()
            t.join(timeout=5)

    def test_sse_stream_includes_context_id_in_events(self):
        ctx = str(uuid.uuid4())
        payload = json.dumps({"message": "sse context", "contextId": ctx}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks/sendSubscribe", body=payload, headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Content-Length": str(len(payload)),
        })
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        events = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
        # At least one event should carry contextId
        ctx_ids = [e.get("contextId") for e in events if "contextId" in e]
        self.assertTrue(any(c == ctx for c in ctx_ids))


# ── TestEchoAgent ───────────────────────────────────────────────────────────────

class TestEchoAgent(unittest.TestCase):
    """Tests using the standalone echo agent from examples/a2a_echo_agent.py.

    Imports and runs the echo server (no minion dependency) to verify the A2A
    client can talk to a minimal compliant server.
    """

    @classmethod
    def setUpClass(cls):
        examples_dir = Path(__file__).parent.parent / "examples"
        sys.path.insert(0, str(examples_dir))
        try:
            from a2a_echo_agent import EchoHandler, AGENT_CARD
            cls._EchoHandler = EchoHandler
            cls._AGENT_CARD = AGENT_CARD
            cls._available = True
        except ImportError:
            cls._available = False
            return

        from http.server import ThreadingHTTPServer

        cls.port = _free_port()
        cls._AGENT_CARD["url"] = f"http://127.0.0.1:{cls.port}"
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), cls._EchoHandler)
        cls.server_thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls.server_thread.start()

        # Wait for server to start
        deadline = time.monotonic() + 3.0
        while True:
            try:
                c = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                c.request("GET", "/.well-known/agent.json")
                c.getresponse()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    cls._available = False
                    break
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_httpd"):
            cls._httpd.shutdown()

    def _skip_if_unavailable(self):
        if not self.__class__._available:
            self.skipTest("echo agent module not available or server did not start")

    def test_echo_agent_card_accessible(self):
        self._skip_if_unavailable()
        status, data = _get(self.port, "/.well-known/agent.json")
        self.assertEqual(status, 200)
        self.assertIn("name", data)

    def test_echo_agent_send_task_completes(self):
        self._skip_if_unavailable()
        _, submit = _post(self.port, "/tasks/send", {"message": "ping"})
        task_id = submit["id"]
        done = _wait_for_status(self.port, task_id, "completed", timeout=5)
        self.assertEqual(_task_state(done), "completed")
        self.assertIn("artifacts", done)
        # Spec: artifact text is in parts[0]["text"]
        self.assertIn("ping", done["artifacts"][0]["parts"][0]["text"])

    def test_echo_agent_get_task_after_send(self):
        self._skip_if_unavailable()
        _, submit = _post(self.port, "/tasks/send", {"message": "hello"})
        task_id = submit["id"]
        _wait_for_status(self.port, task_id, "completed", timeout=5)
        status, data = _get(self.port, f"/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(_task_state(data), "completed")

    def test_echo_agent_unknown_task_returns_404(self):
        self._skip_if_unavailable()
        status, _ = _get(self.port, "/tasks/does-not-exist")
        self.assertEqual(status, 404)

    def test_echo_agent_sse_stream_works(self):
        self._skip_if_unavailable()
        payload = json.dumps({"message": "stream test"}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks/sendSubscribe", body=payload, headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Content-Length": str(len(payload)),
        })
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = resp.read().decode("utf-8")
        events = [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]
        states = [e.get("status", {}).get("state") for e in events if "status" in e]
        self.assertIn("completed", states)


if __name__ == "__main__":
    unittest.main()
