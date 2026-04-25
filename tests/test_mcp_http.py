"""Tests for SSEParser and MCPHTTPClient.

Testing strategy:
  - SSEParser: pure unit tests — feed strings directly, assert SSEEvent output
  - MCPHTTPClient: mock http.client.HTTPConnection to avoid real network calls;
    test _send_request, _send_notification, shutdown, and GET stream dispatch
  - MCPServerConfig.transport property: simple dataclass test
"""

from __future__ import annotations

import http.client
import io
import json
import threading
import time
import unittest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch, call

from minion.mcp.config import MCPServerConfig
from minion.mcp.http_client import MCPHTTPClient
from minion.mcp.sse import SSEEvent, SSEParser


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_http_config(url: str = "http://localhost:9000/mcp") -> MCPServerConfig:
    return MCPServerConfig(name="workspace", url=url)


def _json_line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _sse_bytes(*events: dict, event_ids: Optional[list[Optional[str]]] = None) -> bytes:
    """Build a minimal SSE byte stream from a list of JSON dicts."""
    parts: list[bytes] = []
    for i, obj in enumerate(events):
        eid = event_ids[i] if event_ids else None
        if eid is not None:
            parts.append(f"id: {eid}\n".encode())
        parts.append(f"data: {json.dumps(obj)}\n\n".encode())
    return b"".join(parts)


class _FakeResponse:
    """Lightweight stand-in for http.client.HTTPResponse — properly iterable."""

    def __init__(self, body: bytes, content_type: str = "application/json",
                 headers: Optional[dict] = None, status: int = 200) -> None:
        self.status = status
        self._body = body
        self._content_type = content_type
        self._headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: str = "") -> str:
        return {"Content-Type": self._content_type, **self._headers}.get(name, default)

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))


def _mock_response(body: bytes, content_type: str = "application/json",
                   headers: Optional[dict] = None) -> _FakeResponse:
    """Return a fake HTTPResponse with the given body, iterable line-by-line."""
    return _FakeResponse(body, content_type, headers)


def _mock_connection(response: MagicMock) -> MagicMock:
    conn = MagicMock(spec=http.client.HTTPConnection)
    conn.getresponse.return_value = response
    return conn


# ── TestSSEParser ──────────────────────────────────────────────────────────────

class TestSSEParser(unittest.TestCase):

    def test_single_event_minimal(self):
        parser = SSEParser()
        self.assertIsNone(parser.feed("data: hello"))
        event = parser.feed("")
        self.assertIsNotNone(event)
        self.assertEqual(event.data, "hello")
        self.assertIsNone(event.id)
        self.assertEqual(event.event, "message")

    def test_multi_data_lines_joined(self):
        parser = SSEParser()
        parser.feed("data: line1")
        parser.feed("data: line2")
        event = parser.feed("")
        self.assertEqual(event.data, "line1\nline2")

    def test_id_and_event_type(self):
        parser = SSEParser()
        parser.feed("id: 42")
        parser.feed("event: update")
        parser.feed("data: {}")
        event = parser.feed("")
        self.assertEqual(event.id, "42")
        self.assertEqual(event.event, "update")

    def test_empty_event_not_dispatched(self):
        """A blank line with no preceding data: lines returns None."""
        parser = SSEParser()
        result = parser.feed("")
        self.assertIsNone(result)

    def test_comment_line_ignored(self):
        parser = SSEParser()
        parser.feed(": this is a comment")
        parser.feed("data: real")
        event = parser.feed("")
        self.assertEqual(event.data, "real")

    def test_leading_space_stripped_from_value(self):
        """Single leading space after colon is stripped per SSE spec."""
        parser = SSEParser()
        parser.feed("data: has leading space")
        event = parser.feed("")
        self.assertEqual(event.data, "has leading space")

    def test_unknown_field_ignored(self):
        parser = SSEParser()
        parser.feed("retry: 5000")
        parser.feed("data: payload")
        event = parser.feed("")
        self.assertEqual(event.data, "payload")

    def test_iter_events_from_bytes(self):
        raw = _sse_bytes({"a": 1}, {"b": 2}, event_ids=["1", "2"])
        events = list(SSEParser.iter_events(iter(raw.splitlines(keepends=True))))
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[0].data), {"a": 1})
        self.assertEqual(events[0].id, "1")
        self.assertEqual(json.loads(events[1].data), {"b": 2})
        self.assertEqual(events[1].id, "2")

    def test_iter_events_empty_stream(self):
        events = list(SSEParser.iter_events(iter([])))
        self.assertEqual(events, [])

    def test_parser_resets_after_dispatch(self):
        """State should not bleed between events."""
        parser = SSEParser()
        parser.feed("id: 1")
        parser.feed("event: foo")
        parser.feed("data: first")
        parser.feed("")
        # second event — id and event type should reset to defaults
        parser.feed("data: second")
        event = parser.feed("")
        self.assertIsNone(event.id)
        self.assertEqual(event.event, "message")
        self.assertEqual(event.data, "second")


# ── TestMCPServerConfigTransport ──────────────────────────────────────────────

class TestMCPServerConfigTransport(unittest.TestCase):

    def test_http_transport_when_url_set(self):
        cfg = MCPServerConfig(name="srv", url="http://localhost:9000/mcp")
        self.assertEqual(cfg.transport, "http")

    def test_stdio_transport_when_command_set(self):
        cfg = MCPServerConfig(name="srv", command=["python", "server.py"])
        self.assertEqual(cfg.transport, "stdio")

    def test_stdio_when_both_empty(self):
        cfg = MCPServerConfig(name="srv")
        self.assertEqual(cfg.transport, "stdio")


# ── TestMCPHTTPClientInit ──────────────────────────────────────────────────────

class TestMCPHTTPClientInit(unittest.TestCase):

    def test_initialize_transport_parses_url(self):
        client = MCPHTTPClient("ws", _make_http_config("http://myhost:8080/api/mcp"))
        client._initialize_transport()
        self.assertEqual(client._host, "myhost")
        self.assertEqual(client._port, 8080)
        self.assertEqual(client._path, "/api/mcp")
        self.assertEqual(client._scheme, "http")

    def test_initialize_transport_defaults_port_80(self):
        client = MCPHTTPClient("ws", _make_http_config("http://example.com/mcp"))
        client._initialize_transport()
        self.assertEqual(client._port, 80)

    def test_initialize_transport_defaults_port_443_for_https(self):
        client = MCPHTTPClient("ws", _make_http_config("https://example.com/mcp"))
        client._initialize_transport()
        self.assertEqual(client._port, 443)
        self.assertEqual(client._scheme, "https")

    def test_initialize_transport_raises_on_bad_scheme(self):
        client = MCPHTTPClient("ws", _make_http_config("ftp://bad/mcp"))
        with self.assertRaises(RuntimeError) as ctx:
            client._initialize_transport()
        self.assertIn("unsupported URL scheme", str(ctx.exception))

    def test_initialize_transport_raises_on_missing_hostname(self):
        client = MCPHTTPClient("ws", _make_http_config("http:///mcp"))
        with self.assertRaises(RuntimeError) as ctx:
            client._initialize_transport()
        self.assertIn("no hostname", str(ctx.exception))


# ── TestMCPHTTPClientSendRequest ──────────────────────────────────────────────

class TestMCPHTTPClientSendRequest(unittest.TestCase):

    def _client_with_transport(self) -> MCPHTTPClient:
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        return client

    def test_send_request_json_response(self):
        client = self._client_with_transport()
        expected = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
        mock_resp = _mock_response(json.dumps(expected).encode(), "application/json")
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            result = client._send_request("tools/list")

        self.assertEqual(result, expected)
        mock_conn.request.assert_called_once()
        args = mock_conn.request.call_args
        # method, path, body, headers
        self.assertEqual(args[0][0], "POST")
        self.assertEqual(args[0][1], "/mcp")

    def test_send_request_includes_content_type_header(self):
        client = self._client_with_transport()
        mock_resp = _mock_response(
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
        )
        mock_conn = _mock_connection(mock_resp)
        with patch.object(client, "_make_connection", return_value=mock_conn):
            client._send_request("tools/list")
        headers = mock_conn.request.call_args[1].get("headers") or mock_conn.request.call_args[0][3]
        self.assertEqual(headers.get("Content-Type"), "application/json")

    def test_send_request_captures_session_id(self):
        client = self._client_with_transport()
        self.assertIsNone(client._session_id)
        resp_body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}}}).encode()
        mock_resp = _mock_response(resp_body, headers={"Mcp-Session-Id": "abc123"})
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client._send_request("initialize", {})

        self.assertEqual(client._session_id, "abc123")

    def test_send_request_sends_session_id_on_subsequent_requests(self):
        client = self._client_with_transport()
        client._session_id = "sess-xyz"
        mock_resp = _mock_response(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client._send_request("tools/list")

        _, kwargs = mock_conn.request.call_args
        headers = kwargs.get("headers") or mock_conn.request.call_args[0][3]
        self.assertEqual(headers.get("Mcp-Session-Id"), "sess-xyz")

    def test_send_request_does_not_send_session_id_when_unset(self):
        client = self._client_with_transport()
        mock_resp = _mock_response(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode())
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client._send_request("tools/list")

        _, kwargs = mock_conn.request.call_args
        headers = kwargs.get("headers") or mock_conn.request.call_args[0][3]
        self.assertNotIn("Mcp-Session-Id", headers)

    def test_send_request_sse_response(self):
        """When server responds with text/event-stream, parse SSE to get response."""
        client = self._client_with_transport()
        req_id = client._next_id  # preview what id will be used
        sse_body = _sse_bytes({"jsonrpc": "2.0", "id": req_id, "result": {"found": True}})
        mock_resp = _mock_response(sse_body, "text/event-stream")
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            result = client._send_request("search_files", {"query": "TODO"})

        self.assertEqual(result["result"]["found"], True)

    def test_send_request_raises_on_connection_error(self):
        client = self._client_with_transport()
        mock_conn = MagicMock(spec=http.client.HTTPConnection)
        mock_conn.request.side_effect = OSError("connection refused")

        with patch.object(client, "_make_connection", return_value=mock_conn):
            with self.assertRaises(IOError) as ctx:
                client._send_request("tools/list")
        self.assertIn("HTTP transport error", str(ctx.exception))

    def test_send_request_sse_dispatches_embedded_notifications(self):
        """Notifications (no id) embedded in SSE POST response are dispatched."""
        client = self._client_with_transport()
        req_id = client._next_id
        notification = {"jsonrpc": "2.0", "method": "notifications/message",
                        "params": {"level": "info", "data": "progress"}}
        response = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        sse_body = _sse_bytes(notification, response)
        mock_resp = _mock_response(sse_body, "text/event-stream")
        mock_conn = _mock_connection(mock_resp)

        received: list[dict] = []
        client.notification_callback = lambda name, params: received.append(params)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client._send_request("tools/call", {})

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["level"], "info")


# ── TestMCPHTTPClientNotification ─────────────────────────────────────────────

class TestMCPHTTPClientSendNotification(unittest.TestCase):

    def test_send_notification_posts_without_id(self):
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        mock_resp = MagicMock(spec=http.client.HTTPResponse)
        mock_resp.read.return_value = b""
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client._send_notification("notifications/initialized")

        ca = mock_conn.request.call_args
        # body is a keyword arg: conn.request("POST", path, body=..., headers=...)
        raw_body = ca.kwargs.get("body") or ca[1].get("body")
        body = json.loads(raw_body.decode())
        self.assertNotIn("id", body)
        self.assertEqual(body["method"], "notifications/initialized")

    def test_send_notification_ignores_connection_errors(self):
        """Fire-and-forget — connection errors must not raise."""
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        mock_conn = MagicMock(spec=http.client.HTTPConnection)
        mock_conn.request.side_effect = OSError("refused")

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client._send_notification("notifications/initialized")  # must not raise


# ── TestMCPHTTPClientShutdown ──────────────────────────────────────────────────

class TestMCPHTTPClientShutdown(unittest.TestCase):

    def test_shutdown_sends_delete_with_session_id(self):
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        client._session_id = "sess-del"

        mock_resp = MagicMock(spec=http.client.HTTPResponse)
        mock_resp.read.return_value = b""
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client.shutdown()

        args = mock_conn.request.call_args[0]
        self.assertEqual(args[0], "DELETE")
        self.assertEqual(args[1], "/mcp")
        headers = mock_conn.request.call_args[1].get("headers") or mock_conn.request.call_args[0][3]
        self.assertEqual(headers.get("Mcp-Session-Id"), "sess-del")

    def test_shutdown_skips_delete_when_no_session(self):
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        # no session_id set

        with patch.object(client, "_make_connection") as mock_make:
            client.shutdown()
        mock_make.assert_not_called()

    def test_shutdown_ignores_delete_errors(self):
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        client._session_id = "sess-err"
        mock_conn = MagicMock(spec=http.client.HTTPConnection)
        mock_conn.request.side_effect = OSError("gone")

        with patch.object(client, "_make_connection", return_value=mock_conn):
            client.shutdown()  # must not raise


# ── TestMCPHTTPClientGetStream ─────────────────────────────────────────────────

class TestMCPHTTPClientGetStream(unittest.TestCase):

    def test_get_stream_dispatches_notifications(self):
        """Notifications on the GET SSE stream are delivered to notification_callback."""
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        client._session_id = "sess-stream"

        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/workspace/file_changed",
            "params": {"path": "foo.py", "event": "modified"},
        }
        sse_body = _sse_bytes(notification, event_ids=["7"])

        mock_resp = _mock_response(sse_body, "text/event-stream")
        mock_conn = _mock_connection(mock_resp)

        received: list[dict] = []
        client.notification_callback = lambda name, params: received.append(params)

        # First call returns the SSE stream; second call raises so the loop exits
        call_count = [0]
        def make_conn():
            call_count[0] += 1
            if call_count[0] > 1:
                raise OSError("server gone — exit loop")
            return mock_conn

        with patch.object(client, "_make_connection", side_effect=make_conn):
            t = threading.Thread(target=client._get_stream_loop)
            t.start()
            t.join(timeout=2)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["path"], "foo.py")

    def test_get_stream_exits_on_non_200(self):
        """If server returns non-200 on GET, thread exits cleanly (server doesn't support streams)."""
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()

        mock_resp = _FakeResponse(b"", status=405)  # Method Not Allowed
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            t = threading.Thread(target=client._get_stream_loop)
            t.start()
            t.join(timeout=2)

        self.assertFalse(t.is_alive())

    def test_get_stream_sends_last_event_id_on_reconnect(self):
        """Last-Event-ID is sent so server can replay missed events on reconnect."""
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        client._last_event_id = "42"

        mock_resp = _FakeResponse(b"", status=200)  # empty SSE stream — exits immediately
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            t = threading.Thread(target=client._get_stream_loop)
            t.start()
            t.join(timeout=2)

        headers = mock_conn.request.call_args[1].get("headers") or mock_conn.request.call_args[0][3]
        self.assertEqual(headers.get("Last-Event-ID"), "42")

    def test_get_stream_updates_last_event_id(self):
        """_last_event_id is updated as SSE events with id: fields arrive."""
        client = MCPHTTPClient("ws", _make_http_config())
        client._initialize_transport()
        client._session_id = "s"

        notification = {"jsonrpc": "2.0", "method": "notifications/test", "params": {}}
        sse_body = _sse_bytes(notification, event_ids=["99"])
        mock_resp = _mock_response(sse_body, "text/event-stream")
        mock_conn = _mock_connection(mock_resp)

        with patch.object(client, "_make_connection", return_value=mock_conn):
            t = threading.Thread(target=client._get_stream_loop)
            t.start()
            t.join(timeout=2)

        self.assertEqual(client._last_event_id, "99")


# ── TestMCPConfigLoader (HTTP) ─────────────────────────────────────────────────

class TestMCPConfigLoaderHTTP(unittest.TestCase):

    def test_load_http_config(self):
        from minion.mcp.config import load_mcp_config
        import tempfile, json, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = pathlib.Path(tmp) / ".minion"
            cfg_dir.mkdir()
            (cfg_dir / "mcp.json").write_text(json.dumps({
                "servers": {
                    "workspace": {
                        "url": "http://localhost:9000/mcp",
                    }
                }
            }))
            configs = load_mcp_config(pathlib.Path(tmp))

        self.assertIn("workspace", configs)
        cfg = configs["workspace"]
        self.assertEqual(cfg.url, "http://localhost:9000/mcp")
        self.assertEqual(cfg.transport, "http")
        self.assertEqual(cfg.command, [])

    def test_skip_invalid_url_scheme(self):
        from minion.mcp.config import load_mcp_config
        import tempfile, json, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = pathlib.Path(tmp) / ".minion"
            cfg_dir.mkdir()
            (cfg_dir / "mcp.json").write_text(json.dumps({
                "servers": {
                    "bad": {"url": "ftp://localhost/mcp"}
                }
            }))
            configs = load_mcp_config(pathlib.Path(tmp))

        self.assertNotIn("bad", configs)

    def test_skip_server_with_neither_command_nor_url(self):
        from minion.mcp.config import load_mcp_config
        import tempfile, json, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = pathlib.Path(tmp) / ".minion"
            cfg_dir.mkdir()
            (cfg_dir / "mcp.json").write_text(json.dumps({
                "servers": {
                    "empty": {"confirm_all": True}
                }
            }))
            configs = load_mcp_config(pathlib.Path(tmp))

        self.assertNotIn("empty", configs)


if __name__ == "__main__":
    unittest.main()
