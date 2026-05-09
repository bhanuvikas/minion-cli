"""Agent loop: user prompt → [LLM ↔ tools]* → streamed response.

Core of the ReAct pattern: on each iteration the model either finishes
(stop_reason="end_turn") or requests a tool call (stop_reason="tool_use").
Tool results are injected back as observations and the loop continues.

Responsibilities:
  run_prompt()              — sync wrapper; delegates to run_prompt_async()
  run_prompt_async()        — orchestrates the full agent loop
  _resolve_mentions()       — expand @file.py references before sending to LLM
  _stream_one_iteration_async() — one LLM call: spin → stream events → structured result
  _build_content_blocks()   — assemble content block list for conversation storage
  _execute_tools_async()    — route tool calls (fast path / parallel agents / parallel tools)
"""

import asyncio
import contextlib
import re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .conversation import Conversation
from .llm.base import ContentTextBlock, ContentToolUseBlock, InputTokenRateLimitError, LLMClient, LLMResponse, StreamComplete, TextChunk, ToolAccumulationStart, ToolUseBlock
from .output import ConsoleRenderer, OutputRenderer
from .output.base import SlotSpec
from .reflection import ReflectionConfig, ReflectionResult, reflect
from .theme import (
    BLUE, YELLOW, console,
    print_critique, print_diff, print_iteration_limit,
    print_reflection_header,
)
from .agents.display import get_agent_display_callback as _get_slot_cb
from .llm.base import ToolDefinition
from .tools.definitions import DELEGATION_TOOLS, SIDE_EFFECTING_TOOLS, TOOL_DEFINITIONS
from .tools.executor import ToolExecutor, TOOL_SPINNER_LABELS, _RenderBuffer
from .tracing import get_tracer

MAX_ITERATIONS = 20
_SPINNER_LABEL = f"[{YELLOW}]🍌  thinking...[/]"


def _serialize_messages(messages) -> list:
    """Convert conversation messages to a JSON-serializable list for tracing."""
    import dataclasses
    result = []
    for msg in messages:
        content = msg.content
        if isinstance(content, str):
            content_out = content
        elif isinstance(content, list):
            content_out = []
            for block in content:
                try:
                    content_out.append(dataclasses.asdict(block))
                except Exception:
                    content_out.append(str(block))
        else:
            content_out = str(content)
        result.append({"role": msg.role, "content": content_out})
    return result

def _snapshot_messages(messages) -> list[dict]:
    """Snapshot conversation messages as plain dicts for the subagent inspector.

    Converts SDK content blocks to simple dicts so the result is safe to store
    across threads without holding references to live SDK objects.
    """
    out: list[dict] = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "type": "text", "text": m.content})
        elif isinstance(m.content, list):
            blocks: list[dict] = []
            for b in m.content:
                if hasattr(b, "text") and not hasattr(b, "name"):      # TextBlock
                    blocks.append({"type": "text", "text": b.text})
                elif hasattr(b, "name") and hasattr(b, "input"):       # ToolUseBlock
                    blocks.append({"type": "tool_use", "name": b.name, "input": dict(b.input)})
                elif hasattr(b, "tool_use_id"):                        # ToolResultBlock
                    rc = b.content if isinstance(b.content, str) else str(b.content)
                    blocks.append({"type": "tool_result", "tool_use_id": b.tool_use_id, "content": rc})
            out.append({"role": m.role, "type": "blocks", "blocks": blocks})
    return out


# Matches @path patterns that contain at least one / or a file extension.
# Examples: @src/auth.py  @README.md  @config/settings.ts
# Does NOT match bare @property, @classmethod (no slash or extension dot).
_MENTION_RE = re.compile(
    r"@("
    r"(?:\w[\w\-]*/)+[\w.\-]+"           # path/with/dirs/file  e.g. @src/auth.py
    r"|[\w][\w\-]*\.[\w]+(?:\.[\w]+)*"   # bare word.ext        e.g. @README.md
    r"|\.[a-zA-Z][\w\-]*(?:\.[\w]+)*"    # bare dotfile         e.g. @.gitignore, @.env.example
    r")"
)


# ─── Result type for a single streaming iteration ─────────────────────────────

@dataclass
class _IterationResult:
    full_text: str
    tool_blocks: list[ToolUseBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Optional[LLMResponse] = None
    cancelled: bool = False




def _complete_cancelled_tools(tool_blocks: list[ToolUseBlock], conversation: Conversation) -> None:
    """Add [Cancelled by user] stubs for tool_use IDs that have no result yet.

    Called after KeyboardInterrupt mid-tool-execution so the conversation
    remains structurally valid (every tool_use block has a matching result).
    """
    completed_ids: set[str] = set()
    for msg in reversed(conversation.messages):
        if msg.role == "assistant":
            break
        if isinstance(msg.content, list):
            for block in msg.content:
                if hasattr(block, "tool_use_id"):
                    completed_ids.add(block.tool_use_id)
    for tb in tool_blocks:
        if tb.id not in completed_ids:
            conversation.add_tool_result(tb.id, "[Cancelled by user]")


# ─── @mention resolution ──────────────────────────────────────────────────────

def _resolve_mentions(prompt: str, cwd: Path) -> str:
    """Expand @file.py references by appending file contents to the prompt.

    Preserves the original mention text inline so the model sees what the
    user typed, then appends the actual file contents at the end.
    Deduplicates repeated mentions of the same file.
    """
    mentions = list(dict.fromkeys(_MENTION_RE.findall(prompt)))  # unique, ordered
    if not mentions:
        return prompt

    appended: list[str] = []
    for mention_path in mentions:
        p = cwd / mention_path
        if not p.exists():
            appended.append(f"[@{mention_path}: file not found]")
        elif not p.is_file():
            appended.append(f"[@{mention_path}: not a file — cannot inject]")
        else:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                appended.append(f"[Contents of {mention_path}]\n{content}")
            except Exception as e:
                appended.append(f"[@{mention_path}: error reading file — {e}]")

    if not appended:
        return prompt
    return prompt + "\n\n" + "\n\n".join(appended)


# ─── Private helpers ──────────────────────────────────────────────────────────

def _build_content_blocks(result: _IterationResult) -> list:
    """Assemble typed ContentBlocks for an assistant tool-use turn."""
    blocks = []
    if result.full_text:
        blocks.append(ContentTextBlock(text=result.full_text))
    for tb in result.tool_blocks:
        blocks.append(ContentToolUseBlock(id=tb.id, name=tb.name, input=tb.input))
    return blocks


def _agent_slots(tool_blocks: list[ToolUseBlock]) -> list[SlotSpec]:
    """Build SlotSpec list for delegation tools (spawn_agent / send_remote_task)."""
    def _label(tb: ToolUseBlock) -> str:
        if tb.name == "spawn_agent":
            return tb.input.get("role") or "researcher"
        return tb.input.get("agent") or "remote"
    return [SlotSpec(key=tb.id, tool_name=tb.name, inputs=tb.input, label=_label(tb)) for tb in tool_blocks]


def _tool_slots(tool_blocks: list[ToolUseBlock]) -> list[SlotSpec]:
    """Build SlotSpec list for generic (non-delegation) parallel tools."""
    return [SlotSpec(key=tb.id, tool_name=tb.name, inputs=tb.input, label=None) for tb in tool_blocks]


# ─── Public entry point ───────────────────────────────────────────────────────

def _run_reflection(
    prompt: str,
    response: str,
    client: LLMClient,
    config: ReflectionConfig,
    verbose: bool,
    conversation: Conversation,
) -> None:
    """Run the self-refine loop and update the conversation if refined.

    Delegates all LLM calls to reflection.reflect(). Passes the full
    conversation history as context so the critic and refiner can see tool
    results (e.g. file contents from read_file calls). Handles display of
    critique and diff when verbose=True. Replaces the last assistant message
    with the refined text when refinement occurred.

    Invariant: called only immediately after conversation.add_assistant(),
    so conversation.messages[-1] is always the draft assistant message.
    """
    from .llm.base import Message

    print_reflection_header(round_num=1, max_rounds=config.depth)
    result = reflect(prompt, response, client, config, context_messages=conversation.messages)

    if verbose:
        for c in result.critiques:
            print_critique(c.score, c.response_type, c.critique)
        if result.was_refined:
            print_diff(response, result.final_response)

    if result.was_refined:
        console.print(f"\n[bold {BLUE}]minion[/] › [muted](refined)[/]")
        console.print(result.final_response)
        # Replace the draft with the refined version so future turns reference
        # the improved response, not the original streaming draft.
        conversation.messages[-1] = Message(
            role="assistant", content=result.final_response
        )
    else:
        score_hint = f" · score: {result.final_score}/10" if verbose else ""
        console.print(f"[muted]  ↳ accepted{score_hint}[/]")


def run_prompt(
    prompt: str,
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    dry_run: bool = False,
    reflect_config: Optional[ReflectionConfig] = None,
    verbose: bool = False,
    memory_tokens: int = 0,
    max_iterations: Optional[int] = None,
    tools: Optional[list[ToolDefinition]] = None,
    render_markdown: bool = False,
    markdown_title: str = "",
    spinner_label: Optional[str] = None,
    mcp_manager=None,
    capture_output: bool = False,
    enable_agents: bool = True,
    agent_depth: int = 0,
    agent_registry=None,
    agent_label: Optional[str] = None,
    a2a_manager=None,
    confirm_callback=None,  # Callable[[str], bool] | None — overrides questionary for dangerous tools
    auto_compact: bool = True,
    approval_mode: str = "off",
    permission_store=None,  # PermissionStore | None
    stream_markdown: bool = False,
    hook_runner=None,  # HookRunner | None
    tui_app=None,  # kept for backward compat — ignored; use renderer= instead
    confirmation_manager=None,  # ConfirmationManager | None — pre-built instance
    renderer: Optional[OutputRenderer] = None,
) -> Optional[str]:
    """Thin sync wrapper — delegates to run_prompt_async() via asyncio.run().

    Safe to call from threads where no event loop is running (including
    asyncio.to_thread()). Do NOT call from a coroutine; use run_prompt_async() directly.
    """
    return asyncio.run(run_prompt_async(
        prompt=prompt, client=client, conversation=conversation,
        system_prompt=system_prompt, dry_run=dry_run, reflect_config=reflect_config,
        verbose=verbose, memory_tokens=memory_tokens, max_iterations=max_iterations,
        tools=tools, render_markdown=render_markdown, markdown_title=markdown_title,
        spinner_label=spinner_label, mcp_manager=mcp_manager, capture_output=capture_output,
        enable_agents=enable_agents, agent_depth=agent_depth, agent_registry=agent_registry,
        agent_label=agent_label, a2a_manager=a2a_manager, confirm_callback=confirm_callback,
        auto_compact=auto_compact, approval_mode=approval_mode, permission_store=permission_store,
        stream_markdown=stream_markdown, hook_runner=hook_runner,
        tui_app=tui_app, confirmation_manager=confirmation_manager, renderer=renderer,
    ))


# ─── Async implementation ─────────────────────────────────────────────────────

async def _stream_one_iteration_async(
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    system_dynamic: str = "",
    tools: Optional[list[ToolDefinition]] = None,
    silent: bool = False,
    flush_narration: bool = True,
    spinner_label: Optional[str] = None,
    agent_label: Optional[str] = None,
    stream_markdown: bool = False,
    renderer: Optional[OutputRenderer] = None,
) -> Optional[_IterationResult]:
    """Async equivalent of _stream_one_iteration(). Uses client.async_stream()."""
    _llm_start = _time.monotonic()
    effective_tools = tools if tools is not None else TOOL_DEFINITIONS
    get_tracer().emit(
        "llm_request",
        message_count=len(conversation.messages),
        messages=_serialize_messages(conversation.messages),
        system=system_prompt,
        tools=effective_tools,
        tool_names=[t.name for t in effective_tools],
        model=getattr(client, "model_id", "unknown"),
        estimated_input_tokens=sum(len(str(m.content)) for m in conversation.messages) // 4,
    )
    effective_spinner = spinner_label or _SPINNER_LABEL
    _renderer = renderer or ConsoleRenderer()

    gen = client.async_stream(conversation.messages, system=system_prompt, system_dynamic=system_dynamic, tools=effective_tools)
    _in_live = _get_slot_cb() is not None
    _first_cm = contextlib.nullcontext() if _in_live else _renderer.spinner(effective_spinner)

    try:
        with _first_cm:
            first_event = await gen.__anext__()
    except StopAsyncIteration:
        first_event = None
    except InputTokenRateLimitError:
        # Don't pop the message and don't print an error — propagate so
        # run_prompt_async can auto-compact the conversation and retry.
        get_tracer().emit(
            "llm_error",
            error="input_token_rate_limit",
            latency_ms=int((_time.monotonic() - _llm_start) * 1000),
        )
        raise
    except Exception as e:
        get_tracer().emit(
            "llm_error",
            error=str(e),
            latency_ms=int((_time.monotonic() - _llm_start) * 1000),
        )
        # Only remove the last message if it's the plain-text user turn that opened
        # this prompt (content is a str). In iteration 2+ the last message is a
        # tool_result (content is a list); popping it would leave the preceding
        # assistant tool_use block without a matching tool_result, causing a 400.
        if conversation.messages and isinstance(conversation.messages[-1].content, str):
            conversation.messages.pop()
        _renderer.on_error(str(e))
        return None

    if first_event is None:
        if conversation.messages and isinstance(conversation.messages[-1].content, str):
            conversation.messages.pop()
        _renderer.on_error("Received an empty response from the model.")
        return None

    text_chunks: list[str] = []
    tool_blocks: list[ToolUseBlock] = []
    stop_reason = "end_turn"
    usage: Optional[LLMResponse] = None
    printed_prefix = False

    def _process(event) -> None:
        nonlocal printed_prefix, stop_reason, usage
        if isinstance(event, TextChunk):
            text_chunks.append(event.text)
            _slot_cb = _get_slot_cb()
            if _slot_cb is not None:
                # Subagent mode: route to slot display (takes priority over renderer)
                _slot_cb("text", text=event.text)
                return
            # Renderer handles silent vs non-silent and TUI vs console internally
            if not printed_prefix:
                _renderer.on_assistant_start(
                    display_name=agent_label or "minion",
                    stream_markdown=stream_markdown,
                    silent=silent,
                )
                printed_prefix = True
            _renderer.on_assistant_chunk(event.text)
        elif isinstance(event, ToolAccumulationStart):
            if not silent and printed_prefix and _get_slot_cb() is None:
                _renderer.on_tool_accumulation_start(event.name)
        elif isinstance(event, ToolUseBlock):
            _renderer.on_tool_use_block_received()
            tool_blocks.append(event)
        elif isinstance(event, StreamComplete):
            _renderer.on_tool_use_block_received()  # stop any pending spinner
            stop_reason = event.stop_reason
            usage = LLMResponse(
                content="",
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                model=event.model,
                cache_read_tokens=event.cache_read_tokens,
                cache_creation_tokens=event.cache_creation_tokens,
            )
            get_tracer().emit(
                "llm_response",
                response="".join(text_chunks),
                stop_reason=event.stop_reason,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                model=event.model,
                latency_ms=int((_time.monotonic() - _llm_start) * 1000),
                tool_calls=[{"name": tb.name, "input": tb.input} for tb in tool_blocks],
            )

    _cancelled = False

    if silent:
        _in_live2 = _get_slot_cb() is not None
        _silent_cm = contextlib.nullcontext() if _in_live2 else _renderer.spinner(effective_spinner)
        with _silent_cm:
            _process(first_event)
            try:
                async for event in gen:
                    _process(event)
            except (KeyboardInterrupt, asyncio.CancelledError):
                _cancelled = True
        if not _cancelled and flush_narration and stop_reason == "tool_use" and text_chunks:
            _renderer.on_narration_flush("".join(text_chunks), display_name=agent_label or "minion")
    else:
        _process(first_event)
        try:
            async for event in gen:
                _process(event)
        except (KeyboardInterrupt, asyncio.CancelledError):
            _cancelled = True

    # Commit/finalise the assistant turn (close MD streamer, print newline, or finalize TUI buffer)
    if printed_prefix:
        _renderer.on_assistant_end()

    if _cancelled:
        return _IterationResult(
            full_text="".join(text_chunks),
            tool_blocks=[],
            stop_reason="end_turn",
            usage=usage,
            cancelled=True,
        )

    return _IterationResult(
        full_text="".join(text_chunks),
        tool_blocks=tool_blocks,
        stop_reason=stop_reason,
        usage=usage,
    )


async def _execute_parallel_agents_async(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
    renderer: Optional[OutputRenderer] = None,
) -> None:
    """Async parallel agent dispatch using asyncio.TaskGroup.

    Each delegation tool gets its own slot in ParallelDisplay. Tool execution
    runs in asyncio.to_thread() so sync agent runners don't block the event loop.
    The callback is set inside the thread (thread-local) to route output correctly.
    """
    from .agents.display import ParallelDisplay, set_agent_display_callback, set_active_live_display

    _parallel = renderer.parallel_display if renderer is not None else None
    display = _parallel if _parallel is not None else ParallelDisplay()

    slots = _agent_slots(tool_blocks)
    await display.pre_register_async(slots)

    # Register agents with the inspector registry and wrap display callbacks so
    # turn_end / complete / error events also update the registry.
    from .tui.agent_registry import get_registry as _get_agent_registry
    _reg = _get_agent_registry()
    _reg.clear()
    for tb, slot in zip(tool_blocks, slots):
        _reg.register(tb.id, label=slot.label, task=tb.input.get("task", ""), role=tb.input.get("role", ""))

    def _make_registry_cb(tb_id: str, display_cb):
        def _cb(event: str, **data) -> None:
            display_cb(event, **data)
            _reg.update(tb_id, event, **data)
        return _cb

    # Create callbacks once so pre-marking and task callbacks share the same slot state.
    _callbacks = {tb.id: _make_registry_cb(tb.id, display.make_callback(tb.id)) for tb in tool_blocks}

    async def _run_spawn_async(tb: ToolUseBlock) -> str:
        task = tb.input.get("task", "")
        role = tb.input.get("role") or "researcher"
        callback = _callbacks[tb.id]
        set_agent_display_callback(callback)
        set_active_live_display(display)

        # Confirmation: ConfirmationManager.confirm_sync() serializes via its own
        # lock and pauses/resumes the display automatically (uses get_active_live_display()).
        if executor._confirmation_manager is not None:
            _cm = executor._confirmation_manager
            def _confirm_cb(name, inputs):
                from .tools.executor import _diff_lines_for_panel as _dlp
                from .tui import is_tui_active as _ita
                _dlns = _dlp(name, inputs) if _ita() else []
                return _cm.confirm_sync(name, inputs, diff_lines=_dlns)
        else:
            _confirm_cb = executor._confirm_callback

        _start = _time.monotonic()
        try:
            get_tracer().emit("tool_call", tool_name="spawn_agent", inputs=tb.input)
            result = await asyncio.to_thread(executor._agent_runner, task, role, _confirm_cb)
            latency_ms = int((_time.monotonic() - _start) * 1000)
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=result, success=True)
            # Skip empty lines and markdown headers to find the first meaningful line.
            first_line = next(
                (l.strip() for l in result.split("\n")
                 if l.strip() and not l.strip().startswith("#")),
                result.split("\n")[0],
            )[:100]
            if result.startswith("Error:"):
                callback("error", error=first_line.removeprefix("Error: "))
            else:
                callback("complete", latency_ms=latency_ms, preview=first_line)
            return result
        except Exception as exc:
            err = f"Error: {exc}"
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=err, success=False)
            callback("error", error=str(exc)[:72])
            return err
        finally:
            set_agent_display_callback(None)

    async def _run_remote_async(tb: ToolUseBlock) -> str:
        callback = _callbacks[tb.id]
        callback("running")
        start = _time.monotonic()
        set_agent_display_callback(callback)
        try:
            result = await executor.execute_async(tb)
            latency_ms = int((_time.monotonic() - start) * 1000)
            preview = result.split("\n")[0][:100] if result else ""
            if result.startswith("Error:"):
                callback("error", error=result.removeprefix("Error: ")[:60])
            else:
                callback("complete", latency_ms=latency_ms, preview=preview)
            return result
        except Exception as exc:
            callback("error", error=str(exc)[:60])
            return f"Error: {exc}"
        finally:
            set_agent_display_callback(None)

    async def _run_one_async(tb: ToolUseBlock) -> str:
        if tb.name == "spawn_agent":
            return await _run_spawn_async(tb)
        return await _run_remote_async(tb)

    tasks_map: dict[str, asyncio.Task] = {}
    for tb in tool_blocks:
        _callbacks[tb.id]("running")

    with display:
        display.render_now()
        async with asyncio.TaskGroup() as tg:
            for tb in tool_blocks:
                tasks_map[tb.id] = tg.create_task(_run_one_async(tb))

    for tb in tool_blocks:
        conversation.add_tool_result(tb.id, tasks_map[tb.id].result())

    # Let the done+preview state render in the slots zone before clearing.
    await asyncio.sleep(0)

    # Commit completed agent slots to the scrollback and clear the live zone.
    # needs_scrollback_flush=True on SlotsManager (TUI); False on ParallelDisplay (console).
    if _parallel is not None and renderer is not None and _parallel.needs_scrollback_flush:
        from rich.markup import escape as _rme
        for tb, state in zip(tool_blocks, _parallel.slot_results()):
            label = state.get("label", tb.name)
            task = tb.input.get("task", "")
            task_clean = task.replace("\n", " ").strip()
            if len(task_clean) > 58:
                task_clean = task_clean[:58] + "…"
            renderer.on_info(
                f"[#888888]⏺[/]  [bold]\\[{_rme(label)}][/]  [#C0C0C0]{_rme(task_clean)}[/]"
            )
            status = state.get("status", "")
            if status == "complete":
                latency = state.get("latency_ms", 0) / 1000
                renderer.on_info(f"   [muted]└─[/]  [bold #4CAF50]done ({latency:.1f}s)[/]")
                preview = state.get("preview", "")
                if preview:
                    renderer.on_info(f"       [#C0C0C0]{_rme(preview[:100])}[/]")
            elif status == "error":
                error = state.get("error", "unknown error")
                renderer.on_info(f"   [muted]└─[/]  [bold red]Error: {error}[/]")
        _parallel.clear()


async def _execute_parallel_tools_async(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
    renderer: Optional[OutputRenderer] = None,
) -> None:
    """Async parallel generic tool execution using asyncio.TaskGroup.

    STANDALONE mode (no outer callback): top-level parallel tools. Creates its own
    AgentLiveDisplay, pre-marks all slots running, and lets execute_async handle
    pause/resume for each confirmation via the async lock.

    SUBAGENT mode (outer callback exists): running inside spawn_agent. Notifies the
    outer slot via parallel_sub_* events instead of creating a conflicting inner
    Live display. Sets active_live_display so confirmation still pauses the outer
    display correctly.
    """
    from .agents.display import (
        ParallelDisplay,
        get_agent_display_callback, get_active_live_display,
        set_agent_display_callback, set_active_live_display,
    )

    outer_cb = get_agent_display_callback()
    outer_display = get_active_live_display()

    # ── SUBAGENT MODE: avoid nested Live display conflict ────────────────────
    if outer_cb is not None:
        outer_cb("parallel_sub_start", tools=[
            {"key": tb.id, "name": tb.name, "inputs": tb.input}
            for tb in tool_blocks
        ])

        async def _run_sub(tb: ToolUseBlock) -> str:
            if outer_display is not None:
                set_active_live_display(outer_display)
            try:
                result = await executor.execute_async(tb)
                outer_cb("parallel_sub_done", key=tb.id)
                return result
            except Exception as exc:
                outer_cb("parallel_sub_done", key=tb.id)
                return f"Error: {exc}"

        tasks_map: dict[str, asyncio.Task] = {}
        async with asyncio.TaskGroup() as tg:
            for tb in tool_blocks:
                tasks_map[tb.id] = tg.create_task(_run_sub(tb))
        outer_cb("parallel_sub_clear")
        for tb in tool_blocks:
            conversation.add_tool_result(tb.id, tasks_map[tb.id].result())
        return

    # ── STANDALONE MODE: top-level parallel tools ────────────────────────────
    _parallel = renderer.parallel_display if renderer is not None else None
    display = _parallel if _parallel is not None else ParallelDisplay()

    callbacks = {tb.id: display.make_callback(tb.id) for tb in tool_blocks}
    slots = _tool_slots(tool_blocks)
    display.pre_register(slots)

    _buffers: dict[str, _RenderBuffer] = {}

    async def _run_tb_async(tb: ToolUseBlock) -> str:
        _buf = _RenderBuffer()
        _buffers[tb.id] = _buf
        slot_cb = callbacks[tb.id]
        set_agent_display_callback(slot_cb)
        set_active_live_display(display)
        start = _time.monotonic()
        try:
            result = await executor.execute_async(tb, _slot_renderer=_buf)
            latency_ms = int((_time.monotonic() - start) * 1000)
            first_line = result.split("\n")[0][:100]
            if result.startswith("Error:"):
                slot_cb("error", error=first_line.removeprefix("Error: "))
            else:
                slot_cb("complete", latency_ms=latency_ms, preview=first_line)
            return result
        except Exception as exc:
            slot_cb("error", error=str(exc)[:80])
            return f"Error: {exc}"
        finally:
            set_agent_display_callback(None)

    tasks_map: dict[str, asyncio.Task] = {}
    # Pre-mark ALL slots "running" before any task can pause for confirmation.
    for tb in tool_blocks:
        callbacks[tb.id]("running")

    with display:
        display.render_now()
        async with asyncio.TaskGroup() as tg:
            for tb in tool_blocks:
                tasks_map[tb.id] = tg.create_task(_run_tb_async(tb))

    for tb in tool_blocks:
        conversation.add_tool_result(tb.id, tasks_map[tb.id].result())

    # Flush each tool's render buffer to the scrollback in order, then clear
    # the live slots zone. execute_async already rendered everything correctly
    # (call header, diff, result) into the per-tool buffer — no reconstruction.
    if _parallel is not None and renderer is not None and _parallel.needs_scrollback_flush:
        for tb in tool_blocks:
            _buffers.get(tb.id, _RenderBuffer()).flush_to(renderer)
        _parallel.clear()


async def _execute_tools_async(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
    renderer: Optional[OutputRenderer] = None,
) -> None:
    """Async router for tool execution — mirrors _execute_tools()."""
    if len(tool_blocks) == 1:
        tb = tool_blocks[0]
        result = await executor.execute_async(tb)
        conversation.add_tool_result(tb.id, result)
        return

    if all(tb.name in DELEGATION_TOOLS for tb in tool_blocks):
        await _execute_parallel_agents_async(tool_blocks, executor, conversation, renderer=renderer)
        return

    await _execute_parallel_tools_async(tool_blocks, executor, conversation, renderer=renderer)


async def run_prompt_async(
    prompt: str,
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    system_dynamic: str = "",
    dry_run: bool = False,
    reflect_config: Optional[ReflectionConfig] = None,
    verbose: bool = False,
    memory_tokens: int = 0,
    max_iterations: Optional[int] = None,
    tools: Optional[list[ToolDefinition]] = None,
    render_markdown: bool = False,
    markdown_title: str = "",
    spinner_label: Optional[str] = None,
    mcp_manager=None,
    capture_output: bool = False,
    enable_agents: bool = True,
    agent_depth: int = 0,
    agent_registry=None,
    agent_label: Optional[str] = None,
    a2a_manager=None,
    confirm_callback=None,
    auto_compact: bool = True,
    approval_mode: str = "off",
    permission_store=None,  # PermissionStore | None
    stream_markdown: bool = False,
    hook_runner=None,  # HookRunner | None
    tui_app=None,  # kept for backward compat — ignored; use renderer= instead
    confirmation_manager=None,  # ConfirmationManager | None — pre-built instance
    renderer: Optional[OutputRenderer] = None,  # output strategy; auto-detected when None
) -> Optional[str]:
    """Async version of run_prompt(). Same behaviour, runs in an asyncio event loop.

    Use this from async callers (e.g. run_repl_async). The sync run_prompt() is
    kept for backward-compatible callers (agents, one-shot CLI) during migration.
    """
    # Auto-detect renderer when not provided: check if TUI is active in this context.
    if renderer is None:
        from .tui import get_tui_app as _detect_tui
        _detected = _detect_tui()
        if _detected is not None:
            from .output import TuiRenderer
            renderer = TuiRenderer(_detected)
        else:
            renderer = ConsoleRenderer()
    _renderer = renderer

    limit = max_iterations if max_iterations is not None else MAX_ITERATIONS

    if tools is None and mcp_manager is not None and mcp_manager.has_tools():
        effective_tools: Optional[list[ToolDefinition]] = TOOL_DEFINITIONS + mcp_manager.get_tool_definitions()
    else:
        effective_tools = tools

    from .agents.runner import MAX_AGENT_DEPTH
    _exclude_spawn = not enable_agents or agent_depth >= MAX_AGENT_DEPTH
    if _exclude_spawn:
        base = effective_tools if effective_tools is not None else TOOL_DEFINITIONS
        effective_tools = [t for t in base if t.name != "spawn_agent"]

    _subagent_tokens: list[int] = []
    if enable_agents and agent_depth < MAX_AGENT_DEPTH and agent_registry is not None:
        from .agents import SUBAGENT_GUIDANCE
        from .agents.runner import run_agent
        _agent_runner = lambda task, role, confirm_callback=None: run_agent(  # noqa: E731
            task, role, agent_registry, client,
            parent_depth=agent_depth, mcp_manager=mcp_manager,
            _token_accumulator=_subagent_tokens,
            confirm_callback=confirm_callback,
        )
        system_prompt = system_prompt + "\n\n" + SUBAGENT_GUIDANCE
    else:
        _agent_runner = None

    if a2a_manager is not None and a2a_manager.has_agents():
        from .a2a import A2A_REMOTE_GUIDANCE
        _remote_task_runner = lambda agent, task: a2a_manager.send_task(agent, task)  # noqa: E731
        names = ", ".join(a2a_manager.agent_names())
        _a2a_guidance = A2A_REMOTE_GUIDANCE + f"\nConfigured agent names: {names}"
        system_prompt = system_prompt + "\n\n" + _a2a_guidance
    else:
        _remote_task_runner = None

    if _remote_task_runner is None:
        base_et = effective_tools if effective_tools is not None else TOOL_DEFINITIONS
        effective_tools = [t for t in base_et if t.name != "send_remote_task"]

    executor = ToolExecutor(
        dry_run=dry_run, mcp_manager=mcp_manager,
        agent_runner=_agent_runner, agent_label=agent_label,
        remote_task_runner=_remote_task_runner,
        confirm_callback=confirm_callback,
        approval_mode=approval_mode,
        permission_store=permission_store,
        hook_runner=hook_runner,
        confirmation_manager=confirmation_manager,
        renderer=_renderer,
    )
    prompt = _resolve_mentions(prompt, Path.cwd())
    conversation.add_user(prompt)
    final_usage: Optional[LLMResponse] = None
    side_effects_occurred = False
    _auto_compacted = False  # only auto-compact once per run_prompt call

    for _ in range(limit):
        try:
            result = await _stream_one_iteration_async(
                client, conversation, system_prompt, system_dynamic=system_dynamic,
                tools=effective_tools,
                silent=render_markdown,
                flush_narration=render_markdown,
                spinner_label=spinner_label,
                agent_label=agent_label,
                stream_markdown=stream_markdown,
                renderer=_renderer,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl+C before the first token arrived — nothing was committed to the
            # conversation except possibly the initial user message (a plain string).
            if conversation.messages and isinstance(conversation.messages[-1].content, str):
                conversation.messages.pop()
            _renderer.on_cancellation()
            return None
        except InputTokenRateLimitError:
            if auto_compact and not _auto_compacted:
                _auto_compacted = True
                # Pop the pending user message so compaction summarises only
                # prior history, then re-add it after so the LLM still sees it.
                pending_user_msg = None
                if conversation.messages and isinstance(conversation.messages[-1].content, str):
                    pending_user_msg = conversation.messages.pop()
                _renderer.on_info(
                    f"\n[{YELLOW}]⚠ Input token rate limit hit — auto-compacting conversation...[/]"
                )
                from .compact import get_strategy as _get_compact_strategy
                _compact_strategy = _get_compact_strategy("summary")
                with _renderer.spinner("[muted]summarizing...[/]"):
                    await asyncio.to_thread(
                        _compact_strategy.compact, conversation, client, system_prompt
                    )
                msgs_after = len(conversation.messages)
                _renderer.on_info(
                    f"[{YELLOW}]Compacted.[/] [muted]"
                    f"Context reduced to {msgs_after} messages — retrying...[/]"
                )
                if pending_user_msg is not None:
                    conversation.messages.append(pending_user_msg)
                continue  # retry this iteration with compacted context
            # auto_compact disabled or already tried once — surface the error
            _renderer.on_error(
                "Rate limited due to input token count. "
                "Use /compact to reduce context size, or set auto_compact = false in config.toml."
            )
            return None
        if result is None:
            return None

        if result.cancelled:
            if result.full_text:
                conversation.add_assistant(result.full_text, result.usage)
            elif conversation.messages and isinstance(conversation.messages[-1].content, str):
                # First iteration, no output — clean up the pending user message
                conversation.messages.pop()
            _renderer.on_cancellation()
            return None

        if result.usage:
            final_usage = result.usage

        if result.stop_reason not in ("end_turn", "tool_use"):
            conversation.add_assistant(result.full_text, result.usage)
            if _get_slot_cb() is None:
                _renderer.on_stop_reason(result.stop_reason)
            if capture_output:
                return result.full_text
            break

        if result.stop_reason == "end_turn":
            conversation.add_assistant(result.full_text, result.usage)
            if (_scb := _get_slot_cb()) is not None:
                _scb("turn_end", messages=_snapshot_messages(conversation.messages))
            if capture_output:
                return result.full_text
            if render_markdown and result.full_text:
                _renderer.on_markdown_panel(result.full_text, title=markdown_title or "Response")
            if reflect_config and reflect_config.depth > 0:
                if side_effects_occurred:
                    _renderer.on_info("[muted]  ↳ reflection skipped (side-effecting tools were used)[/]")
                else:
                    _run_reflection(
                        prompt=prompt,
                        response=result.full_text,
                        client=client,
                        config=reflect_config,
                        verbose=verbose,
                        conversation=conversation,
                    )
            if hook_runner is not None:
                from pathlib import Path as _Path
                from .hooks.events import StopTurnEvent
                from .tracing import get_tracer as _gt
                await hook_runner.fire(StopTurnEvent(
                    session_id=_gt().session_id or "",
                    cwd=_Path.cwd(),
                    response_text=result.full_text,
                ))
            break

        if result.stop_reason == "tool_use":
            conversation.add_assistant_blocks(_build_content_blocks(result), result.usage)

            if dry_run:
                for tb in result.tool_blocks:
                    _renderer.on_tool_call(tb.name, tb.input, dry_run=True)
                _renderer.on_info(f"\n[muted]Dry-run complete. {len(result.tool_blocks)} tool call(s) shown.[/]")
                break

            for tb in result.tool_blocks:
                if tb.name in SIDE_EFFECTING_TOOLS or "__" in tb.name or tb.name in DELEGATION_TOOLS:
                    side_effects_occurred = True

            try:
                await _execute_tools_async(result.tool_blocks, executor, conversation, renderer=_renderer)
            except (KeyboardInterrupt, asyncio.CancelledError):
                _complete_cancelled_tools(result.tool_blocks, conversation)
                _renderer.on_cancellation()
                return None
            if (_scb := _get_slot_cb()) is not None:
                _scb("turn_end", messages=_snapshot_messages(conversation.messages))
            if _get_slot_cb() is None:
                _renderer.on_info("")  # blank line spacer after tool block (console only shows blank)

    else:
        _renderer.on_iteration_limit(limit)

    if not capture_output:
        system_prompt_tokens = len(system_prompt) // 4
        if final_usage:
            total_input = (final_usage.input_tokens + final_usage.cache_read_tokens
                           + final_usage.cache_creation_tokens)
            conversation.truncate_if_needed(total_input, final_usage.output_tokens)
        snapshot = conversation.build_snapshot(final_usage, system_prompt_tokens, memory_tokens)
        _renderer.on_session_summary(snapshot, approval_mode=approval_mode)
        if _subagent_tokens:
            _renderer.on_subagent_tokens(len(_subagent_tokens), sum(_subagent_tokens))
    return None
