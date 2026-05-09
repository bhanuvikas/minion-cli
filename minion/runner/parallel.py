"""Parallel tool and agent execution.

Two parallel paths, selected by _execute_tools_async():
  1. Delegation tools (spawn_agent / send_remote_task) → _execute_parallel_agents_async()
       Each agent runs in asyncio.to_thread() (sync run_prompt() under the hood).
       A thread-local display callback routes each agent's output to its own slot.
       asyncio.TaskGroup waits for all agents, then results are injected into conversation.

  2. Generic parallel tools → _execute_parallel_tools_async()
       STANDALONE mode (top-level call): creates its own ParallelDisplay / SlotsManager,
         each tool runs in asyncio.to_thread(), a _RenderBuffer captures output per-tool,
         flushed to scrollback in order after all tasks complete.
       SUBAGENT mode (inside spawn_agent): outer slot display already active, so uses
         parallel_sub_* events on the outer callback instead of a nested Live display.
"""

import asyncio
import time as _time
from typing import Optional

from ..llm.conversation import Conversation
from ..llm.base import ToolUseBlock
from ..output import OutputRenderer
from ..output.base import SlotSpec
from ..tools.definitions import DELEGATION_TOOLS
from ..tools.executor import ToolExecutor, _RenderBuffer
from ..tracing import get_tracer


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
    from ..agents.display import ParallelDisplay, set_agent_display_callback, set_active_live_display

    _parallel = renderer.parallel_display if renderer is not None else None
    display = _parallel if _parallel is not None else ParallelDisplay()

    slots = _agent_slots(tool_blocks)
    await display.pre_register_async(slots)

    # Register agents with the inspector registry and wrap display callbacks so
    # turn_end / complete / error events also update the registry.
    from ..tui.agent_registry import get_registry as _get_agent_registry
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
        # set_agent_display_callback() uses a threading.local so each concurrent agent
        # routes its text/tool output to its own slot without interfering with others.
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
                from ..tools.executor import _diff_lines_for_panel as _dlp
                from ..tui import is_tui_active as _ita
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
    # Pre-mark all slots "running" before starting any task — prevents stale idle state
    # if a fast-completing agent wins the slot before slower ones show their status.
    for tb in tool_blocks:
        _callbacks[tb.id]("running")

    # TaskGroup starts all agents concurrently and awaits all of them.
    # If any task raises, the group cancels the rest and re-raises.
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
        from ..output.formatter import format_agent_slot_summary as _fmt_agent_slot
        for tb, state in zip(tool_blocks, _parallel.slot_results()):
            label = state.get("label", tb.name)
            task  = tb.input.get("task", "")
            for line in _fmt_agent_slot(label, task, state):
                renderer.on_info(line)
        _parallel.clear()


async def _execute_parallel_tools_async(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
    renderer: Optional[OutputRenderer] = None,
) -> None:
    """Async parallel generic tool execution using asyncio.TaskGroup.

    STANDALONE mode (no outer callback): top-level parallel tools. Creates its own
    ParallelDisplay, pre-marks all slots running, and lets execute_async handle
    pause/resume for each confirmation via the async lock.

    SUBAGENT mode (outer callback exists): running inside spawn_agent. Notifies the
    outer slot via parallel_sub_* events instead of creating a conflicting inner
    Live display. Sets active_live_display so confirmation still pauses the outer
    display correctly.
    """
    from ..agents.display import (
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
        # _RenderBuffer captures each tool's renderer calls (call header, diff, result)
        # so they can be replayed to scrollback in order after all tasks complete.
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
    """Route tool calls: single fast path, parallel agents, or parallel generic tools."""
    if len(tool_blocks) == 1:
        tb = tool_blocks[0]
        result = await executor.execute_async(tb)
        conversation.add_tool_result(tb.id, result)
        return

    if all(tb.name in DELEGATION_TOOLS for tb in tool_blocks):
        await _execute_parallel_agents_async(tool_blocks, executor, conversation, renderer=renderer)
        return

    await _execute_parallel_tools_async(tool_blocks, executor, conversation, renderer=renderer)
