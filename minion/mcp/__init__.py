"""minion.mcp — MCP (Model Context Protocol) client package.

Public API:
    load_mcp_manager(cwd)        — sync: load config + connect servers, return MCPManager
    load_mcp_manager_async(cwd)  — async: same but preferred from async contexts
    MCPManager                   — manages multiple MCP server sessions via SDK
    MCPServerConfig              — config dataclass for a single server
"""

from .config import MCPServerConfig
from .manager import MCPManager, load_mcp_manager, load_mcp_manager_async

__all__ = ["MCPManager", "MCPServerConfig", "load_mcp_manager", "load_mcp_manager_async"]
