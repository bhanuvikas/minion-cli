"""Core ReAct loop — streaming iteration and main run_prompt_async loop.

Flow overview:
  run_prompt_async()
    │  Assembles the effective tool set (native + MCP tools, filtered by depth/mode)
    │  Loops up to MAX_ITERATIONS:
    │    _stream_one_iteration_async()
    │      │  Opens client.async_stream() → async generator of events
    │      │  Waits for first event inside a spinner, then processes the rest
    │      │  Dispatches: TextChunk → display, ToolUseBlock → accumulate, StreamComplete → capture
    │      └  Returns _IterationResult(full_text, tool_blocks, stop_reason)
    │    stop_reason == "end_turn"  → commit to conversation, optional reflection, break
    │    stop_reason == "tool_use"  → _execute_tools_async() → loop for next LLM turn
    │    rate limit hit             → auto-compact conversation history, retry same iteration
    └  Prints session summary (token usage, cost estimate)
"""

import asyncio
import contextlib
import dataclasses
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..llm.conversation import Conversation
from ..llm.base import (
    ContentTextBlock, ContentToolResultBlock, ContentToolUseBlock, InputTokenRateLimitError,
    LLMClient, LLMResponse, StreamComplete, TextChunk, ToolAccumulationStart,
    ToolDefinition, ToolUseBlock,
)
from ..output import ConsoleRenderer, OutputRenderer
from ..llm.reflection import ReflectionConfig, reflect
from ..theme import (
    BLUE, YELLOW, console,
    print_critique, print_diff, print_iteration_limit,
    print_reflection_header,
)
from ..agents.display import get_agent_display_callback as _get_slot_cb
from ..tools.definitions import DELEGATION_TOOLS, SIDE_EFFECTING_TOOLS, TOOL_DEFINITIONS
from ..tools.executor import ToolExecutor
from ..tracing import get_tracer
from .context import _resolve_mentions, _serialize_messages, _snapshot_messages
from .parallel import _execute_tools_async

MAX_ITERATIONS = 20
_SPINNER_LABEL = f"[{YELLOW}]🍌  thinking...[/]"


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
                if isinstance(block, ContentToolResultBlock):
                    completed_ids.add(block.tool_use_id)
    for tb in tool_blocks:
        if tb.id not in completed_ids:
            conversation.add_tool_result(tb.id, "[Cancelled by user]")


def _build_content_blocks(result: "_IterationResult") -> list:
    """Assemble typed ContentBlocks for an assistant tool-use turn."""
    blocks = []
    if result.full_text:
        blocks.append(ContentTextBlock(text=result.full_text))
    for tb in result.tool_blocks:
        blocks.append(ContentToolUseBlock(id=tb.id, name=tb.name, input=tb.input))
    return blocks


def _run_reflection(
    prompt: str,
    response: str,
    client: LLMClient,
    config: ReflectionConfig,
    verbose: bool,
    conversation: Conversation,
) -> None:
    """Run the self-refine loop and update the conversation if refined."""
    from ..llm.base import Message

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
        conversation.messages[-1] = Message(
            role="assistant", content=result.final_response
        )
    else:
        score_hint = f" · score: {result.final_score}/10" if verbose else ""
        console.print(f"[muted]  ↳ accepted{score_hint}[/]")


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
    """One LLM call: spin → stream events → structured result."""
    _llm_start = _time.monotonic()
    effective_tools = tools if tools is not None else TOOL_DEFINITIONS
    get_tracer().emit(
        "llm_request",
        message_count=len(conversation.messages),
        messages=_serialize_messages(conversation.messages),
        system=system_prompt,
        tools=[dataclasses.asdict(t) for t in effective_tools],
        tool_names=[t.name for t in effective_tools],
        model=getattr(client, "model_id", "unknown"),
        estimated_input_tokens=sum(len(str(m.content)) for m in conversation.messages) // 4,
    )
    effective_spinner = spinner_label or _SPINNER_LABEL
    _renderer = renderer or ConsoleRenderer()

    gen = client.async_stream(conversation.messages, system=system_prompt, system_dynamic=system_dynamic, tools=effective_tools)
    # Consume the first event inside a spinner so it spins while the server responds.
    # Skip when a slot display is already active (subagent mode) to avoid nested spinners.
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

    # Inline closure: processes each streaming event without thread-hopping.
    # TextChunk → display text; ToolUseBlock → accumulate for later execution;
    # StreamComplete → capture stop_reason and token usage.
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

    # silent=True: accumulate text without streaming, then optionally flush it as narration.
    # Used when render_markdown=True so the full response renders as a panel afterward.
    # silent=False: stream each TextChunk to the renderer as it arrives (default REPL mode).
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
    confirm_callback=None,
    auto_compact: bool = True,
    approval_mode: str = "off",
    permission_store=None,
    stream_markdown: bool = False,
    hook_runner=None,
    tui_app=None,
    confirmation_manager=None,
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
    permission_store=None,
    stream_markdown: bool = False,
    hook_runner=None,
    tui_app=None,
    confirmation_manager=None,
    renderer: Optional[OutputRenderer] = None,
) -> Optional[str]:
    """Orchestrate the full agent loop (LLM ↔ tools)*.

    Use this from async callers (e.g. run_repl_async). The sync run_prompt() is
    kept for backward-compatible callers (agents, one-shot CLI).
    """
    # Auto-detect renderer when not provided: check if TUI is active in this context.
    if renderer is None:
        from ..tui import get_tui_app as _detect_tui
        _detected = _detect_tui()
        if _detected is not None:
            from ..output import TuiRenderer
            renderer = TuiRenderer(_detected)
        else:
            renderer = ConsoleRenderer()
    _renderer = renderer

    limit = max_iterations if max_iterations is not None else MAX_ITERATIONS

    # Merge MCP tools into the native set; then strip spawn_agent / send_remote_task
    # based on depth, agent_enabled flag, and A2A availability.
    if tools is None and mcp_manager is not None and mcp_manager.has_tools():
        effective_tools: Optional[list[ToolDefinition]] = TOOL_DEFINITIONS + mcp_manager.get_tool_definitions()
    else:
        effective_tools = tools

    from ..agents.runner import MAX_AGENT_DEPTH
    # Remove spawn_agent at max recursion depth to prevent runaway subagent chains.
    _exclude_spawn = not enable_agents or agent_depth >= MAX_AGENT_DEPTH
    if _exclude_spawn:
        base = effective_tools if effective_tools is not None else TOOL_DEFINITIONS
        effective_tools = [t for t in base if t.name != "spawn_agent"]

    _subagent_tokens: list[int] = []
    if enable_agents and agent_depth < MAX_AGENT_DEPTH and agent_registry is not None:
        from ..agents import SUBAGENT_GUIDANCE
        from ..agents.runner import run_agent
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
        from ..a2a import A2A_REMOTE_GUIDANCE
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
    # Expand any @file.py mentions in the prompt by appending their contents.
    prompt = _resolve_mentions(prompt, Path.cwd())
    conversation.add_user(prompt)
    final_usage: Optional[LLMResponse] = None
    side_effects_occurred = False
    _auto_compacted = False  # only auto-compact once per run_prompt call

    # ── ReAct loop: LLM call → tool execution → repeat ───────────────────────
    # Each iteration calls the LLM and dispatches any tool calls it requests.
    # Exits when model says end_turn, hits the limit, or is cancelled / rate-limited.
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
            # Context window full: auto-compact conversation history and retry this iteration.
            # _auto_compacted guards against infinite retry loops if compaction doesn't help.
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
                from ..compact import get_strategy as _get_compact_strategy
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

        # Dispatch on stop_reason: end_turn → done, tool_use → run tools and loop back.
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
                from ..hooks.events import StopTurnEvent
                from ..tracing import get_tracer as _gt
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

            # Execute all tool calls (potentially in parallel), inject results into conversation.
            try:
                await _execute_tools_async(result.tool_blocks, executor, conversation, renderer=_renderer)
            except (KeyboardInterrupt, asyncio.CancelledError):
                _complete_cancelled_tools(result.tool_blocks, conversation)
                _renderer.on_cancellation()
                return None
            if (_scb := _get_slot_cb()) is not None:
                _scb("turn_end", messages=_snapshot_messages(conversation.messages))
            if _get_slot_cb() is None:
                _renderer.on_info("")  # blank line spacer after tool block (console only)

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
