"""Tests for MCPManager with the official MCP SDK.

Uses unittest.mock to patch the SDK's ClientSession and transport context managers,
avoiding any real subprocess or HTTP connections.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minion.mcp.config import MCPServerConfig
from minion.mcp.manager import MCPManager, load_mcp_manager_async


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_tool(name: str, description: str = "", destructive: bool = False):
    t = SimpleNamespace(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
        annotations=SimpleNamespace(destructiveHint=destructive) if destructive else None,
    )
    return t


def _make_prompt(name: str, description: str = "", arguments=None):
    p = SimpleNamespace(
        name=name,
        description=description,
        arguments=arguments or [],
    )
    return p


def _make_session(tools=None, prompts=None, has_resources=True):
    """Build a mock ClientSession with the expected SDK interface."""
    session = AsyncMock()
    session.initialize = AsyncMock()

    # list_tools
    tools_result = SimpleNamespace(tools=tools or [])
    session.list_tools = AsyncMock(return_value=tools_result)

    # list_resources
    if has_resources:
        resources_result = SimpleNamespace(resources=[
            SimpleNamespace(uri="file:///readme", name="readme", description=""),
        ])
        session.list_resources = AsyncMock(return_value=resources_result)
    else:
        session.list_resources = AsyncMock(side_effect=Exception("no resources"))

    # list_prompts
    prompts_result = SimpleNamespace(prompts=prompts or [])
    session.list_prompts = AsyncMock(return_value=prompts_result)

    # call_tool → returns CallToolResult-like object with content list
    text_item = SimpleNamespace(text="tool output")
    call_result = SimpleNamespace(content=[text_item])
    session.call_tool = AsyncMock(return_value=call_result)

    # read_resource
    resource_item = SimpleNamespace(text="resource content")
    resource_result = SimpleNamespace(contents=[resource_item])
    session.read_resource = AsyncMock(return_value=resource_result)

    # get_prompt
    msg = SimpleNamespace(
        role=SimpleNamespace(value="user"),
        content=SimpleNamespace(text="prompt text"),
    )
    prompt_result = SimpleNamespace(messages=[msg])
    session.get_prompt = AsyncMock(return_value=prompt_result)

    return session


def _patch_transport(session_mock):
    """Return context manager patches so _run_server() uses our mock session."""
    # Transport CM yields (reader, writer); they're ignored — session is mocked separately
    transport_cm = AsyncMock()
    transport_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    transport_cm.__aexit__ = AsyncMock(return_value=False)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session_mock)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return transport_cm, session_cm


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def stdio_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="test_server",
        command=["echo", "hello"],
        env={},
        confirm_all=False,
    )


@pytest.fixture
def http_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="http_server",
        url="http://localhost:9999",
        confirm_all=False,
    )


# ── Unit Tests: MCPManager ────────────────────────────────────────────────────

class TestMCPManagerConnect:
    @pytest.mark.asyncio
    async def test_connect_populates_tools(self, stdio_config):
        tools = [_make_tool("search", "Search files"), _make_tool("write", "Write file")]
        session = _make_session(tools=tools)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            manager.shutdown()

        assert manager.has_tools() is False  # shutdown cleared state
        # Re-connect to inspect state before shutdown
        manager2 = MCPManager()
        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            await manager2.connect_all({"test_server": stdio_config})
            assert manager2.has_tools()
            defs = manager2.get_tool_definitions()
            assert len(defs) == 2
            names = {d["name"] for d in defs}
            assert "test_server__search" in names
            assert "test_server__write" in names
            manager2.shutdown()

    @pytest.mark.asyncio
    async def test_connect_http_uses_sse_client(self, http_config):
        session = _make_session()
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.sse_client", return_value=transport_cm) as mock_sse, \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"http_server": http_config})
            manager.shutdown()

        mock_sse.assert_called_once_with(url="http://localhost:9999")

    @pytest.mark.asyncio
    async def test_connect_failure_removes_server(self, stdio_config):
        """A server that fails to connect is removed from the manager's state."""
        transport_cm = AsyncMock()
        transport_cm.__aenter__ = AsyncMock(side_effect=Exception("connection refused"))
        transport_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})

        assert not manager.has_tools()

    @pytest.mark.asyncio
    async def test_multiple_servers_connected_concurrently(self, stdio_config, http_config):
        tools_a = [_make_tool("tool_a")]
        tools_b = [_make_tool("tool_b")]
        session_a = _make_session(tools=tools_a)
        session_b = _make_session(tools=tools_b)
        tcm_a, scm_a = _patch_transport(session_a)
        tcm_b, scm_b = _patch_transport(session_b)

        with patch("minion.mcp.manager.stdio_client", return_value=tcm_a), \
             patch("minion.mcp.manager.sse_client", return_value=tcm_b), \
             patch("minion.mcp.manager.ClientSession", side_effect=[scm_a, scm_b]):
            manager = MCPManager()
            await manager.connect_all({
                "test_server": stdio_config,
                "http_server": http_config,
            })
            defs = manager.get_tool_definitions()
            names = {d["name"] for d in defs}
            assert "test_server__tool_a" in names
            assert "http_server__tool_b" in names
            manager.shutdown()


class TestMCPManagerMetadata:
    @pytest.mark.asyncio
    async def test_is_dangerous_by_annotation(self, stdio_config):
        tools = [_make_tool("rm", destructive=True), _make_tool("cat", destructive=False)]
        session = _make_session(tools=tools)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            assert manager.is_dangerous("test_server__rm") is True
            assert manager.is_dangerous("test_server__cat") is False
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_is_dangerous_confirm_all(self, stdio_config):
        stdio_config.confirm_all = True
        tools = [_make_tool("cat")]
        session = _make_session(tools=tools)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            assert manager.is_dangerous("test_server__cat") is True
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_server_summary_has_tools_and_prompts(self, stdio_config):
        prompt_arg = SimpleNamespace(name="query", description="Search query", required=True)
        prompts = [_make_prompt("search_prompt", arguments=[prompt_arg])]
        tools = [_make_tool("grep")]
        session = _make_session(tools=tools, prompts=prompts)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            summary = manager.server_summary()
            assert len(summary) == 1
            assert summary[0]["name"] == "test_server"
            assert "grep" in summary[0]["tools"]
            assert summary[0]["prompts"][0]["name"] == "search_prompt"
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_has_resources_true_when_server_supports_it(self, stdio_config):
        session = _make_session(has_resources=True)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            assert manager.has_resources() is True
            manager.shutdown()


class TestMCPManagerCallTool:
    @pytest.mark.asyncio
    async def test_call_tool_routes_to_server(self, stdio_config):
        tools = [_make_tool("search")]
        session = _make_session(tools=tools)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            result = await manager.call_tool("test_server__search", {"query": "hello"})
            assert result == "tool output"
            session.call_tool.assert_called_once_with("search", {"query": "hello"})
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_call_tool_unknown_server_returns_error(self, stdio_config):
        session = _make_session()
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            result = await manager.call_tool("missing__tool", {})
            assert "not connected" in result
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_call_tool_malformed_name_returns_error(self, stdio_config):
        session = _make_session()
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            result = await manager.call_tool("no_double_underscore", {})
            assert "malformed" in result
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_call_tool_exception_returns_error_string(self, stdio_config):
        tools = [_make_tool("boom")]
        session = _make_session(tools=tools)
        session.call_tool = AsyncMock(side_effect=Exception("server crashed"))
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            result = await manager.call_tool("test_server__boom", {})
            assert "server crashed" in result
            manager.shutdown()


class TestMCPManagerPrompt:
    @pytest.mark.asyncio
    async def test_get_prompt_returns_messages(self, stdio_config):
        prompts = [_make_prompt("my_prompt")]
        session = _make_session(prompts=prompts)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            messages = await manager.get_prompt("test_server__my_prompt", {"key": "val"})
            assert isinstance(messages, list)
            assert messages[0]["role"] == "user"
            assert messages[0]["content"]["text"] == "prompt text"
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_get_prompt_malformed_name_returns_error(self, stdio_config):
        session = _make_session()
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            messages = await manager.get_prompt("no_separator", {})
            assert "Error" in messages[0]["content"]["text"]
            manager.shutdown()


class TestMCPManagerResource:
    @pytest.mark.asyncio
    async def test_read_resource_returns_content(self, stdio_config):
        session = _make_session(has_resources=True)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            result = await manager.read_resource("file:///readme")
            assert result == "resource content"
            manager.shutdown()

    @pytest.mark.asyncio
    async def test_read_resource_no_server_with_resources(self, stdio_config):
        session = _make_session(has_resources=False)
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            result = await manager.read_resource("file:///readme")
            assert "No MCP server owns resource" in result
            manager.shutdown()


class TestMCPManagerShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_clears_state(self, stdio_config):
        session = _make_session(tools=[_make_tool("t")])
        transport_cm, session_cm = _patch_transport(session)

        with patch("minion.mcp.manager.stdio_client", return_value=transport_cm), \
             patch("minion.mcp.manager.ClientSession", return_value=session_cm):
            manager = MCPManager()
            await manager.connect_all({"test_server": stdio_config})
            assert manager.has_tools()
            manager.shutdown()
            assert not manager.has_tools()

    @pytest.mark.asyncio
    async def test_empty_manager_has_no_tools(self):
        manager = MCPManager()
        assert not manager.has_tools()
        assert not manager.has_resources()
        assert not manager.has_prompts()
        manager.shutdown()


class TestLoadMcpManagerAsync:
    @pytest.mark.asyncio
    async def test_returns_empty_manager_when_no_config(self, tmp_path):
        manager = await load_mcp_manager_async(cwd=tmp_path)
        assert not manager.has_tools()
        manager.shutdown()
