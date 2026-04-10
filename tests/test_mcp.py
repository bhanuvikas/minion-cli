"""Tests for the MCP (Model Context Protocol) client system.

Strategy:
- Tests that exercise business logic (connect handshake, tool/resource/prompt parsing,
  routing) mock _send_request directly. This keeps tests focused on what they actually
  test and avoids needing a realistic thread/process mock.
- Tests for dead-process detection use a real thread with an EOF-returning mock stdout,
  then join the thread before asserting.
- Notification dispatch tests call _handle_notification() directly — no thread or
  subprocess needed to verify the callback contract.
- Manager and executor tests inject mock MCPClient instances directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minion.mcp.config import MCPServerConfig, load_mcp_config
from minion.mcp.client import MCPClient, MCPTool
from minion.mcp.manager import MCPManager
from minion.tools.executor import ToolExecutor
from minion.llm.base import ToolUseBlock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_process_eof():
    """Mock Popen process whose stdout returns EOF immediately.

    Used for tests that mock _send_request: the reader thread starts, sees EOF,
    and exits cleanly without interfering with the patched _send_request calls.
    """
    process = MagicMock()
    process.stdin = MagicMock()
    process.stdout = MagicMock()
    process.stdout.readline.return_value = b""
    process.returncode = None
    return process


def _server_config(name: str = "test", command: list | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=command or ["echo", "hello"],
        env={},
        confirm_all=False,
    )


def _initialize_response(req_id: int = 1, capabilities: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": capabilities if capabilities is not None else {"tools": {}},
            "serverInfo": {"name": "test-server", "version": "0.1.0"},
        },
    }


def _tools_list_response(tools: list[dict], req_id: int = 2) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}


def _tool_call_response(text: str, req_id: int = 3, is_error: bool = False) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }


def _resources_list_response(resources: list[dict], req_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": resources}}


def _prompts_list_response(prompts: list[dict], req_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": prompts}}


def _resource_read_response(uri: str, text: str, req_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]},
    }


def _prompt_get_response(messages: list[dict], req_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"description": "test prompt", "messages": messages},
    }


_SAMPLE_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "annotations": {"destructiveHint": True},
    },
]

_SAMPLE_RESOURCES = [
    {"uri": "notes://ideas", "name": "ideas", "description": "Note: ideas", "mimeType": "text/plain"},
    {"uri": "notes://todo", "name": "todo", "description": "Note: todo", "mimeType": "text/plain"},
]

_SAMPLE_PROMPTS = [
    {
        "name": "summarize_notes",
        "description": "Summarize all notes",
        "arguments": [],
    },
    {
        "name": "find_related",
        "description": "Find related notes",
        "arguments": [{"name": "topic", "description": "Topic", "required": True}],
    },
]

# Capabilities dict advertised by a server that supports tools + resources + prompts
_FULL_CAPS = {"tools": {}, "resources": {}, "prompts": {}}


# ── TestMCPConfig ─────────────────────────────────────────────────────────────

class TestMCPConfig:
    def test_load_config_missing_files_returns_empty(self, tmp_path):
        result = load_mcp_config(cwd=tmp_path)
        assert result == {}

    def test_load_config_user_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        user_dir = tmp_path / "home" / ".minion"
        user_dir.mkdir(parents=True)
        (user_dir / "mcp.json").write_text(json.dumps({
            "servers": {
                "notes": {"command": ["python", "notes.py"], "env": {}}
            }
        }))
        result = load_mcp_config(cwd=tmp_path)
        assert "notes" in result
        assert result["notes"].command == ["python", "notes.py"]
        assert result["notes"].confirm_all is False

    def test_load_config_project_only(self, tmp_path):
        project_minion = tmp_path / ".minion"
        project_minion.mkdir()
        (project_minion / "mcp.json").write_text(json.dumps({
            "servers": {
                "git": {"command": ["npx", "mcp-git"], "env": {}}
            }
        }))
        result = load_mcp_config(cwd=tmp_path)
        assert "git" in result
        assert result["git"].command == ["npx", "mcp-git"]

    def test_load_config_project_shadows_user(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        user_dir = tmp_path / "home" / ".minion"
        user_dir.mkdir(parents=True)
        (user_dir / "mcp.json").write_text(json.dumps({
            "servers": {"notes": {"command": ["python", "user_notes.py"]}}
        }))
        project_minion = tmp_path / ".minion"
        project_minion.mkdir()
        (project_minion / "mcp.json").write_text(json.dumps({
            "servers": {"notes": {"command": ["python", "project_notes.py"]}}
        }))
        result = load_mcp_config(cwd=tmp_path)
        assert result["notes"].command == ["python", "project_notes.py"]

    def test_load_config_malformed_json_warns_and_skips_tier(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        user_dir = tmp_path / "home" / ".minion"
        user_dir.mkdir(parents=True)
        (user_dir / "mcp.json").write_text("not valid json {{{")

        project_minion = tmp_path / ".minion"
        project_minion.mkdir()
        (project_minion / "mcp.json").write_text(json.dumps({
            "servers": {"good": {"command": ["echo"]}}
        }))
        result = load_mcp_config(cwd=tmp_path)
        assert "good" in result


# ── TestMCPClient ─────────────────────────────────────────────────────────────

class TestMCPClient:
    """Tests for MCPClient connect handshake, tool parsing, and tool calling.

    All tests mock _send_request directly. The reader thread starts and sees
    EOF immediately (from _mock_process_eof), so it exits without interfering.
    """

    def _connected_client(self, name="test", send_request_responses=None, capabilities=None):
        """Connect a client using mocked _send_request responses."""
        caps = capabilities or {"tools": {}}
        responses = send_request_responses or [
            _initialize_response(req_id=1, capabilities=caps),
            _tools_list_response(_SAMPLE_TOOLS, req_id=2),
        ]
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient(name, _server_config(name=name))
            with patch.object(client, "_send_request", side_effect=responses), \
                 patch.object(client, "_send_notification"):
                client.connect()
        return client

    def test_connect_sends_initialize_and_lists_tools(self):
        client = self._connected_client()
        assert len(client.tools) == 2
        assert client.tools[0].name == "read_file"
        assert client.tools[1].name == "delete_file"

    def test_connect_raises_on_subprocess_failure(self):
        with patch("subprocess.Popen", side_effect=FileNotFoundError("not found")):
            client = MCPClient("bad", _server_config(command=["nonexistent"]))
            with pytest.raises(RuntimeError, match="failed to start"):
                client.connect()

    def test_connect_raises_on_bad_initialize_response(self):
        bad_init = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient("test", _server_config())
            with patch.object(client, "_send_request", side_effect=[bad_init]), \
                 patch.object(client, "_send_notification"):
                with pytest.raises(RuntimeError, match="serverInfo"):
                    client.connect()

    def test_get_tool_definitions_prefixes_names(self):
        client = self._connected_client(name="myserver")
        names = [d["name"] for d in client.get_tool_definitions()]
        assert "myserver__read_file" in names
        assert "myserver__delete_file" in names

    def test_get_tool_definitions_maps_input_schema_key(self):
        client = self._connected_client(name="srv")
        for d in client.get_tool_definitions():
            assert "input_schema" in d
            assert "inputSchema" not in d

    def test_call_tool_sends_request_and_returns_text(self):
        client = self._connected_client(name="srv", send_request_responses=[
            _initialize_response(req_id=1),
            _tools_list_response(_SAMPLE_TOOLS, req_id=2),
            _tool_call_response("file contents here", req_id=3),
        ])
        with patch.object(client, "_send_request",
                          return_value=_tool_call_response("file contents here", req_id=3)):
            result = client.call_tool("read_file", {"path": "/tmp/test.txt"})
        assert result == "file contents here"

    def test_call_tool_returns_error_text_on_is_error_true(self):
        with patch.object(
            MCPClient, "_send_request",
            return_value=_tool_call_response("file not found", req_id=3, is_error=True),
        ):
            client = self._connected_client(name="srv")
            result = client.call_tool("read_file", {"path": "/does/not/exist"})
        assert result.startswith("Error:")
        assert "file not found" in result

    def test_call_tool_returns_error_string_on_dead_process(self):
        """When the reader thread has exited (server dead), call_tool returns an error string."""
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient("srv", _server_config(name="srv"))
            with patch.object(client, "_send_request", side_effect=[
                _initialize_response(req_id=1),
                _tools_list_response(_SAMPLE_TOOLS, req_id=2),
            ]), patch.object(client, "_send_notification"):
                client.connect()

        # Thread sees EOF immediately; join to confirm _thread_exited is set
        client._read_thread.join(timeout=1)
        assert client._thread_exited

        # Real _send_request now hits _thread_exited=True → IOError → error string
        result = client.call_tool("read_file", {"path": "/tmp/test.txt"})
        assert "Error:" in result

    def test_annotation_destructive_hint_parsed(self):
        client = self._connected_client()
        read_tool = next(t for t in client.tools if t.name == "read_file")
        delete_tool = next(t for t in client.tools if t.name == "delete_file")
        assert read_tool.destructive is False
        assert delete_tool.destructive is True


# ── TestMCPManager ────────────────────────────────────────────────────────────

class TestMCPManager:
    def _connected_manager(self, server_names: list[str]) -> MCPManager:
        """Build an MCPManager with pre-populated mock clients (no subprocess)."""
        manager = MCPManager()
        for name in server_names:
            client = MagicMock(spec=MCPClient)
            client.name = name
            client.tools = [
                MCPTool(
                    name="tool_a",
                    description="Tool A",
                    input_schema={"type": "object"},
                    server_name=name,
                    destructive=False,
                ),
            ]
            client.get_tool_definitions.return_value = [
                {"name": f"{name}__tool_a", "description": "Tool A", "input_schema": {"type": "object"}}
            ]
            client.is_dangerous.return_value = False
            client.call_tool.return_value = f"result from {name}"
            manager._clients[name] = client
        return manager

    def test_connect_all_skips_failing_server_gracefully(self):
        manager = MCPManager()
        configs = {
            "bad": MCPServerConfig("bad", command=["nonexistent"], env={}, confirm_all=False),
        }
        with patch("minion.mcp.manager.MCPClient") as MockClient:
            instance = MockClient.return_value
            instance.connect.side_effect = RuntimeError("command not found")
            manager.connect_all(configs)

        assert len(manager._clients) == 0

    def test_connect_all_stores_connected_clients(self):
        manager = MCPManager()
        configs = {
            "notes": MCPServerConfig("notes", command=["python", "notes.py"], env={}, confirm_all=False),
        }
        with patch("minion.mcp.manager.MCPClient") as MockClient:
            instance = MockClient.return_value
            instance.connect.return_value = None
            instance.tools = []
            manager.connect_all(configs)

        assert "notes" in manager._clients

    def test_get_tool_definitions_merges_all_clients(self):
        manager = self._connected_manager(["server_a", "server_b"])
        names = [d["name"] for d in manager.get_tool_definitions()]
        assert "server_a__tool_a" in names
        assert "server_b__tool_a" in names

    def test_call_tool_routes_to_correct_client(self):
        manager = self._connected_manager(["notes", "git"])
        manager.call_tool("notes__tool_a", {"arg": "val"})
        manager._clients["notes"].call_tool.assert_called_once_with("tool_a", {"arg": "val"})
        manager._clients["git"].call_tool.assert_not_called()

    def test_call_tool_unknown_server_returns_error_string(self):
        manager = self._connected_manager(["notes"])
        result = manager.call_tool("nonexistent__some_tool", {})
        assert "Error:" in result
        assert "nonexistent" in result

    def test_shutdown_calls_all_clients(self):
        manager = self._connected_manager(["a", "b"])
        manager.shutdown()
        assert not manager.has_tools()


# ── TestMCPAwareExecutor ──────────────────────────────────────────────────────

class TestMCPAwareExecutor:
    def _tool_block(self, name: str, inputs: dict) -> ToolUseBlock:
        return ToolUseBlock(id="abc123", name=name, input=inputs)

    def test_execute_native_tool_still_works_with_mcp_manager_present(self):
        mock_manager = MagicMock()
        executor = ToolExecutor(mcp_manager=mock_manager)
        result = executor.execute(self._tool_block("read_file", {"path": __file__}))
        mock_manager.call_tool.assert_not_called()
        assert isinstance(result, str)

    def test_execute_mcp_tool_routes_via_manager(self):
        mock_manager = MagicMock()
        mock_manager.is_dangerous.return_value = False
        mock_manager.call_tool.return_value = "created note!"
        executor = ToolExecutor(mcp_manager=mock_manager)
        result = executor.execute(self._tool_block("notes__create_note", {"title": "hi"}))
        mock_manager.call_tool.assert_called_once_with("notes__create_note", {"title": "hi"})
        assert result == "created note!"

    def test_execute_mcp_tool_manager_none_returns_unknown_tool_error(self):
        executor = ToolExecutor(mcp_manager=None)
        result = executor.execute(self._tool_block("notes__create_note", {}))
        assert "Unknown tool" in result or "Error" in result

    def test_execute_unknown_tool_no_mcp_manager_returns_error(self):
        executor = ToolExecutor(mcp_manager=None)
        result = executor.execute(self._tool_block("totally_unknown_tool", {}))
        assert "Unknown tool" in result or "Error" in result


# ── TestMCPClientResources ────────────────────────────────────────────────────

class TestMCPClientResources:
    def _client_with_resources(self):
        """Connect a client that advertises tools + resources + prompts."""
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient("notes", _server_config(name="notes"))
            with patch.object(client, "_send_request", side_effect=[
                _initialize_response(req_id=1, capabilities=_FULL_CAPS),
                _tools_list_response([], req_id=2),
                _prompts_list_response([], req_id=3),
            ]), patch.object(client, "_send_notification"):
                client.connect()
        return client

    def test_connect_sets_has_resources_capability_when_present(self):
        client = self._client_with_resources()
        assert client._has_resources_capability is True

    def test_connect_skips_resources_when_capability_absent(self):
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient("notes", _server_config(name="notes"))
            with patch.object(client, "_send_request", side_effect=[
                _initialize_response(req_id=1, capabilities={"tools": {}}),
                _tools_list_response([], req_id=2),
            ]), patch.object(client, "_send_notification"):
                client.connect()
        assert client._has_resources_capability is False
        assert client.prompts == []

    def test_list_resources_returns_live_resource_list(self):
        client = self._client_with_resources()
        with patch.object(client, "_send_request",
                          return_value=_resources_list_response(_SAMPLE_RESOURCES, req_id=4)):
            resources = client.list_resources()
        assert len(resources) == 2
        assert resources[0].uri == "notes://ideas"
        assert resources[1].name == "todo"
        assert resources[0].server_name == "notes"

    def test_read_resource_returns_text_content(self):
        client = self._client_with_resources()
        with patch.object(client, "_send_request",
                          return_value=_resource_read_response("notes://ideas", "Build a banana OS", req_id=4)):
            result = client.read_resource("notes://ideas")
        assert result == "Build a banana OS"

    def test_read_resource_returns_error_string_on_dead_process(self):
        """When the reader thread has exited, read_resource returns an error string."""
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient("notes", _server_config(name="notes"))
            with patch.object(client, "_send_request", side_effect=[
                _initialize_response(req_id=1, capabilities=_FULL_CAPS),
                _tools_list_response([], req_id=2),
                _prompts_list_response([], req_id=3),
            ]), patch.object(client, "_send_notification"):
                client.connect()

        client._read_thread.join(timeout=1)
        assert client._thread_exited

        result = client.read_resource("notes://ideas")
        assert result.startswith("Error")


# ── TestMCPClientPrompts ──────────────────────────────────────────────────────

class TestMCPClientPrompts:
    def test_connect_discovers_prompts_when_capability_present(self):
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient("notes", _server_config(name="notes"))
            with patch.object(client, "_send_request", side_effect=[
                _initialize_response(req_id=1, capabilities=_FULL_CAPS),
                _tools_list_response([], req_id=2),
                _prompts_list_response(_SAMPLE_PROMPTS, req_id=3),
            ]), patch.object(client, "_send_notification"):
                client.connect()

        assert len(client.prompts) == 2
        assert client.prompts[0].name == "summarize_notes"
        assert client.prompts[0].namespaced_name == "notes__summarize_notes"
        find = client.prompts[1]
        assert find.name == "find_related"
        assert len(find.arguments) == 1
        assert find.arguments[0].required is True

    def test_get_prompt_returns_messages(self):
        messages = [{"role": "user", "content": {"type": "text", "text": "Summarize my notes please."}}]
        with patch("subprocess.Popen", return_value=_mock_process_eof()):
            client = MCPClient("notes", _server_config(name="notes"))
            with patch.object(client, "_send_request", side_effect=[
                _initialize_response(req_id=1, capabilities=_FULL_CAPS),
                _tools_list_response([], req_id=2),
                _prompts_list_response(_SAMPLE_PROMPTS, req_id=3),
            ]), patch.object(client, "_send_notification"):
                client.connect()
            with patch.object(client, "_send_request",
                               return_value=_prompt_get_response(messages, req_id=4)):
                result = client.get_prompt("summarize_notes")

        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert "Summarize" in result[0]["content"]["text"]


# ── TestMCPManagerResources ───────────────────────────────────────────────────

class TestMCPManagerResources:
    def _manager_with_resources(self) -> MCPManager:
        from minion.mcp.client import MCPResource, MCPPrompt
        manager = MCPManager()
        client = MagicMock(spec=MCPClient)
        client.name = "notes"
        client.tools = []
        client._has_resources_capability = True
        client.prompts = [
            MCPPrompt(name="summarize_notes", server_name="notes"),
        ]
        client.get_tool_definitions.return_value = []
        client.list_resources.return_value = [
            MCPResource(uri="notes://ideas", name="ideas", server_name="notes"),
        ]
        client.read_resource.return_value = "Build a banana OS"
        client.get_prompt.return_value = [{"role": "user", "content": {"type": "text", "text": "Summarize…"}}]
        manager._clients["notes"] = client
        return manager

    def test_has_resources_true_when_client_has_capability(self):
        assert self._manager_with_resources().has_resources() is True

    def test_has_prompts_true_when_client_has_prompts(self):
        assert self._manager_with_resources().has_prompts() is True

    def test_read_resource_routes_to_owning_client(self):
        manager = self._manager_with_resources()
        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit = MagicMock()
            result = manager.read_resource("notes://ideas")
        manager._clients["notes"].read_resource.assert_called_once_with("notes://ideas")
        assert result == "Build a banana OS"

    def test_read_resource_unknown_uri_returns_error_string(self):
        manager = self._manager_with_resources()
        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit = MagicMock()
            result = manager.read_resource("notes://nonexistent")
        assert "Error" in result
        assert "nonexistent" in result

    def test_get_prompt_routes_to_correct_client(self):
        manager = self._manager_with_resources()
        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit = MagicMock()
            result = manager.get_prompt("notes__summarize_notes")
        manager._clients["notes"].get_prompt.assert_called_once_with("summarize_notes", None)
        assert result[0]["role"] == "user"

    def test_get_prompt_unknown_server_returns_error_messages(self):
        manager = self._manager_with_resources()
        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value.emit = MagicMock()
            result = manager.get_prompt("badserver__some_prompt")
        assert len(result) == 1
        assert "Error" in result[0]["content"]["text"]
        assert "badserver" in result[0]["content"]["text"]

    def test_server_summary_includes_resources_and_prompts(self):
        manager = self._manager_with_resources()
        summary = manager.server_summary()
        assert len(summary) == 1
        s = summary[0]
        assert s["name"] == "notes"
        assert len(s["resources"]) == 1
        assert s["resources"][0]["uri"] == "notes://ideas"
        assert len(s["prompts"]) == 1
        assert s["prompts"][0]["name"] == "summarize_notes"


# ── TestMCPNotifications ──────────────────────────────────────────────────────

class TestMCPNotifications:
    """Tests for notification dispatch and manager logging integration.

    Notification delivery is tested by calling _handle_notification() directly —
    no subprocess or thread needed to verify the callback contract.
    """

    def test_notification_delivered_to_callback(self):
        """notifications/message is routed to notification_callback."""
        client = MCPClient("notes", _server_config(name="notes"))
        received: list[tuple] = []
        client.notification_callback = lambda s, p: received.append((s, p))

        client._handle_notification({
            "method": "notifications/message",
            "params": {"level": "info", "logger": "test-server", "data": "Tool executed"},
        })

        assert len(received) == 1
        server_name, params = received[0]
        assert server_name == "notes"
        assert params["level"] == "info"
        assert params["data"] == "Tool executed"

    def test_notification_without_callback_does_not_crash(self):
        """Notifications with no callback set are silently ignored."""
        client = MCPClient("notes", _server_config())
        # notification_callback is None by default — should not raise
        client._handle_notification({
            "method": "notifications/message",
            "params": {"level": "warning", "data": "disk full"},
        })

    def test_unknown_notification_method_ignored(self):
        """Notification methods other than notifications/message are silently ignored."""
        client = MCPClient("notes", _server_config())
        called = []
        client.notification_callback = lambda s, p: called.append(p)

        client._handle_notification({"method": "notifications/resources/changed", "params": {}})

        assert called == []

    def test_multiple_notifications_all_delivered_in_order(self):
        """Multiple _handle_notification calls deliver in call order."""
        client = MCPClient("notes", _server_config())
        received: list[str] = []
        client.notification_callback = lambda s, p: received.append(p["data"])

        for msg in ["first", "second", "third"]:
            client._handle_notification({
                "method": "notifications/message",
                "params": {"level": "info", "data": msg},
            })

        assert received == ["first", "second", "third"]

    def test_manager_on_notification_emits_mcp_log_event(self):
        """MCPManager._on_notification() emits an mcp_log trace event."""
        manager = MCPManager()
        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            emit = MagicMock()
            mock_tracer.return_value.emit = emit
            manager._on_notification("notes", {
                "level": "warning",
                "logger": "minion-notes",
                "data": "Note not found",
            })
        emit.assert_called_once_with(
            "mcp_log",
            server_name="notes",
            level="warning",
            logger="minion-notes",
            data="Note not found",
        )

    def test_manager_on_notification_defaults_missing_fields(self):
        """Missing level/logger/data in params use safe defaults."""
        manager = MCPManager()
        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            emit = MagicMock()
            mock_tracer.return_value.emit = emit
            manager._on_notification("srv", {})
        emit.assert_called_once_with(
            "mcp_log",
            server_name="srv",
            level="info",
            logger="",
            data="",
        )

    def test_connect_all_wires_notification_callback(self):
        """connect_all() sets notification_callback on each MCPClient before connect."""
        manager = MCPManager()
        configs = {
            "notes": MCPServerConfig("notes", command=["python", "notes.py"], env={}, confirm_all=False),
        }
        with patch("minion.mcp.manager.MCPClient") as MockClient:
            instance = MockClient.return_value
            instance.connect.return_value = None
            instance.tools = []
            manager.connect_all(configs)

        assert instance.notification_callback == manager._on_notification
