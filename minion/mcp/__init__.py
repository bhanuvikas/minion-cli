"""minion.mcp — MCP (Model Context Protocol) client package.

Public API:
    load_mcp_manager(cwd)  — load config + connect servers, return MCPManager
    MCPManager             — manages multiple MCP server connections
    MCPServerConfig        — config dataclass for a single server
"""

from .config import MCPServerConfig
from .manager import MCPManager, load_mcp_manager

__all__ = ["MCPServerConfig", "MCPManager", "load_mcp_manager"]
