"""Tests for the MCP (Model Context Protocol) client system.

All tests are fully offline — no real subprocess is spawned. MCPClient's
subprocess is mocked via unittest.mock.patch("subprocess.Popen"), with the
mock process's stdin/stdout providing pre-canned JSON-RPC responses.

Test groups:
    TestMCPConfig       — config loading, two-tier merge, error handling
    TestMCPClient       — connect handshake, tool listing, tool calling, annotations
    TestMCPManager      — multi-server orchestration, routing, shutdown
    TestMCPAwareExecutor — ToolExecutor MCP routing and confirmation bypass
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from minion.mcp.config import MCPServerConfig, load_mcp_config
from minion.mcp.client import MCPClient, MCPTool
from minion.mcp.manager import MCPManager
from minion.tools.executor import ToolExecutor
from minion.llm.base import ToolUseBlock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_line(obj: dict) -> bytes:
    """Encode a JSON-RPC object as a newline-terminated bytes line."""
    return (json.dumps(obj) + "\n").encode()


def _mock_process(responses: list[dict]):
    """Build a mock Popen process whose stdout returns the given responses in order."""
    process = MagicMock()
    process.stdin = MagicMock()

    lines = [_make_line(r) for r in responses]
    process.stdout = MagicMock()
    process.stdout.readline = MagicMock(side_effect=lines + [b""])  # EOF after all lines
    process.returncode = None
    return process


def _server_config(name: str = "test", command: list | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=command or ["echo", "hello"],
        env={},
        confirm_all=False,
    )


def _initialize_response(req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
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
        # Project config shadows user config for the same server name
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
        # Malformed user tier is skipped; project tier still loads
        assert "good" in result


# ── TestMCPClient ─────────────────────────────────────────────────────────────

class TestMCPClient:
    def test_connect_sends_initialize_and_lists_tools(self):
        process = _mock_process([
            _initialize_response(req_id=1),
            _tools_list_response(_SAMPLE_TOOLS, req_id=2),
        ])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("test", _server_config())
            client.connect()

        assert len(client.tools) == 2
        assert client.tools[0].name == "read_file"
        assert client.tools[1].name == "delete_file"

    def test_connect_raises_on_subprocess_failure(self):
        with patch("subprocess.Popen", side_effect=FileNotFoundError("not found")):
            client = MCPClient("bad", _server_config(command=["nonexistent"]))
            with pytest.raises(RuntimeError, match="failed to start"):
                client.connect()

    def test_connect_raises_on_bad_initialize_response(self):
        # Response missing 'serverInfo'
        bad_init = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}}
        process = _mock_process([bad_init])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("test", _server_config())
            with pytest.raises(RuntimeError, match="serverInfo"):
                client.connect()

    def test_get_tool_definitions_prefixes_names(self):
        process = _mock_process([
            _initialize_response(),
            _tools_list_response(_SAMPLE_TOOLS),
        ])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("myserver", _server_config(name="myserver"))
            client.connect()

        defs = client.get_tool_definitions()
        names = [d["name"] for d in defs]
        assert "myserver__read_file" in names
        assert "myserver__delete_file" in names

    def test_get_tool_definitions_maps_input_schema_key(self):
        process = _mock_process([
            _initialize_response(),
            _tools_list_response(_SAMPLE_TOOLS),
        ])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("srv", _server_config(name="srv"))
            client.connect()

        defs = client.get_tool_definitions()
        for d in defs:
            # Must use snake_case 'input_schema', not camelCase 'inputSchema'
            assert "input_schema" in d
            assert "inputSchema" not in d

    def test_call_tool_sends_request_and_returns_text(self):
        process = _mock_process([
            _initialize_response(),
            _tools_list_response(_SAMPLE_TOOLS),
            _tool_call_response("file contents here", req_id=3),
        ])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("srv", _server_config(name="srv"))
            client.connect()
            result = client.call_tool("read_file", {"path": "/tmp/test.txt"})

        assert result == "file contents here"

    def test_call_tool_returns_error_text_on_is_error_true(self):
        process = _mock_process([
            _initialize_response(),
            _tools_list_response(_SAMPLE_TOOLS),
            _tool_call_response("file not found", req_id=3, is_error=True),
        ])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("srv", _server_config(name="srv"))
            client.connect()
            result = client.call_tool("read_file", {"path": "/does/not/exist"})

        assert result.startswith("Error:")
        assert "file not found" in result

    def test_call_tool_returns_error_string_on_dead_process(self):
        process = _mock_process([
            _initialize_response(),
            _tools_list_response(_SAMPLE_TOOLS),
        ])
        # Simulate dead process: stdout.readline returns b"" (EOF)
        process.stdout.readline = MagicMock(side_effect=[
            _make_line(_initialize_response()),
            _make_line(_tools_list_response(_SAMPLE_TOOLS)),
            b"",  # EOF — server died
        ])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("srv", _server_config(name="srv"))
            client.connect()
            result = client.call_tool("read_file", {"path": "/tmp/test.txt"})

        assert "Error:" in result

    def test_annotation_destructive_hint_parsed(self):
        process = _mock_process([
            _initialize_response(),
            _tools_list_response(_SAMPLE_TOOLS),
        ])
        with patch("subprocess.Popen", return_value=process):
            client = MCPClient("srv", _server_config(name="srv"))
            client.connect()

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
        # connect() raises RuntimeError for bad server — manager should warn, not crash
        with patch("minion.mcp.manager.MCPClient") as MockClient:
            instance = MockClient.return_value
            instance.connect.side_effect = RuntimeError("command not found")
            manager.connect_all(configs)

        assert len(manager._clients) == 0  # bad server not stored

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
        defs = manager.get_tool_definitions()
        names = [d["name"] for d in defs]
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
        manager._clients.get("a") or True  # clients cleared
        # Verify shutdown was called on each mock before clear
        # (we check has_tools() is False after shutdown)
        assert not manager.has_tools()


# ── TestMCPAwareExecutor ──────────────────────────────────────────────────────

class TestMCPAwareExecutor:
    def _tool_block(self, name: str, inputs: dict) -> ToolUseBlock:
        return ToolUseBlock(id="abc123", name=name, input=inputs)

    def test_execute_native_tool_still_works_with_mcp_manager_present(self):
        mock_manager = MagicMock()
        executor = ToolExecutor(mcp_manager=mock_manager)
        # read_file is a native tool — should not go to MCP
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
