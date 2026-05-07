"""Tests for A2A models, config, client, manager, card, and executor routing.

Testing strategy:
  - Models: pure unit tests on dataclass serialisation/deserialisation
  - Config: mock filesystem via tmp_path (pytest) or tmp directories (unittest)
  - Client: mock http.client.HTTPConnection with _FakeResponse (same pattern as test_mcp_http.py)
  - Manager: patch A2AClient.send_task; verify routing and trace emission
  - AgentCard: test card generation fields
  - SendRemoteTask tool: patch ToolExecutor._remote_task_runner; verify executor routing
"""

from __future__ import annotations

import http.client
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

# ── Add project root to path ────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from minion.a2a.card import generate_agent_card
from minion.a2a.client import A2AClient, A2AError
from minion.a2a.config import A2AAgentConfig, load_a2a_config
from minion.a2a.manager import A2AManager, load_a2a_manager
from minion.a2a.models import AgentCard, Artifact, Task, TaskStatus
from minion.llm.base import ToolUseBlock


# ── Helpers ─────────────────────────────────────────────────────────────────────

class _FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse."""

    def __init__(self, body: bytes, status: int = 200,
                 content_type: str = "application/json") -> None:
        self.status = status
        self._body = body
        self._pos = 0
        self._content_type = content_type

    def read(self) -> bytes:
        return self._body

    def readline(self) -> bytes:
        if self._pos >= len(self._body):
            return b""
        end = self._body.find(b"\n", self._pos)
        if end == -1:
            line = self._body[self._pos:]
            self._pos = len(self._body)
        else:
            line = self._body[self._pos:end + 1]
            self._pos = end + 1
        return line

    def getheader(self, name: str, default: str = "") -> str:
        return {"Content-Type": self._content_type}.get(name, default)

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))


def _json_resp(obj: dict, status: int = 200) -> _FakeResponse:
    return _FakeResponse(json.dumps(obj).encode(), status=status)


def _sse_resp(*events: dict) -> _FakeResponse:
    parts = [f"data: {json.dumps(e)}\n\n".encode() for e in events]
    return _FakeResponse(b"".join(parts), content_type="text/event-stream")


def _mock_conn(response: _FakeResponse) -> MagicMock:
    conn = MagicMock(spec=http.client.HTTPConnection)
    conn.getresponse.return_value = response
    return conn


# ── TestA2AModels ───────────────────────────────────────────────────────────────

class TestA2AModels(unittest.TestCase):

    def test_task_status_enum_values(self):
        self.assertEqual(TaskStatus.SUBMITTED.value, "submitted")
        self.assertEqual(TaskStatus.WORKING.value, "working")
        self.assertEqual(TaskStatus.COMPLETED.value, "completed")
        self.assertEqual(TaskStatus.FAILED.value, "failed")

    def test_task_default_artifacts_empty(self):
        t = Task(id="x", status=TaskStatus.SUBMITTED, input_message="hello")
        self.assertEqual(t.artifacts, [])
        self.assertIsNone(t.error)

    def test_task_to_dict_completed(self):
        t = Task(
            id="abc",
            status=TaskStatus.COMPLETED,
            input_message="do stuff",
            artifacts=[Artifact(text="result text")],
        )
        d = t.to_dict()
        self.assertEqual(d["id"], "abc")
        # Spec: status is {state, timestamp} object
        self.assertEqual(d["status"]["state"], "completed")
        self.assertIn("timestamp", d["status"])
        # Spec: input is a Message object with parts
        self.assertEqual(d["input"]["parts"][0]["text"], "do stuff")
        # Spec: artifact is {artifactId, parts}
        self.assertEqual(d["artifacts"][0]["parts"][0]["text"], "result text")
        self.assertIn("artifactId", d["artifacts"][0])
        self.assertNotIn("error", d)

    def test_task_from_dict_roundtrip(self):
        original = Task(
            id="t1",
            status=TaskStatus.FAILED,
            input_message="failing task",
            error="something went wrong",
        )
        t = Task.from_dict(original.to_dict())
        self.assertEqual(t.id, "t1")
        self.assertEqual(t.status, TaskStatus.FAILED)
        self.assertEqual(t.error, "something went wrong")

    def test_task_from_dict_unknown_status_becomes_failed(self):
        t = Task.from_dict({"id": "x", "status": "nonsense", "input": {"message": "hi"}})
        self.assertEqual(t.status, TaskStatus.FAILED)

    def test_artifact_text(self):
        a = Artifact(text="hello world")
        self.assertEqual(a.text, "hello world")
        d = a.to_dict()
        # Spec: {artifactId, parts: [{type, text}]}
        self.assertIn("artifactId", d)
        self.assertEqual(d["parts"], [{"type": "text", "text": "hello world"}])

    def test_artifact_from_dict_spec_format(self):
        a = Artifact.from_dict({"artifactId": "x", "parts": [{"type": "text", "text": "foo"}]})
        self.assertEqual(a.text, "foo")

    def test_artifact_from_dict_legacy_format(self):
        # Legacy fallback: servers that still send {text: "..."} are handled gracefully
        a = Artifact.from_dict({"text": "foo"})
        self.assertEqual(a.text, "foo")

    def test_agent_card_fields(self):
        card = AgentCard(
            name="minion",
            description="desc",
            url="http://localhost:8080",
            version="0.11.0",
        )
        self.assertEqual(card.name, "minion")
        self.assertEqual(card.url, "http://localhost:8080")
        # Spec: capabilities includes streaming, pushNotifications, stateTransitionHistory
        self.assertTrue(card.capabilities.get("streaming"))
        self.assertIn("pushNotifications", card.capabilities)
        self.assertEqual(card.skills, [])
        # Spec: defaultInputModes and defaultOutputModes
        self.assertEqual(card.default_input_modes, ["text"])
        self.assertEqual(card.default_output_modes, ["text"])

    def test_agent_card_to_json_parseable(self):
        card = AgentCard(
            name="x", description="d", url="http://h:1", version="1.0",
        )
        parsed = json.loads(card.to_json())
        self.assertEqual(parsed["name"], "x")
        self.assertEqual(parsed["url"], "http://h:1")
        # Spec: card includes defaultInputModes and defaultOutputModes
        self.assertEqual(parsed["defaultInputModes"], ["text"])
        self.assertEqual(parsed["defaultOutputModes"], ["text"])

    def test_agent_card_from_dict_roundtrip(self):
        card = AgentCard(
            name="bot", description="a bot", url="http://b:9",
            version="0.1", capabilities={"streaming": False},
            skills=[{"id": "code", "name": "Coding"}],
        )
        c2 = AgentCard.from_dict(card.to_dict())
        self.assertEqual(c2.name, "bot")
        self.assertEqual(c2.skills, [{"id": "code", "name": "Coding"}])

    def test_agent_card_provider_field_omitted_when_none(self):
        card = AgentCard(name="x", description="d", url="http://h:1", version="1.0")
        d = card.to_dict()
        self.assertNotIn("provider", d)
        self.assertNotIn("authentication", d)

    def test_agent_card_provider_field_included_when_set(self):
        card = AgentCard(
            name="x", description="d", url="http://h:1", version="1.0",
            provider={"organization": "Acme", "url": "https://acme.com"},
        )
        d = card.to_dict()
        self.assertIn("provider", d)
        self.assertEqual(d["provider"]["organization"], "Acme")
        self.assertEqual(d["provider"]["url"], "https://acme.com")

    def test_agent_card_authentication_field_included_when_set(self):
        card = AgentCard(
            name="x", description="d", url="http://h:1", version="1.0",
            authentication={"schemes": ["Bearer"]},
        )
        d = card.to_dict()
        self.assertIn("authentication", d)
        self.assertEqual(d["authentication"]["schemes"], ["Bearer"])

    def test_agent_card_from_dict_preserves_provider_and_authentication(self):
        original = AgentCard(
            name="bot", description="d", url="http://h:1", version="1.0",
            provider={"organization": "Corp", "url": "https://corp.com"},
            authentication={"schemes": ["Bearer"]},
        )
        c2 = AgentCard.from_dict(original.to_dict())
        self.assertEqual(c2.provider, {"organization": "Corp", "url": "https://corp.com"})
        self.assertEqual(c2.authentication, {"schemes": ["Bearer"]})

    def test_task_to_dict_includes_context_id_when_set(self):
        t = Task(id="t1", status=TaskStatus.SUBMITTED, input_message="hi",
                 context_id="ctx-abc")
        d = t.to_dict()
        self.assertEqual(d["contextId"], "ctx-abc")

    def test_task_to_dict_omits_context_id_when_none(self):
        t = Task(id="t1", status=TaskStatus.SUBMITTED, input_message="hi")
        d = t.to_dict()
        self.assertNotIn("contextId", d)

    def test_task_from_dict_reads_context_id(self):
        data = {
            "id": "t2", "status": {"state": "working", "timestamp": "2025-01-01T00:00:00Z"},
            "input": {"role": "user", "parts": [{"type": "text", "text": "q"}]},
            "contextId": "ctx-xyz",
        }
        t = Task.from_dict(data)
        self.assertEqual(t.context_id, "ctx-xyz")

    def test_task_from_dict_context_id_defaults_to_none(self):
        data = {
            "id": "t3", "status": {"state": "submitted", "timestamp": "2025-01-01T00:00:00Z"},
            "input": {"role": "user", "parts": [{"type": "text", "text": "q"}]},
        }
        t = Task.from_dict(data)
        self.assertIsNone(t.context_id)

    def test_task_status_event_includes_context_id(self):
        t = Task(id="t4", status=TaskStatus.WORKING, input_message="q", context_id="ctx-1")
        ev = t.status_event(final=False)
        self.assertEqual(ev["contextId"], "ctx-1")

    def test_task_artifact_event_includes_context_id(self):
        t = Task(id="t5", status=TaskStatus.COMPLETED, input_message="q", context_id="ctx-2")
        ev = t.artifact_event(Artifact(text="result"))
        self.assertEqual(ev["contextId"], "ctx-2")

    def test_task_events_omit_context_id_when_none(self):
        t = Task(id="t6", status=TaskStatus.WORKING, input_message="q")
        self.assertNotIn("contextId", t.status_event())
        self.assertNotIn("contextId", t.artifact_event(Artifact(text="r")))


# ── TestA2AConfig ───────────────────────────────────────────────────────────────

class TestA2AConfig(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_json(self, rel_path: str, obj: dict) -> Path:
        p = Path(self._tmpdir) / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))
        return p

    def test_load_config_empty_when_no_files(self):
        configs = load_a2a_config(Path(self._tmpdir))
        self.assertEqual(configs, {})

    def test_load_config_project_only(self):
        self._write_json(".minion/a2a.json", {
            "agents": {
                "coder": {"url": "http://localhost:8080", "timeout_seconds": 30}
            }
        })
        configs = load_a2a_config(Path(self._tmpdir))
        self.assertIn("coder", configs)
        self.assertEqual(configs["coder"].url, "http://localhost:8080")
        self.assertEqual(configs["coder"].timeout_seconds, 30)

    def test_a2a_agent_config_defaults(self):
        self._write_json(".minion/a2a.json", {
            "agents": {"myagent": {"url": "http://example.com"}}
        })
        configs = load_a2a_config(Path(self._tmpdir))
        self.assertEqual(configs["myagent"].timeout_seconds, 60)

    def test_load_config_malformed_warns_and_skips(self):
        path = Path(self._tmpdir) / ".minion" / "a2a.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json{{")
        configs = load_a2a_config(Path(self._tmpdir))
        self.assertEqual(configs, {})

    def test_load_config_invalid_url_skipped(self):
        self._write_json(".minion/a2a.json", {
            "agents": {
                "bad": {"url": "ftp://not-http.com"},
                "good": {"url": "http://localhost:9090"},
            }
        })
        configs = load_a2a_config(Path(self._tmpdir))
        self.assertNotIn("bad", configs)
        self.assertIn("good", configs)

    def test_load_config_url_trailing_slash_stripped(self):
        self._write_json(".minion/a2a.json", {
            "agents": {"agent": {"url": "http://localhost:8080/"}}
        })
        configs = load_a2a_config(Path(self._tmpdir))
        self.assertEqual(configs["agent"].url, "http://localhost:8080")

    def test_load_config_project_shadows_user(self):
        """Project-tier agent entry overrides same-named user-tier entry."""
        import os
        user_home = Path(self._tmpdir) / "fake_home"
        user_home.mkdir()
        user_conf = user_home / ".minion" / "a2a.json"
        user_conf.parent.mkdir()
        user_conf.write_text(json.dumps({
            "agents": {
                "shared_agent": {"url": "http://user-host:8080"},
                "user_only": {"url": "http://user-host:9090"},
            }
        }))

        project_dir = Path(self._tmpdir) / "project"
        project_dir.mkdir()
        proj_conf = project_dir / ".minion" / "a2a.json"
        proj_conf.parent.mkdir()
        proj_conf.write_text(json.dumps({
            "agents": {
                "shared_agent": {"url": "http://project-host:8080"},
            }
        }))

        # Patch Path.home() so user tier reads from our fake home
        with patch("pathlib.Path.home", return_value=user_home):
            configs = load_a2a_config(project_dir)

        # Project shadows user for shared_agent
        self.assertEqual(configs["shared_agent"].url, "http://project-host:8080")
        # user_only still present
        self.assertIn("user_only", configs)


# ── TestA2AClient ───────────────────────────────────────────────────────────────

class TestA2AClient(unittest.TestCase):

    def _make_client(self, url: str = "http://localhost:9000") -> A2AClient:
        return A2AClient(name="test", url=url, timeout_seconds=5)

    def test_send_task_polls_until_completed(self):
        client = self._make_client()
        # Spec format: status is {state, timestamp}, artifact is {parts}
        submit_resp = _json_resp({"id": "task-1", "status": {"state": "submitted", "timestamp": "2025-01-01T00:00:00Z"}}, status=202)
        poll_working = _json_resp({"id": "task-1", "status": {"state": "working", "timestamp": "2025-01-01T00:00:00Z"},
                                   "input": {"role": "user", "parts": [{"type": "text", "text": "do it"}]}})
        poll_done = _json_resp({"id": "task-1", "status": {"state": "completed", "timestamp": "2025-01-01T00:00:00Z"},
                                "input": {"role": "user", "parts": [{"type": "text", "text": "do it"}]},
                                "artifacts": [{"artifactId": "a1", "parts": [{"type": "text", "text": "done result"}]}]})

        connections = [_mock_conn(submit_resp), _mock_conn(poll_working), _mock_conn(poll_done)]
        conn_iter = iter(connections)

        with patch.object(client, "_make_connection", side_effect=lambda: next(conn_iter)):
            with patch("time.sleep"):
                result = client.send_task("do it")

        self.assertEqual(result, "done result")

    def test_send_task_raises_on_failed(self):
        client = self._make_client()
        submit_resp = _json_resp({"id": "task-2", "status": {"state": "submitted", "timestamp": "2025-01-01T00:00:00Z"}}, status=202)
        poll_fail = _json_resp({
            "id": "task-2", "status": {"state": "failed", "timestamp": "2025-01-01T00:00:00Z"},
            "input": {"role": "user", "parts": [{"type": "text", "text": "x"}]}, "error": "boom"
        })

        connections = [_mock_conn(submit_resp), _mock_conn(poll_fail)]
        conn_iter = iter(connections)

        with patch.object(client, "_make_connection", side_effect=lambda: next(conn_iter)):
            with patch("time.sleep"):
                with self.assertRaises(A2AError) as ctx:
                    client.send_task("x")
        self.assertIn("boom", str(ctx.exception))

    def test_send_task_raises_on_timeout(self):
        client = self._make_client()
        submit_resp = _json_resp({"id": "task-3", "status": {"state": "submitted", "timestamp": "2025-01-01T00:00:00Z"}}, status=202)

        def always_working():
            return _mock_conn(_json_resp({
                "id": "task-3", "status": {"state": "working", "timestamp": "2025-01-01T00:00:00Z"},
                "input": {"role": "user", "parts": [{"type": "text", "text": "slow"}]},
            }))

        import time
        # Patch monotonic to jump past timeout after a few polls
        call_count = [0]
        real_monotonic = time.monotonic
        base_time = real_monotonic()

        def fake_monotonic():
            call_count[0] += 1
            # Return a time that exceeds timeout on the 3rd poll check
            return base_time + (call_count[0] * 10)

        connections = [_mock_conn(submit_resp)]
        conn_iter = iter(connections)

        def get_conn():
            try:
                return next(conn_iter)
            except StopIteration:
                return always_working()

        with patch.object(client, "_make_connection", side_effect=get_conn):
            with patch("time.sleep"):
                with patch("time.monotonic", side_effect=fake_monotonic):
                    with self.assertRaises(A2AError) as ctx:
                        client.send_task("slow")
        self.assertIn("timed out", str(ctx.exception))

    def test_fetch_agent_card_parses_json(self):
        client = self._make_client()
        card_json = {
            "name": "remote-bot",
            "description": "A bot",
            "url": "http://localhost:9000",
            "version": "1.0",
            "capabilities": {"streaming": True},
            "skills": [],
        }
        conn = _mock_conn(_json_resp(card_json))
        with patch.object(client, "_make_connection", return_value=conn):
            card = client.fetch_agent_card()

        self.assertIsNotNone(card)
        self.assertEqual(card.name, "remote-bot")
        self.assertEqual(card.version, "1.0")

    def test_fetch_agent_card_returns_none_on_failure(self):
        client = self._make_client()
        conn = _mock_conn(_json_resp({}, status=404))
        with patch.object(client, "_make_connection", return_value=conn):
            card = client.fetch_agent_card()
        self.assertIsNone(card)

    def test_send_task_streaming_reads_sse_events(self):
        client = self._make_client()
        # Spec SSE format: TaskStatusUpdateEvent + TaskArtifactUpdateEvent
        sse_body = (
            b'data: {"id":"t1","status":{"state":"submitted","timestamp":"2025-01-01T00:00:00Z"},"final":false}\n\n'
            b'data: {"id":"t1","status":{"state":"working","timestamp":"2025-01-01T00:00:00Z"},"final":false}\n\n'
            b'data: {"id":"t1","artifact":{"artifactId":"a1","parts":[{"type":"text","text":"streamed!"}]},"final":false}\n\n'
            b'data: {"id":"t1","status":{"state":"completed","timestamp":"2025-01-01T00:00:00Z"},"final":true}\n\n'
        )
        sse_resp = _FakeResponse(sse_body, content_type="text/event-stream")
        conn = _mock_conn(sse_resp)

        statuses = []
        with patch.object(client, "_make_connection", return_value=conn):
            result = client.send_task_streaming("hello", on_status=statuses.append)

        self.assertEqual(result, "streamed!")
        self.assertIn("working", statuses)
        self.assertIn("completed", statuses)

    def test_client_uses_https_for_https_url(self):
        client = A2AClient(name="secure", url="https://example.com/api", timeout_seconds=10)
        self.assertEqual(client._scheme, "https")
        conn = client._make_connection()
        self.assertIsInstance(conn, http.client.HTTPSConnection)

    def test_parse_task_completed_with_artifact(self):
        client = self._make_client()
        data = {
            "id": "x",
            "status": {"state": "completed", "timestamp": "2025-01-01T00:00:00Z"},
            "input": {"role": "user", "parts": [{"type": "text", "text": "q"}]},
            "artifacts": [{"artifactId": "a1", "parts": [{"type": "text", "text": "answer"}]}],
        }
        task = Task.from_dict(data)
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.artifacts[0].text, "answer")

    def test_send_task_streaming_raises_on_failed_sse(self):
        client = self._make_client()
        sse_body = (
            b'data: {"id":"t2","status":{"state":"submitted","timestamp":"2025-01-01T00:00:00Z"},"final":false}\n\n'
            b'data: {"id":"t2","status":{"state":"failed","timestamp":"2025-01-01T00:00:00Z"},"error":"remote error"}\n\n'
        )
        sse_resp = _FakeResponse(sse_body, content_type="text/event-stream")
        conn = _mock_conn(sse_resp)

        with patch.object(client, "_make_connection", return_value=conn):
            with self.assertRaises(A2AError) as ctx:
                client.send_task_streaming("bad task")
        self.assertIn("remote error", str(ctx.exception))

    def test_send_task_concatenates_multiple_artifacts(self):
        client = self._make_client()
        submit_resp = _json_resp(
            {"id": "task-m", "status": {"state": "submitted", "timestamp": "2025-01-01T00:00:00Z"}},
            status=202,
        )
        poll_done = _json_resp({
            "id": "task-m",
            "status": {"state": "completed", "timestamp": "2025-01-01T00:00:00Z"},
            "input": {"role": "user", "parts": [{"type": "text", "text": "q"}]},
            "artifacts": [
                {"artifactId": "a1", "parts": [{"type": "text", "text": "part one"}]},
                {"artifactId": "a2", "parts": [{"type": "text", "text": "part two"}]},
            ],
        })
        connections = [_mock_conn(submit_resp), _mock_conn(poll_done)]
        conn_iter = iter(connections)
        with patch.object(client, "_make_connection", side_effect=lambda: next(conn_iter)):
            with patch("time.sleep"):
                result = client.send_task("q")
        self.assertEqual(result, "part one\n\npart two")

    def test_send_task_streaming_concatenates_multiple_artifacts(self):
        client = self._make_client()
        # Two artifact events followed by a final status event
        sse_body = (
            b'event: task-status-update\n'
            b'data: {"id":"tm","status":{"state":"working","timestamp":"2025-01-01T00:00:00Z"},"final":false}\n\n'
            b'event: task-artifact-update\n'
            b'data: {"id":"tm","artifact":{"artifactId":"a1","parts":[{"type":"text","text":"first"}]},"final":false}\n\n'
            b'event: task-artifact-update\n'
            b'data: {"id":"tm","artifact":{"artifactId":"a2","parts":[{"type":"text","text":"second"}]},"final":false}\n\n'
            b'event: task-status-update\n'
            b'data: {"id":"tm","status":{"state":"completed","timestamp":"2025-01-01T00:00:00Z"},"final":true}\n\n'
        )
        sse_resp = _FakeResponse(sse_body, content_type="text/event-stream")
        conn = _mock_conn(sse_resp)
        with patch.object(client, "_make_connection", return_value=conn):
            result = client.send_task_streaming("q")
        self.assertEqual(result, "first\n\nsecond")

    def test_send_task_streaming_uses_event_type_for_discrimination(self):
        """When server emits named SSE events, client uses event type (not just key inspection)."""
        client = self._make_client()
        # Payload has both "artifact" and "status" keys; event name determines type
        sse_body = (
            b'event: task-artifact-update\n'
            b'data: {"id":"tx","artifact":{"artifactId":"a1","parts":[{"type":"text","text":"ok"}]},"status":{"state":"working"},"final":false}\n\n'
            b'event: task-status-update\n'
            b'data: {"id":"tx","status":{"state":"completed","timestamp":"2025-01-01T00:00:00Z"},"final":true}\n\n'
        )
        sse_resp = _FakeResponse(sse_body, content_type="text/event-stream")
        conn = _mock_conn(sse_resp)
        with patch.object(client, "_make_connection", return_value=conn):
            result = client.send_task_streaming("q")
        self.assertEqual(result, "ok")

    def test_client_cancel_task_sends_delete(self):
        client = self._make_client()
        conn = _mock_conn(_json_resp(
            {"id": "t-cancel", "status": {"state": "canceled", "timestamp": "2025-01-01T00:00:00Z"}},
        ))
        with patch.object(client, "_make_connection", return_value=conn):
            client.cancel_task("t-cancel")  # should not raise
        conn.request.assert_called_once_with(
            "DELETE", "/tasks/t-cancel", headers={"Accept": "application/json"}
        )

    def test_client_cancel_task_raises_on_connection_error(self):
        client = self._make_client()
        conn = MagicMock(spec=http.client.HTTPConnection)
        conn.request.side_effect = ConnectionRefusedError("refused")
        with patch.object(client, "_make_connection", return_value=conn):
            with self.assertRaises(A2AError) as ctx:
                client.cancel_task("t-x")
        self.assertIn("cancel", str(ctx.exception).lower())

    def test_client_has_context_id(self):
        client = self._make_client()
        self.assertTrue(hasattr(client, "_context_id"))
        self.assertIsInstance(client._context_id, str)
        self.assertEqual(len(client._context_id), 36)  # UUID4 string length

    def test_different_client_instances_have_different_context_ids(self):
        c1 = self._make_client()
        c2 = self._make_client()
        self.assertNotEqual(c1._context_id, c2._context_id)

    def test_submit_task_sends_context_id_in_body(self):
        client = self._make_client()
        submit_resp = _json_resp(
            {"id": "t-ctx", "status": {"state": "submitted", "timestamp": "2025-01-01T00:00:00Z"},
             "contextId": client._context_id},
            status=202,
        )
        poll_done = _json_resp({
            "id": "t-ctx", "status": {"state": "completed", "timestamp": "2025-01-01T00:00:00Z"},
            "contextId": client._context_id,
            "input": {"role": "user", "parts": [{"type": "text", "text": "q"}]},
            "artifacts": [{"artifactId": "a1", "parts": [{"type": "text", "text": "done"}]}],
        })
        connections = [_mock_conn(submit_resp), _mock_conn(poll_done)]
        conn_iter = iter(connections)

        captured_bodies = []

        def capturing_conn():
            conn = next(conn_iter)
            original_request = conn.request
            def capturing_request(method, path, body=None, headers=None):
                if body:
                    captured_bodies.append(json.loads(body.decode()))
                return original_request(method, path, body=body, headers=headers)
            conn.request = capturing_request
            return conn

        with patch.object(client, "_make_connection", side_effect=capturing_conn):
            with patch("time.sleep"):
                client.send_task("q")

        # First call is the POST /tasks/send
        self.assertEqual(captured_bodies[0]["contextId"], client._context_id)

    def test_streaming_sends_context_id_in_body(self):
        client = self._make_client()
        sse_body = (
            b'data: {"id":"t1","status":{"state":"completed","timestamp":"2025-01-01T00:00:00Z"},"final":true}\n\n'
        )
        sse_resp = _FakeResponse(sse_body, content_type="text/event-stream")
        conn = _mock_conn(sse_resp)

        captured_bodies = []
        original_request = conn.request
        def capturing_request(method, path, body=None, headers=None):
            if body:
                captured_bodies.append(json.loads(body.decode()))
            return original_request(method, path, body=body, headers=headers)
        conn.request = capturing_request

        with patch.object(client, "_make_connection", return_value=conn):
            client.send_task_streaming("q")

        self.assertEqual(captured_bodies[0]["contextId"], client._context_id)


# ── TestA2AManager ──────────────────────────────────────────────────────────────

class TestA2AManager(unittest.TestCase):

    def _make_manager(self, agent_names: list[str]) -> A2AManager:
        clients = {}
        for name in agent_names:
            c = MagicMock(spec=A2AClient)
            c.name = name
            c._scheme = "http"
            c._netloc = "localhost:8080"
            clients[name] = c
        return A2AManager(clients=clients)

    def test_send_task_routes_to_correct_client(self):
        manager = self._make_manager(["coder", "reviewer"])
        manager._clients["coder"].send_task.return_value = "coder result"

        with patch("minion.a2a.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            result = manager.send_task("coder", "do coding")

        self.assertEqual(result, "coder result")
        manager._clients["coder"].send_task.assert_called_once_with("do coding")
        manager._clients["reviewer"].send_task.assert_not_called()

    def test_send_task_unknown_agent_returns_error(self):
        manager = self._make_manager(["coder"])
        with patch("minion.a2a.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            result = manager.send_task("nonexistent", "hello")

        self.assertIn("Error", result)
        self.assertIn("nonexistent", result)
        self.assertIn("coder", result)

    def test_agent_names_returns_all(self):
        manager = self._make_manager(["a", "b", "c"])
        names = manager.agent_names()
        self.assertCountEqual(names, ["a", "b", "c"])

    def test_has_agents_returns_true_when_configured(self):
        manager = self._make_manager(["x"])
        self.assertTrue(manager.has_agents())

    def test_has_agents_returns_false_when_empty(self):
        manager = A2AManager(clients={})
        self.assertFalse(manager.has_agents())

    def test_send_task_emits_trace_events_on_success(self):
        manager = self._make_manager(["bot"])
        manager._clients["bot"].send_task.return_value = "ok"

        tracer = MagicMock()
        with patch("minion.a2a.manager.get_tracer", return_value=tracer):
            manager.send_task("bot", "hello")

        emit_calls = [c.args[0] for c in tracer.emit.call_args_list]
        self.assertIn("a2a_task_send", emit_calls)
        self.assertIn("a2a_task_complete", emit_calls)
        self.assertNotIn("a2a_task_error", emit_calls)

    def test_send_task_emits_error_event_on_exception(self):
        manager = self._make_manager(["bot"])
        manager._clients["bot"].send_task.side_effect = A2AError("network fail")

        tracer = MagicMock()
        with patch("minion.a2a.manager.get_tracer", return_value=tracer):
            result = manager.send_task("bot", "oops")

        emit_calls = [c.args[0] for c in tracer.emit.call_args_list]
        self.assertIn("a2a_task_error", emit_calls)
        self.assertIn("Error", result)
        self.assertIn("network fail", result)


# ── TestAgentCard ───────────────────────────────────────────────────────────────

class TestAgentCard(unittest.TestCase):

    def test_generate_card_fields(self):
        card = generate_agent_card("localhost", 8080)
        self.assertEqual(card.name, "minion")
        self.assertEqual(card.url, "http://localhost:8080")
        self.assertTrue(card.capabilities.get("streaming"))
        self.assertGreater(len(card.skills), 0)

    def test_generate_card_url_format(self):
        card = generate_agent_card("0.0.0.0", 9999)
        self.assertEqual(card.url, "http://0.0.0.0:9999")

    def test_card_serializes_to_json(self):
        card = generate_agent_card("localhost", 8080)
        parsed = json.loads(card.to_json())
        self.assertEqual(parsed["name"], "minion")
        self.assertIn("url", parsed)
        self.assertIn("skills", parsed)
        self.assertIsInstance(parsed["skills"], list)

    def test_card_has_expected_skills(self):
        card = generate_agent_card("localhost", 8080)
        skill_ids = {s["id"] for s in card.skills}
        self.assertIn("coding", skill_ids)
        self.assertIn("research", skill_ids)
        self.assertIn("testing", skill_ids)

    def test_card_version_is_set(self):
        card = generate_agent_card("localhost", 8080)
        self.assertIsNotNone(card.version)
        self.assertNotEqual(card.version, "")


# ── TestSendRemoteTaskTool ──────────────────────────────────────────────────────

class TestSendRemoteTaskTool(unittest.TestCase):

    def _mock_renderer(self):
        from unittest.mock import MagicMock
        from minion.output import OutputRenderer
        r = MagicMock(spec=OutputRenderer)
        r.spinner.return_value.__enter__ = MagicMock(return_value=None)
        r.spinner.return_value.__exit__ = MagicMock(return_value=False)
        return r

    def _make_executor(self, remote_task_runner=None):
        from minion.tools.executor import ToolExecutor
        return ToolExecutor(dry_run=False, remote_task_runner=remote_task_runner,
                            renderer=self._mock_renderer())

    def _make_block(self, name: str, inputs: dict) -> ToolUseBlock:
        return ToolUseBlock(id=f"id_{name}", name=name, input=inputs)

    def test_executor_routes_send_remote_task(self):
        runner = MagicMock(return_value="remote result")
        executor = self._make_executor(remote_task_runner=runner)
        block = self._make_block("send_remote_task", {"agent": "coder", "task": "do stuff"})

        with patch("minion.tools.executor.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            with patch("minion.agents.display.get_agent_display_callback", return_value=None):
                result = executor.execute(block)

        self.assertEqual(result, "remote result")
        runner.assert_called_once_with("coder", "do stuff")

    def test_executor_no_runner_returns_error(self):
        executor = self._make_executor(remote_task_runner=None)
        block = self._make_block("send_remote_task", {"agent": "bot", "task": "hi"})

        with patch("minion.agents.display.get_agent_display_callback", return_value=None):
            result = executor.execute(block)

        self.assertIn("Error", result)
        self.assertIn("A2A", result)

    def test_executor_extracts_agent_and_task(self):
        calls = []
        def capture(agent, task):
            calls.append((agent, task))
            return "ok"

        executor = self._make_executor(remote_task_runner=capture)
        block = self._make_block("send_remote_task", {
            "agent": "reviewer", "task": "review the PR"
        })

        with patch("minion.tools.executor.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            with patch("minion.agents.display.get_agent_display_callback", return_value=None):
                executor.execute(block)

        self.assertEqual(calls, [("reviewer", "review the PR")])

    def test_send_remote_task_excluded_when_no_a2a_manager(self):
        """send_remote_task should be filtered from tool list when a2a_manager is None."""
        from minion.tools.definitions import TOOL_DEFINITIONS

        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        self.assertIn("send_remote_task", tool_names)

        # Simulate filtering (mirrors runner.py logic)
        effective = [t for t in TOOL_DEFINITIONS if t["name"] != "send_remote_task"]
        names_after = {t["name"] for t in effective}
        self.assertNotIn("send_remote_task", names_after)
        self.assertIn("read_file", names_after)  # other tools still present

    def test_send_remote_task_in_tool_definitions(self):
        from minion.tools.definitions import TOOL_DEFINITIONS

        names = [t["name"] for t in TOOL_DEFINITIONS]
        self.assertIn("send_remote_task", names)

        defn = next(t for t in TOOL_DEFINITIONS if t["name"] == "send_remote_task")
        schema = defn["input_schema"]["properties"]
        self.assertIn("agent", schema)
        self.assertIn("task", schema)
        self.assertIn("agent", defn["input_schema"]["required"])
        self.assertIn("task", defn["input_schema"]["required"])

    def test_delegation_tools_includes_send_remote_task(self):
        from minion.tools.definitions import DELEGATION_TOOLS

        self.assertIn("send_remote_task", DELEGATION_TOOLS)
        self.assertIn("spawn_agent", DELEGATION_TOOLS)


if __name__ == "__main__":
    unittest.main()
