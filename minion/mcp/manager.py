"""MCPManager — manages multiple MCP server sessions using the official MCP SDK.

Each configured server gets a persistent async session maintained by a background
asyncio.Task. The task opens the transport, initialises the session, and waits
forever (until cancelled). Tool/resource/prompt calls reuse the live session —
no reconnect overhead per call.

Public API (async I/O):
    connect_all(configs) — async, call once at REPL startup
    call_tool(name, args) — async
    get_prompt(name, args) — async
    read_resource(uri) — async

Public API (sync metadata — no I/O):
    has_tools(), get_tool_definitions(), is_dangerous(name), shutdown()

Backward-compat sync wrapper (for use from threads only, via asyncio.run()):
    call_tool_sync(name, args)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from ..theme import console
from ..tracing import get_tracer
from .config import MCPServerConfig, load_mcp_config


@dataclass
class _MCPTool:
    name: str
    description: str
    input_schema: dict
    destructive: bool = False


@dataclass
class _MCPPromptArg:
    name: str
    description: str
    required: bool


@dataclass
class _MCPPrompt:
    name: str
    description: str
    arguments: list[_MCPPromptArg] = field(default_factory=list)


@dataclass
class _ServerState:
    """Runtime state for one connected MCP server."""
    name: str
    config: MCPServerConfig
    session: Optional[ClientSession] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    error: Optional[Exception] = None
    tools: list[_MCPTool] = field(default_factory=list)
    prompts: list[_MCPPrompt] = field(default_factory=list)
    has_resources: bool = False
    task: Optional[asyncio.Task] = None


async def _run_server(state: _ServerState) -> None:
    """Background task that owns the server's transport + session lifetime."""
    cfg = state.config
    name = state.name
    try:
        if cfg.transport == "http":
            transport_cm = sse_client(url=cfg.url)
        else:
            env = {**os.environ, **cfg.env}
            params = StdioServerParameters(
                command=cfg.command[0],
                args=cfg.command[1:],
                env=env,
            )
            transport_cm = stdio_client(params)

        async with transport_cm as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # Populate tools
                tools_result = await session.list_tools()
                for t in tools_result.tools:
                    destructive = bool(
                        (t.annotations and getattr(t.annotations, "destructiveHint", False))
                        or cfg.confirm_all
                    )
                    schema = t.inputSchema if isinstance(t.inputSchema, dict) else {}
                    state.tools.append(_MCPTool(
                        name=t.name,
                        description=t.description or "",
                        input_schema=schema,
                        destructive=destructive,
                    ))

                # Detect resources capability
                try:
                    await session.list_resources()
                    state.has_resources = True
                except Exception:
                    state.has_resources = False

                # Populate prompts
                try:
                    prompts_result = await session.list_prompts()
                    for p in prompts_result.prompts:
                        args = []
                        for a in (p.arguments or []):
                            args.append(_MCPPromptArg(
                                name=a.name,
                                description=a.description or "",
                                required=bool(a.required),
                            ))
                        state.prompts.append(_MCPPrompt(
                            name=p.name,
                            description=p.description or "",
                            arguments=args,
                        ))
                except Exception:
                    pass

                state.session = session
                state.ready.set()

                # Block indefinitely — keep the session alive until task is cancelled.
                await asyncio.Event().wait()

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state.error = exc
        state.ready.set()


class MCPManager:
    """Manages multiple MCP server sessions using the official MCP SDK."""

    def __init__(self) -> None:
        self._states: dict[str, _ServerState] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect_all(self, configs: dict[str, MCPServerConfig]) -> None:
        """Connect to all configured servers concurrently. Warns on failures."""
        async def _connect_one(name: str, cfg: MCPServerConfig) -> None:
            state = _ServerState(name=name, config=cfg)
            self._states[name] = state
            t0 = time.monotonic()
            task = asyncio.create_task(_run_server(state))
            state.task = task

            await state.ready.wait()

            if state.error is not None:
                latency_ms = int((time.monotonic() - t0) * 1000)
                console.print(
                    f"[muted]Warning: MCP server '{name}' failed to connect: {state.error}[/]"
                )
                get_tracer().emit(
                    "mcp_server_connect",
                    server_name=name,
                    command=cfg.command,
                    tool_count=0,
                    success=False,
                    error=str(state.error),
                    latency_ms=latency_ms,
                )
                get_tracer().emit(
                    "mcp_error",
                    server_name=name,
                    tool_name="",
                    error=str(state.error),
                    context="connect",
                )
                # Remove failed server so it doesn't appear in summaries
                del self._states[name]
            else:
                latency_ms = int((time.monotonic() - t0) * 1000)
                get_tracer().emit(
                    "mcp_server_connect",
                    server_name=name,
                    command=cfg.command,
                    tool_count=len(state.tools),
                    success=True,
                    latency_ms=latency_ms,
                )

        async with asyncio.TaskGroup() as tg:
            for name, cfg in configs.items():
                tg.create_task(_connect_one(name, cfg))

    def shutdown(self) -> None:
        """Cancel all server background tasks."""
        for state in self._states.values():
            if state.task and not state.task.done():
                state.task.cancel()
        self._states.clear()

    # ── Sync metadata (no I/O) ────────────────────────────────────────────────

    def has_tools(self) -> bool:
        return any(s.tools for s in self._states.values())

    def has_resources(self) -> bool:
        return any(s.has_resources for s in self._states.values())

    def has_prompts(self) -> bool:
        return any(s.prompts for s in self._states.values())

    def get_tool_definitions(self) -> list[dict]:
        """Return merged list of Anthropic-format tool definitions from all servers."""
        defs: list[dict] = []
        for name, state in self._states.items():
            for t in state.tools:
                defs.append({
                    "name": f"{name}__{t.name}",
                    "description": t.description,
                    "input_schema": t.input_schema or {"type": "object", "properties": {}},
                })
        return defs

    def is_dangerous(self, namespaced_name: str) -> bool:
        server_name = namespaced_name.split("__", 1)[0]
        state = self._states.get(server_name)
        if state is None:
            return False
        if state.config.confirm_all:
            return True
        tool_name = namespaced_name.split("__", 1)[1] if "__" in namespaced_name else ""
        return any(t.destructive for t in state.tools if t.name == tool_name)

    def server_summary(self) -> list[dict]:
        """Return cached server info (no live RPC calls — use async version for live resources)."""
        result = []
        for name, state in self._states.items():
            result.append({
                "name": name,
                "tools": [t.name for t in state.tools],
                "resources": [],  # live resources require async — see server_summary_async()
                "prompts": [
                    {
                        "name": p.name,
                        "description": p.description,
                        "arguments": [{"name": a.name, "required": a.required} for a in p.arguments],
                    }
                    for p in state.prompts
                ],
            })
        return result

    def get_prompt_info(self, namespaced_name: str) -> "_MCPPrompt | None":
        parts = namespaced_name.split("__", 1)
        if len(parts) != 2:
            return None
        server_name, prompt_name = parts
        state = self._states.get(server_name)
        if state is None:
            return None
        for p in state.prompts:
            if p.name == prompt_name:
                return p
        return None

    # ── Async I/O ─────────────────────────────────────────────────────────────

    async def call_tool(self, namespaced_name: str, arguments: dict) -> str:
        """Route a namespaced tool call to the correct server session."""
        parts = namespaced_name.split("__", 1)
        if len(parts) != 2:
            return f"Error: malformed MCP tool name '{namespaced_name}' (expected 'server__tool')"

        server_name, tool_name = parts
        state = self._states.get(server_name)
        if state is None or state.session is None:
            return f"Error: MCP server '{server_name}' is not connected"

        get_tracer().emit(
            "mcp_tool_call",
            server_name=server_name,
            tool_name=tool_name,
            namespaced_name=namespaced_name,
            inputs=arguments,
        )

        t0 = time.monotonic()
        try:
            result_obj = await state.session.call_tool(tool_name, arguments or None)
            # SDK returns CallToolResult with content list
            parts_text = []
            for item in result_obj.content:
                if hasattr(item, "text"):
                    parts_text.append(item.text)
                else:
                    parts_text.append(str(item))
            result = "\n".join(parts_text) if parts_text else "(no output)"
            success = True
        except Exception as e:
            result = f"Error: {e}"
            success = False

        latency_ms = int((time.monotonic() - t0) * 1000)
        get_tracer().emit(
            "mcp_tool_result",
            server_name=server_name,
            tool_name=tool_name,
            output=result,
            success=success,
            latency_ms=latency_ms,
        )
        if not success:
            get_tracer().emit(
                "mcp_error",
                server_name=server_name,
                tool_name=tool_name,
                error=result,
                context="call",
            )
        return result

    def call_tool_sync(self, namespaced_name: str, arguments: dict) -> str:
        """Sync wrapper for call_tool(). Only safe to call from a thread (no running event loop)."""
        return asyncio.run(self.call_tool(namespaced_name, arguments))

    async def read_resource(self, uri: str) -> str:
        """Read a resource by URI, routing to the server that supports resources."""
        for name, state in self._states.items():
            if not state.has_resources or state.session is None:
                continue
            try:
                result_obj = await state.session.read_resource(uri)  # type: ignore[arg-type]
                parts_text = []
                for item in result_obj.contents:
                    if hasattr(item, "text"):
                        parts_text.append(item.text)
                    else:
                        parts_text.append(str(item))
                result = "\n".join(parts_text) if parts_text else "(empty resource)"
                get_tracer().emit("mcp_resource_read", server_name=name, uri=uri)
                return result
            except Exception:
                continue
        return f"Error: No MCP server owns resource URI '{uri}'"

    async def get_prompt(self, namespaced_name: str, arguments: dict | None = None) -> list[dict]:
        """Get a prompt template by namespaced name."""
        parts = namespaced_name.split("__", 1)
        if len(parts) != 2:
            return [{"role": "user", "content": {"type": "text", "text": f"Error: malformed prompt name '{namespaced_name}'"}}]

        server_name, prompt_name = parts
        state = self._states.get(server_name)
        if state is None or state.session is None:
            return [{"role": "user", "content": {"type": "text", "text": f"Error: MCP server '{server_name}' is not connected"}}]

        get_tracer().emit(
            "mcp_prompt_get",
            server_name=server_name,
            prompt_name=prompt_name,
            arguments=arguments or {},
        )
        t0 = time.monotonic()
        try:
            result_obj = await state.session.get_prompt(prompt_name, arguments)
            messages = []
            for msg in result_obj.messages:
                role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
                if hasattr(msg.content, "text"):
                    content = {"type": "text", "text": msg.content.text}
                else:
                    content = {"type": "text", "text": str(msg.content)}
                messages.append({"role": role, "content": content})
        except Exception as e:
            messages = [{"role": "user", "content": {"type": "text", "text": f"Error: {e}"}}]

        latency_ms = int((time.monotonic() - t0) * 1000)
        get_tracer().emit(
            "mcp_prompt_result",
            server_name=server_name,
            prompt_name=prompt_name,
            injected_text="",
            message_count=len(messages),
            success=not (messages and "Error:" in str(messages[0])),
            latency_ms=latency_ms,
        )
        return messages

    async def server_summary_async(self) -> list[dict]:
        """Like server_summary() but fetches live resources via RPC."""
        result = []
        for name, state in self._states.items():
            resources = []
            if state.has_resources and state.session is not None:
                try:
                    res_result = await state.session.list_resources()
                    for r in res_result.resources:
                        resources.append({
                            "uri": str(r.uri),
                            "name": r.name or "",
                            "description": r.description or "",
                        })
                except Exception:
                    pass
            result.append({
                "name": name,
                "tools": [t.name for t in state.tools],
                "resources": resources,
                "prompts": [
                    {
                        "name": p.name,
                        "description": p.description,
                        "arguments": [{"name": a.name, "required": a.required} for a in p.arguments],
                    }
                    for p in state.prompts
                ],
            })
        return result

    # ── Notification (no-op for SDK — SDK handles internally) ─────────────────

    def _on_notification(self, server_name: str, params: dict) -> None:
        """Kept for interface compatibility — SDK handles notifications internally."""


async def load_mcp_manager_async(cwd: Path | None = None) -> "MCPManager":
    """Async: load config + connect to all configured MCP servers.

    Returns an MCPManager with live sessions. Call this from async contexts
    (e.g. run_repl_async). Always returns a manager — empty if no servers configured.
    """
    configs = load_mcp_config(cwd)
    manager = MCPManager()
    if configs:
        await manager.connect_all(configs)
    return manager


def load_mcp_manager(cwd: Path | None = None) -> "MCPManager":
    """Sync: load config + connect to all configured MCP servers.

    Safe to call from sync contexts. Runs the async connect_all in a new event loop.
    Returns an empty MCPManager if no servers configured.
    """
    configs = load_mcp_config(cwd)
    manager = MCPManager()
    if configs:
        asyncio.run(manager.connect_all(configs))
    return manager
