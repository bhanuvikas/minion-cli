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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import stdio_client
import mcp.types as mcp_types

from ..llm.base import ToolDefinition
from ..theme import console
from ..tracing import get_tracer
from .config import MCPServerConfig, load_mcp_config


@asynccontextmanager
async def _http_transport(url: str):
    """Adapt streamable_http_client's 3-tuple yield to (read, write) for ClientSession."""
    async with streamable_http_client(url=url) as (r, w, _):
        yield r, w


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
    llm_client: Optional[object] = None  # set by MCPManager.set_llm_client() for sampling


# ─── Notification / request callbacks ────────────────────────────────────────

async def _console_print_safe(text: str) -> None:
    """Print an MCP notification, coordinating with the active TUI or console prompt."""
    try:
        from ..tui import get_tui_app as _get_tui_app
        _tui = _get_tui_app()
        if _tui is not None:
            _tui.conversation.append_system(text)
            return
    except Exception:
        pass
    console.print(text)


async def _logging_callback(state: _ServerState,
                             params: mcp_types.LoggingMessageNotificationParams) -> None:
    """Handle notifications/message — server log lines, routed by level."""
    level = str(params.level)
    data = params.data
    logger = params.logger or state.name

    if isinstance(data, str):
        msg = data
    elif isinstance(data, dict):
        # Format structured payloads: prefer a "message" key, then event+path
        # for file-change events, then compact JSON for anything else.
        if "message" in data:
            msg = str(data["message"])
        elif "event" in data and "path" in data:
            path_str = str(data["path"])
            # Suppress internal .minion/ directory events (memory writes, config, etc.)
            if any(p == ".minion" for p in Path(path_str).parts):
                return
            msg = f"{data['event']}  {data['path']}"
        else:
            import json as _json
            msg = _json.dumps(data, separators=(",", ":"))
    else:
        msg = str(data) if data is not None else ""

    get_tracer().emit("mcp_notification", server_name=state.name,
                      method="notifications/message", level=level, message=msg)

    if level == "debug":
        pass  # Nefario only — too noisy for the console
    elif level in ("info", "notice"):
        await _console_print_safe(f"[muted][{logger}] {msg}[/]\n")
    elif level == "warning":
        await _console_print_safe(f"[yellow][{logger}] ⚠ {msg}[/]\n")
    else:  # error, critical, alert, emergency
        await _console_print_safe(f"[bold red][{logger}] ✗ {msg}[/]\n")


async def _message_handler(state: _ServerState, message: object) -> None:
    """Catch-all for server notifications not handled by specific callbacks.

    Handles: tools/list_changed, resources/list_changed, prompts/list_changed,
             resources/updated, progress.
    """
    if not isinstance(message, mcp_types.ServerNotification):
        return  # Requests are handled by their dedicated callbacks; errors ignored here.

    match message.root:
        case mcp_types.ToolListChangedNotification():
            if state.session is None:
                return
            try:
                tools_result = await state.session.list_tools()
                state.tools.clear()
                for t in tools_result.tools:
                    destructive = bool(
                        (t.annotations and getattr(t.annotations, "destructiveHint", False))
                        or state.config.confirm_all
                    )
                    schema = t.inputSchema if isinstance(t.inputSchema, dict) else {}
                    state.tools.append(_MCPTool(
                        name=t.name, description=t.description or "",
                        input_schema=schema, destructive=destructive,
                    ))
                get_tracer().emit("mcp_notification", server_name=state.name,
                                  method="notifications/tools/list_changed",
                                  tool_count=len(state.tools))
                await _console_print_safe(
                    f"[muted][{state.name}] Tool list updated "
                    f"({len(state.tools)} available)[/]"
                )
            except Exception as e:
                await _console_print_safe(f"[muted][{state.name}] Failed to refresh tools: {e}[/]")

        case mcp_types.ResourceListChangedNotification():
            if state.session is None:
                return
            try:
                await state.session.list_resources()
                state.has_resources = True
                get_tracer().emit("mcp_notification", server_name=state.name,
                                  method="notifications/resources/list_changed")
            except Exception:
                pass

        case mcp_types.PromptListChangedNotification():
            if state.session is None:
                return
            try:
                prompts_result = await state.session.list_prompts()
                state.prompts.clear()
                for p in prompts_result.prompts:
                    args = [
                        _MCPPromptArg(name=a.name, description=a.description or "",
                                      required=bool(a.required))
                        for a in (p.arguments or [])
                    ]
                    state.prompts.append(_MCPPrompt(
                        name=p.name, description=p.description or "", arguments=args,
                    ))
                get_tracer().emit("mcp_notification", server_name=state.name,
                                  method="notifications/prompts/list_changed",
                                  prompt_count=len(state.prompts))
            except Exception:
                pass

        case mcp_types.ResourceUpdatedNotification(params=params):
            uri = str(params.uri)
            get_tracer().emit("mcp_notification", server_name=state.name,
                              method="notifications/resources/updated", uri=uri)
            await _console_print_safe(f"[muted][{state.name}] Resource updated: {uri}[/]")

        case mcp_types.ProgressNotification(params=params):
            msg = params.message or ""
            progress = int(params.progress) if params.progress is not None else 0
            total = int(params.total) if params.total is not None else None
            bar = f"{progress}/{total}" if total else str(progress)
            await _console_print_safe(f"[muted][{state.name}] ▶ {bar} {msg}[/]".rstrip())

        case _:
            pass  # cancelled handled by SDK; other unknown notifications silently ignored


async def _sampling_callback(
    state: _ServerState,
    context: object,
    params: mcp_types.CreateMessageRequestParams,
) -> mcp_types.CreateMessageResult | mcp_types.ErrorData:
    """Handle sampling/createMessage — server asks our LLM to generate a response."""
    if state.llm_client is None:
        return mcp_types.ErrorData(code=-32603, message="No LLM client configured for sampling")

    from ..llm.base import Message

    # Convert MCP SamplingMessages to our Message format (text only)
    messages: list[Message] = []
    for m in params.messages:
        content = m.content
        text = content.text if hasattr(content, "text") else str(content)  # type: ignore[union-attr]
        messages.append(Message(role=str(m.role), content=text))

    system = params.systemPrompt or ""

    try:
        response = await state.llm_client.async_complete(messages, system=system)  # type: ignore[union-attr]
        get_tracer().emit("mcp_notification", server_name=state.name,
                          method="sampling/createMessage",
                          model=response.model, tokens=response.output_tokens)
        return mcp_types.CreateMessageResult(
            role="assistant",
            content=mcp_types.TextContent(type="text", text=response.content),
            model=response.model,
            stopReason="endTurn",
        )
    except Exception as e:
        return mcp_types.ErrorData(code=-32603, message=f"Sampling failed: {e}")


async def _roots_callback(context: object) -> mcp_types.ListRootsResult:
    """Handle roots/list — return current working directory as the exposed root."""
    cwd = Path.cwd()
    return mcp_types.ListRootsResult(
        roots=[mcp_types.Root(uri=cwd.as_uri(), name=cwd.name)]  # type: ignore[arg-type]
    )


async def _elicitation_callback(
    state: _ServerState,
    context: object,
    params: object,
) -> mcp_types.ElicitResult | mcp_types.ErrorData:
    """Handle elicitation/create — surface interactive form or URL prompt to user."""
    import questionary
    from ..config import MINION_STYLE

    message = getattr(params, "message", "Input required")
    console.print(f"\n[bold yellow][{state.name}] Input required:[/] {message}")

    # URL elicitation: open browser, wait for user to confirm
    if hasattr(params, "url") and params.url:  # type: ignore[union-attr]
        import webbrowser
        url = str(params.url)  # type: ignore[union-attr]
        console.print(f"[muted]Opening: {url}[/]")
        webbrowser.open(url)
        confirmed = await asyncio.to_thread(
            lambda: questionary.confirm(" Press Enter when done (n to cancel)", style=MINION_STYLE).ask()
        )
        if not confirmed:
            return mcp_types.ElicitResult(action="cancel", content=None)
        return mcp_types.ElicitResult(action="accept", content={})

    # Form elicitation: render each field via questionary
    schema = getattr(params, "requestedSchema", None)
    if schema is None:
        return mcp_types.ElicitResult(action="accept", content={})

    raw = schema.model_dump() if hasattr(schema, "model_dump") else (schema if isinstance(schema, dict) else {})
    properties: dict = raw.get("properties", {})
    required: list = raw.get("required", [])

    answers: dict = {}
    try:
        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                continue
            field_type = field_schema.get("type", "string")
            description = field_schema.get("description", field_name)
            is_required = field_name in required
            label = f" {description}{' *' if is_required else ''}"

            if field_type == "boolean":
                answer = await asyncio.to_thread(
                    lambda lb=label: questionary.confirm(lb, style=MINION_STYLE).ask()
                )
            elif "enum" in field_schema:
                answer = await asyncio.to_thread(
                    lambda lb=label, ch=field_schema["enum"]: questionary.select(lb, choices=ch, pointer="  ❯ ", style=MINION_STYLE).ask()
                )
            elif field_type in ("number", "integer"):
                raw_answer = await asyncio.to_thread(
                    lambda lb=label: questionary.text(lb, style=MINION_STYLE).ask()
                )
                if raw_answer is None:
                    return mcp_types.ElicitResult(action="cancel", content=None)
                try:
                    answer = int(raw_answer) if field_type == "integer" else float(raw_answer)
                except ValueError:
                    answer = raw_answer
            else:
                answer = await asyncio.to_thread(
                    lambda lb=label: questionary.text(lb, style=MINION_STYLE).ask()
                )

            if answer is None:  # user hit Ctrl+C
                return mcp_types.ElicitResult(action="cancel", content=None)
            answers[field_name] = answer

        return mcp_types.ElicitResult(action="accept", content=answers)
    except (KeyboardInterrupt, EOFError):
        return mcp_types.ElicitResult(action="cancel", content=None)


# ─── Server background task ───────────────────────────────────────────────────

async def _run_server(state: _ServerState) -> None:
    """Background task that owns the server's transport + session lifetime."""
    cfg = state.config
    name = state.name
    try:
        if cfg.transport == "http":
            transport_cm = _http_transport(cfg.url)
        else:
            env = {**os.environ, **cfg.env}
            params = StdioServerParameters(
                command=cfg.command[0],
                args=cfg.command[1:],
                env=env,
            )
            transport_cm = stdio_client(params)

        async with transport_cm as (r, w):
            async with ClientSession(
                r, w,
                logging_callback=lambda params: _logging_callback(state, params),
                message_handler=lambda msg: _message_handler(state, msg),  # type: ignore[arg-type]
                sampling_callback=lambda ctx, params: _sampling_callback(state, ctx, params),  # type: ignore[arg-type]
                list_roots_callback=lambda ctx: _roots_callback(ctx),  # type: ignore[arg-type]
                elicitation_callback=lambda ctx, params: _elicitation_callback(state, ctx, params),  # type: ignore[arg-type]
            ) as session:
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
        self._connection_warnings: list[str] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect_all(self, configs: dict[str, MCPServerConfig]) -> None:
        """Connect to all configured servers concurrently. Warns on failures."""
        self._connection_warnings.clear()

        async def _connect_one(name: str, cfg: MCPServerConfig) -> None:
            state = _ServerState(name=name, config=cfg)
            self._states[name] = state
            t0 = time.monotonic()
            task = asyncio.create_task(_run_server(state))
            state.task = task

            await state.ready.wait()

            if state.error is not None:
                latency_ms = int((time.monotonic() - t0) * 1000)
                self._connection_warnings.append(
                    f"  [bold #a8a8a8]Warning[/]  [#888888]MCP server '{name}' failed to connect: {state.error}[/]"
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

    async def reconnect_all(self, cwd: Path | None = None) -> None:
        """Shut down existing sessions, re-read config, and reconnect.

        Allows hot-reload of MCP servers without restarting the REPL.
        """
        self.shutdown()
        configs = load_mcp_config(cwd)
        if configs:
            await self.connect_all(configs)

    def set_llm_client(self, client: object) -> None:
        """Inject the LLM client into all server states for sampling support.

        Call this after connect_all() and after the client is constructed.
        The sampling callback reads state.llm_client lazily at call time so
        servers connected before this call still get the client.
        """
        for state in self._states.values():
            state.llm_client = client

    # ── Sync metadata (no I/O) ────────────────────────────────────────────────

    @property
    def connection_warnings(self) -> list[str]:
        return self._connection_warnings

    def has_tools(self) -> bool:
        return any(s.tools for s in self._states.values())

    def has_resources(self) -> bool:
        return any(s.has_resources for s in self._states.values())

    def has_prompts(self) -> bool:
        return any(s.prompts for s in self._states.values())

    def get_tool_definitions(self) -> list[ToolDefinition]:
        """Return merged list of tool definitions from all MCP servers."""
        defs: list[ToolDefinition] = []
        for name, state in self._states.items():
            for t in state.tools:
                defs.append(ToolDefinition(
                    name=f"{name}__{t.name}",
                    description=t.description,
                    parameters=t.input_schema or {"type": "object", "properties": {}},
                ))
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
                    parts_text.append(item.text)  # type: ignore[union-attr]
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
                        parts_text.append(item.text)  # type: ignore[union-attr]
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
                role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)  # type: ignore[union-attr]
                if hasattr(msg.content, "text"):
                    content = {"type": "text", "text": msg.content.text}  # type: ignore[union-attr]
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
