"""minion.mcp — MCP (Model Context Protocol) client package.

Public API:
    load_mcp_manager(cwd)  — load config + connect servers, return MCPManager
    MCPManager             — manages multiple MCP server connections
    MCPServerConfig        — config dataclass for a single server
    MCPClientBase          — abstract base for transport implementations
    MCPHTTPClient          — Streamable HTTP transport client
"""

from .base import MCPClientBase
from .config import MCPServerConfig
from .http_client import MCPHTTPClient
from .manager import MCPManager, load_mcp_manager

__all__ = ["MCPClientBase", "MCPHTTPClient", "MCPManager", "MCPServerConfig", "load_mcp_manager"]
