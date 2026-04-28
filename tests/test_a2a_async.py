"""Tests for A2A async client, input-required flow, and confirm_callback in ToolExecutor."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minion.a2a.models import TaskStatus


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_task_dict(task_id: str, status: str, artifact: str = "", error: str = "") -> dict:
    d: dict = {"id": task_id, "status": status}
    if artifact:
        d["artifacts"] = [{"text": artifact}]
    if error:
        d["error"] = error
    return d


def _make_input_required_dict(task_id: str, prompt: str) -> dict:
    return {
        "id": task_id,
        "status": "input-required",
        "input": {"prompt": prompt, "type": "confirm"},
    }


# ── Tests for async send_task_async ──────────────────────────────────────────

class TestA2AClientAsync:
    @pytest.mark.asyncio
    async def test_send_task_async_completes(self):
        from minion.a2a.client import A2AClient

        client = A2AClient("test_agent", "http://localhost:9000", timeout_seconds=5)

        submitted = {"id": "t1", "status": "submitted"}
        working   = _make_task_dict("t1", "working")
        completed = _make_task_dict("t1", "completed", artifact="result text")

        mock_httpx_client = AsyncMock()
        mock_httpx_client.__aenter__ = AsyncMock(return_value=mock_httpx_client)
        mock_httpx_client.__aexit__ = AsyncMock(return_value=False)

        post_resp = MagicMock()
        post_resp.json.return_value = submitted
        post_resp.raise_for_status = MagicMock()
        mock_httpx_client.post = AsyncMock(return_value=post_resp)

        get_calls = [working, completed]
        get_resp = MagicMock()
        get_resp.raise_for_status = MagicMock()
        get_resp.json = MagicMock(side_effect=get_calls)
        mock_httpx_client.get = AsyncMock(return_value=get_resp)

        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            result = await client.send_task_async("do something")

        assert result == "result text"

    @pytest.mark.asyncio
    async def test_send_task_async_raises_on_failed(self):
        from minion.a2a.client import A2AClient

        client = A2AClient("test_agent", "http://localhost:9000", timeout_seconds=5)

        submitted = {"id": "t2", "status": "submitted"}
        failed    = _make_task_dict("t2", "failed", error="something went wrong")

        mock_httpx_client = AsyncMock()
        mock_httpx_client.__aenter__ = AsyncMock(return_value=mock_httpx_client)
        mock_httpx_client.__aexit__ = AsyncMock(return_value=False)

        post_resp = MagicMock()
        post_resp.json.return_value = submitted
        post_resp.raise_for_status = MagicMock()
        mock_httpx_client.post = AsyncMock(return_value=post_resp)

        get_resp = MagicMock()
        get_resp.raise_for_status = MagicMock()
        get_resp.json = MagicMock(return_value=failed)
        mock_httpx_client.get = AsyncMock(return_value=get_resp)

        from minion.a2a.client import A2AError
        with patch("httpx.AsyncClient", return_value=mock_httpx_client):
            with pytest.raises(A2AError, match="something went wrong"):
                await client.send_task_async("do something")

    @pytest.mark.asyncio
    async def test_send_task_async_handles_input_required(self):
        from minion.a2a.client import A2AClient

        client = A2AClient("test_agent", "http://localhost:9000", timeout_seconds=5)

        submitted     = {"id": "t3", "status": "submitted"}
        input_req     = _make_input_required_dict("t3", "Allow run_shell?")
        working_again = _make_task_dict("t3", "working")
        completed     = _make_task_dict("t3", "completed", artifact="approved!")

        mock_httpx_client = AsyncMock()
        mock_httpx_client.__aenter__ = AsyncMock(return_value=mock_httpx_client)
        mock_httpx_client.__aexit__ = AsyncMock(return_value=False)

        post_resp = MagicMock()
        post_resp.json.return_value = submitted
        post_resp.raise_for_status = MagicMock()
        # second post (continuation) should also succeed
        cont_resp = MagicMock()
        cont_resp.raise_for_status = MagicMock()
        mock_httpx_client.post = AsyncMock(side_effect=[post_resp, cont_resp])

        get_calls = [input_req, working_again, completed]
        get_resp = MagicMock()
        get_resp.raise_for_status = MagicMock()
        get_resp.json = MagicMock(side_effect=get_calls)
        mock_httpx_client.get = AsyncMock(return_value=get_resp)

        # Simulate user approving
        with patch("httpx.AsyncClient", return_value=mock_httpx_client), \
             patch("minion.a2a.client._prompt_user_approval", return_value=True):
            result = await client.send_task_async("do something")

        assert result == "approved!"
        # Verify continuation was sent with "yes"
        cont_call = mock_httpx_client.post.call_args_list[1]
        assert cont_call.kwargs.get("json") == {"task_id": "t3", "message": "yes"}


# ── Tests for confirm_callback in ToolExecutor ────────────────────────────────

class TestToolExecutorConfirmCallback:
    def test_confirm_callback_used_instead_of_questionary(self):
        from minion.tools.executor import ToolExecutor
        from minion.llm.base import ToolUseBlock

        callback_calls = []
        def my_callback(prompt: str) -> bool:
            callback_calls.append(prompt)
            return True  # always approve

        executor = ToolExecutor(confirm_callback=my_callback)
        tb = ToolUseBlock(id="t1", name="run_shell", input={"command": "ls"})

        with patch.dict("minion.tools.executor._DISPATCH", {"run_shell": lambda **kw: "ok"}):
            result = executor.execute(tb)

        assert len(callback_calls) == 1
        assert "run_shell" in callback_calls[0]
        assert result == "ok"

    def test_confirm_callback_decline_returns_user_declined(self):
        from minion.tools.executor import ToolExecutor
        from minion.llm.base import ToolUseBlock

        executor = ToolExecutor(confirm_callback=lambda prompt: False)
        tb = ToolUseBlock(id="t2", name="run_shell", input={"command": "rm -rf /"})

        result = executor.execute(tb)
        assert result == "User declined tool execution."

    @pytest.mark.asyncio
    async def test_confirm_callback_used_in_execute_async(self):
        from minion.tools.executor import ToolExecutor
        from minion.llm.base import ToolUseBlock

        approved = []
        def my_callback(prompt: str) -> bool:
            approved.append(prompt)
            return True

        executor = ToolExecutor(confirm_callback=my_callback)
        tb = ToolUseBlock(id="t3", name="run_shell", input={"command": "echo hi"})

        with patch.dict("minion.tools.executor._DISPATCH", {"run_shell": lambda **kw: "hi"}):
            result = await executor.execute_async(tb)

        assert len(approved) == 1
        assert result == "hi"

    @pytest.mark.asyncio
    async def test_confirm_callback_async_decline(self):
        from minion.tools.executor import ToolExecutor
        from minion.llm.base import ToolUseBlock

        executor = ToolExecutor(confirm_callback=lambda p: False)
        # run_shell is a dangerous tool — confirm_callback will deny it
        tb = ToolUseBlock(id="t4", name="run_shell", input={"command": "echo hi"})

        with patch.dict("minion.tools.executor._DISPATCH", {"run_shell": lambda **kw: "unreachable"}):
            result = await executor.execute_async(tb)
        assert result == "User declined tool execution."


# ── Tests for A2A server input-required ──────────────────────────────────────

class TestA2AServerInputRequired:
    """Integration tests: real server + real HTTP calls in threads."""

    def _start_server_with_runner(self, runner_fn):
        import http.client as hc
        from minion.a2a.server import A2AServer

        port = _free_port()
        server = A2AServer("127.0.0.1", port, agent_runner=runner_fn)
        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        # Wait for server to accept connections
        deadline = time.monotonic() + 3.0
        while True:
            try:
                c = hc.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/")
                c.getresponse()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("Server didn't start")
                time.sleep(0.05)
        return server, port

    def test_task_transitions_to_input_required_when_callback_invoked(self):
        import http.client as hc
        import json

        # Agent runner that calls the confirm_callback and records the answer
        recorded_answer = []

        def runner(task: str, *, confirm_callback=None):
            if confirm_callback is not None:
                result = confirm_callback("Allow dangerous_tool?")
                recorded_answer.append(result)
            return "done"

        server, port = self._start_server_with_runner(runner)

        # Submit task
        conn = hc.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"message": "run dangerous task"}).encode()
        conn.request("POST", "/tasks/send", body=body,
                     headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        task_id = data["id"]

        # Wait for task to reach input-required
        deadline = time.monotonic() + 3.0
        task_status = "submitted"
        while task_status != "input-required" and time.monotonic() < deadline:
            time.sleep(0.1)
            conn2 = hc.HTTPConnection("127.0.0.1", port, timeout=2)
            conn2.request("GET", f"/tasks/{task_id}")
            r = conn2.getresponse()
            task_data = json.loads(r.read())
            task_status = task_data.get("status")

        assert task_status == "input-required", f"Got status: {task_status}"
        task_input = task_data.get("input", {})
        assert "Allow dangerous_tool?" in task_input.get("prompt", "")

        # Send continuation: approve
        conn3 = hc.HTTPConnection("127.0.0.1", port, timeout=5)
        cont_body = json.dumps({"task_id": task_id, "message": "yes"}).encode()
        conn3.request("POST", "/tasks/send", body=cont_body,
                      headers={"Content-Type": "application/json", "Content-Length": str(len(cont_body))})
        cont_resp = conn3.getresponse()
        cont_resp.read()

        # Wait for task to complete
        deadline = time.monotonic() + 3.0
        while task_status != "completed" and time.monotonic() < deadline:
            time.sleep(0.1)
            conn4 = hc.HTTPConnection("127.0.0.1", port, timeout=2)
            conn4.request("GET", f"/tasks/{task_id}")
            r = conn4.getresponse()
            task_status = json.loads(r.read()).get("status")

        assert task_status == "completed"
        assert recorded_answer == [True]  # callback returned True (approved)

        server.stop()

    def test_denial_sends_false_to_callback(self):
        import http.client as hc
        import json

        recorded_answer = []

        def runner(task: str, *, confirm_callback=None):
            if confirm_callback:
                result = confirm_callback("Allow risky?")
                recorded_answer.append(result)
            return "done regardless"

        server, port = self._start_server_with_runner(runner)

        conn = hc.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"message": "task"}).encode()
        conn.request("POST", "/tasks/send", body=body,
                     headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
        resp = conn.getresponse()
        task_id = json.loads(resp.read())["id"]

        # Wait for input-required
        deadline = time.monotonic() + 3.0
        status = "submitted"
        while status != "input-required" and time.monotonic() < deadline:
            time.sleep(0.1)
            c = hc.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", f"/tasks/{task_id}")
            status = json.loads(c.getresponse().read()).get("status")

        # Send denial
        cont_body = json.dumps({"task_id": task_id, "message": "no"}).encode()
        c2 = hc.HTTPConnection("127.0.0.1", port, timeout=5)
        c2.request("POST", "/tasks/send", body=cont_body,
                   headers={"Content-Type": "application/json", "Content-Length": str(len(cont_body))})
        c2.getresponse().read()

        # Wait for completion
        deadline = time.monotonic() + 3.0
        while status != "completed" and time.monotonic() < deadline:
            time.sleep(0.1)
            c3 = hc.HTTPConnection("127.0.0.1", port, timeout=2)
            c3.request("GET", f"/tasks/{task_id}")
            status = json.loads(c3.getresponse().read()).get("status")

        assert status == "completed"
        assert recorded_answer == [False]  # callback returned False (denied)
        server.stop()


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
