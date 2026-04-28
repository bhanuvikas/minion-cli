"""Agent loop: user prompt → [LLM ↔ tools]* → streamed response.

Core of the ReAct pattern: on each iteration the model either finishes
(stop_reason="end_turn") or requests a tool call (stop_reason="tool_use").
Tool results are injected back as observations and the loop continues.

Responsibilities:
  run_prompt()              — orchestrates the full agent loop
  _resolve_mentions()       — expand @file.py references before sending to LLM
  _stream_one_iteration()   — one LLM call: spin → stream events → structured result
  _build_content_blocks()   — assemble content block list for conversation storage
  _execute_tools()          — run each tool call, inject results into conversation
"""

import asyncio
import contextlib
import re
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .conversation import Conversation
from .llm.base import ContentTextBlock, ContentToolUseBlock, LLMClient, LLMResponse, StreamComplete, TextChunk, ToolUseBlock
from .reflection import ReflectionConfig, ReflectionResult, reflect
from .theme import (
    BLUE, YELLOW, console,
    print_critique, print_diff, print_error, print_iteration_limit,
    print_reflection_header, print_tool_call, print_usage,
)
from .agents.display import get_agent_display_callback as _get_slot_cb
from .tools.definitions import DELEGATION_TOOLS, SIDE_EFFECTING_TOOLS, TOOL_DEFINITIONS
from .tools.executor import ToolExecutor
from .tracing import get_tracer

MAX_ITERATIONS = 20
_SPINNER_LABEL = f"[{YELLOW}]🍌  Bee-do bee-do...[/]"


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

def _stream_one_iteration(
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    tools: Optional[list] = None,
    silent: bool = False,
    flush_narration: bool = True,
    spinner_label: Optional[str] = None,
    agent_label: Optional[str] = None,
) -> Optional[_IterationResult]:
    """Run one LLM streaming call and collect all events into a structured result.

    When silent=False (default): shows spinner until first token, then streams
    text directly to stdout as it arrives.

    When silent=True: suppresses all stdout output and keeps the spinner active
    for the full stream duration. Used by skills with output_format=markdown so
    the response can be collected and rendered as rich Markdown after the loop.

    Returns None on error (already displayed) and pops the pending user message
    so conversation history stays consistent.
    """
    _llm_start = _time.monotonic()
    effective_tools = tools if tools is not None else TOOL_DEFINITIONS
    get_tracer().emit(
        "llm_request",
        message_count=len(conversation.messages),
        messages=_serialize_messages(conversation.messages),
        system=system_prompt,
        tools=effective_tools,
        tool_names=[t["name"] for t in effective_tools],
        model=getattr(client, "model_id", "unknown"),
        estimated_input_tokens=sum(len(str(m.content)) for m in conversation.messages) // 4,
    )
    effective_spinner = spinner_label or _SPINNER_LABEL
    try:
        stream = client.stream(conversation.messages, system=system_prompt, tools=effective_tools)
        _in_live = _get_slot_cb() is not None
        _first_cm = contextlib.nullcontext() if _in_live else console.status(effective_spinner, spinner="dots")
        with _first_cm:
            first_event = next(stream, None)
    except Exception as e:
        get_tracer().emit(
            "llm_error",
            error=str(e),
            latency_ms=int((_time.monotonic() - _llm_start) * 1000),
        )
        conversation.messages.pop()
        print_error(str(e))
        return None

    if first_event is None:
        conversation.messages.pop()
        print_error("Received an empty response from the model.")
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
                # Parallel agent mode: route thinking text to the slot display.
                # Suppress stdout entirely — writing to it would corrupt the Live display.
                _slot_cb("text", text=event.text)
                return
            if not silent:
                if not printed_prefix:
                    display_name = agent_label or "minion"
                    console.print(f"[bold {BLUE}]{display_name}[/] › ", end="")
                    printed_prefix = True
                sys.stdout.write(event.text)
                sys.stdout.flush()
        elif isinstance(event, ToolUseBlock):
            tool_blocks.append(event)
        elif isinstance(event, StreamComplete):
            stop_reason = event.stop_reason
            usage = LLMResponse(
                content="",
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                model=event.model,
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

    if silent:
        # Spinner covers the full LLM streaming call.
        # Narration text is collected but not printed yet — we decide after:
        #   tool_use turn → flush narration so user sees LLM's reasoning
        #   end_turn      → suppress (the markdown Panel replaces it)
        # When inside a Live context (parallel agents), use nullcontext to avoid
        # conflicting with the Live display.
        _in_live2 = _get_slot_cb() is not None
        _silent_cm = contextlib.nullcontext() if _in_live2 else console.status(effective_spinner, spinner="dots")
        with _silent_cm:
            _process(first_event)
            try:
                for event in stream:
                    _process(event)
            except KeyboardInterrupt:
                pass
        if flush_narration and stop_reason == "tool_use" and text_chunks:
            display_name = agent_label or "minion"
            console.print(f"[bold {BLUE}]{display_name}[/] › ", end="")
            sys.stdout.write("".join(text_chunks))
            print()
    else:
        _process(first_event)
        try:
            for event in stream:
                _process(event)
        except KeyboardInterrupt:
            pass  # Ctrl+C mid-stream — stop cleanly, no traceback

    if text_chunks and not silent and _get_slot_cb() is None:
        print()  # newline after streamed text

    return _IterationResult(
        full_text="".join(text_chunks),
        tool_blocks=tool_blocks,
        stop_reason=stop_reason,
        usage=usage,
    )


def _build_content_blocks(result: _IterationResult) -> list:
    """Assemble typed ContentBlocks for an assistant tool-use turn."""
    blocks = []
    if result.full_text:
        blocks.append(ContentTextBlock(text=result.full_text))
    for tb in result.tool_blocks:
        blocks.append(ContentToolUseBlock(id=tb.id, name=tb.name, input=tb.input))
    return blocks


def _execute_parallel_agents(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
) -> None:
    """Execute spawn_agent and send_remote_task calls concurrently with a live display.

    Each delegation tool block gets its own 3-line slot (header + status + detail)
    inside the Live area. For spawn_agent, the subagent's thinking/tool activity is
    routed through the slot callback (thread-local). For send_remote_task, the slot
    shows running → complete/error without subagent internals (remote agent is opaque).
    Traces are emitted manually (bypasses executor.execute() print helpers).
    """
    from .agents.display import AgentLiveDisplay, SlotSpec, set_agent_display_callback

    results: dict[str, str] = {}
    display = AgentLiveDisplay()

    # Use tb.id as slot key (unique per block) to avoid collision when the same
    # role or agent name appears twice. label= is the human-readable name shown
    # in the status row (role for spawn_agent, agent name for send_remote_task).
    slots = []
    for tb in tool_blocks:
        if tb.name == "spawn_agent":
            label = tb.input.get("role") or "researcher"
        else:  # send_remote_task
            label = tb.input.get("agent") or "remote"
        slots.append(SlotSpec(key=tb.id, tool_name=tb.name, inputs=tb.input, label=label))

    # Pre-register all slots before the Live context starts so the display has a
    # fixed height from the first render — Rich Live never resizes, eliminating flicker.
    display.pre_register(slots)

    def run_spawn_agent(tb: ToolUseBlock) -> str:
        task = tb.input.get("task", "")
        role = tb.input.get("role") or "researcher"
        callback = display.make_callback(tb.id)
        set_agent_display_callback(callback)
        try:
            get_tracer().emit("tool_call", tool_name="spawn_agent", inputs=tb.input)
            result = executor._agent_runner(task, role)
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=result, success=True)
            return result
        except Exception as exc:
            err = f"Error: {exc}"
            get_tracer().emit("tool_result", tool_name="spawn_agent", output=err, success=False)
            return err
        finally:
            set_agent_display_callback(None)

    def run_remote_task(tb: ToolUseBlock) -> str:
        agent = tb.input.get("agent", "")
        task = tb.input.get("task", "")
        callback = display.make_callback(tb.id)
        callback("running")
        start = _time.monotonic()
        try:
            get_tracer().emit("tool_call", tool_name="send_remote_task", inputs=tb.input)
            result = executor._remote_task_runner(agent, task)
            latency_ms = int((_time.monotonic() - start) * 1000)
            preview = result.split("\n")[0][:100] if result else ""
            if result.startswith("Error:"):
                callback("error", error=result.removeprefix("Error: ")[:60])
            else:
                callback("complete", latency_ms=latency_ms, preview=preview)
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=result, success=True)
            return result
        except Exception as exc:
            callback("error", error=str(exc)[:60])
            get_tracer().emit("tool_result", tool_name="send_remote_task", output=str(exc), success=False)
            return f"Error: {exc}"

    def run_one(tb: ToolUseBlock) -> str:
        if tb.name == "spawn_agent":
            return run_spawn_agent(tb)
        return run_remote_task(tb)

    with display:
        with ThreadPoolExecutor(max_workers=len(tool_blocks)) as pool:
            future_to_block = {pool.submit(run_one, tb): tb for tb in tool_blocks}
            for future in as_completed(future_to_block):
                tb = future_to_block[future]
                try:
                    results[tb.id] = future.result()
                except Exception as exc:
                    results[tb.id] = f"Error: {exc}"

    # Inject in original order — preserves conversation history coherence.
    for tb in tool_blocks:
        conversation.add_tool_result(tb.id, results[tb.id])


def _execute_parallel_tools(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
) -> None:
    """Execute multiple non-agent tool calls concurrently with a grouped live display.

    Each tool block gets its own 3-line slot (⚙ header + status + result preview).
    The slot callback is set on each thread so executor.execute() routes its
    print_tool_call / print_tool_result through the callback instead of stdout,
    preventing output from corrupting the Live display.
    """
    from .agents.display import AgentLiveDisplay, SlotSpec, set_agent_display_callback

    display = AgentLiveDisplay()

    # tb.id is unique per tool_use block even when the same tool is called twice.
    slots = [
        SlotSpec(key=tb.id, tool_name=tb.name, inputs=tb.input, label=None)
        for tb in tool_blocks
    ]
    display.pre_register(slots)

    results: dict[str, str] = {}

    def _run_tb(tb: ToolUseBlock) -> str:
        slot_cb = display.make_callback(tb.id)
        slot_cb("running")
        set_agent_display_callback(slot_cb)
        start = _time.monotonic()
        try:
            result = executor.execute(tb)  # print_tool_call/result suppressed via slot_cb
            latency_ms = int((_time.monotonic() - start) * 1000)
            first_line = result.split("\n")[0][:100]
            if result.startswith("Error:"):
                slot_cb("error", error=first_line.removeprefix("Error: "))
            else:
                slot_cb("complete", latency_ms=latency_ms, preview=first_line)
            return result
        except Exception as exc:
            slot_cb("error", error=str(exc))
            raise
        finally:
            set_agent_display_callback(None)

    with display:
        with ThreadPoolExecutor(max_workers=len(tool_blocks)) as pool:
            future_to_block = {pool.submit(_run_tb, tb): tb for tb in tool_blocks}
            for future in as_completed(future_to_block):
                tb = future_to_block[future]
                try:
                    results[tb.id] = future.result()
                except Exception as exc:
                    results[tb.id] = f"Error: {exc}"

    for tb in tool_blocks:
        conversation.add_tool_result(tb.id, results[tb.id])


def _execute_tools(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
) -> None:
    """Execute all tool calls from one LLM turn and inject results into conversation.

    When the model requests a single tool call the fast path avoids thread
    overhead. When it requests multiple tool calls they run concurrently via
    ThreadPoolExecutor and results are injected in the original order so the
    conversation stays coherent regardless of which thread finishes first.

    Thread-safety:
    - ToolExecutor.execute() is stateless (reads dry_run/mcp_manager, never writes).
    - _CONFIRM_LOCK in executor.py serializes questionary.confirm() calls.
    - conversation.add_tool_result() is called sequentially after all futures settle.
    """
    if len(tool_blocks) == 1:
        # Fast path: single tool call — no threading overhead.
        tb = tool_blocks[0]
        conversation.add_tool_result(tb.id, executor.execute(tb))
        return

    # Parallel delegation path: all blocks are delegation tools (spawn_agent or
    # send_remote_task) — use the agent live display which shows status per slot.
    if all(tb.name in DELEGATION_TOOLS for tb in tool_blocks):
        _execute_parallel_agents(tool_blocks, executor, conversation)
        return

    # Parallel generic path: any mix of non-agent tool calls — use the generic
    # live display so each tool gets its own slot instead of interleaving output.
    _execute_parallel_tools(tool_blocks, executor, conversation)


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
    tools: Optional[list] = None,
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
) -> Optional[str]:
    """Run the ReAct agent loop for a single user prompt.

    Loops up to max_iterations (defaults to MAX_ITERATIONS). Each iteration is
    one LLM call; if the model requests tool use the results are observed and
    the loop continues. The loop exits when the model signals end_turn or
    dry_run stops it after the first tool-use iteration.

    When reflect_config is provided and depth > 0, runs the self-refine loop
    after the final end_turn response before returning.

    When capture_output=True (used by subagents), all terminal output is
    suppressed and the final response text is returned as a string. The return
    value is None in normal (streaming) mode and str in capture mode.
    """
    limit = max_iterations if max_iterations is not None else MAX_ITERATIONS

    # ── Build effective tool list ──────────────────────────────────────────────
    # MCP tools appended only in all-tools mode (tools=None). Skills/subagents
    # that declare an explicit tool subset intentionally skip MCP tools.
    if tools is None and mcp_manager is not None and mcp_manager.has_tools():
        effective_tools: Optional[list] = TOOL_DEFINITIONS + mcp_manager.get_tool_definitions()
    else:
        effective_tools = tools  # None → _stream_one_iteration uses TOOL_DEFINITIONS

    # spawn_agent is excluded when agents are disabled or when we're already
    # inside a worker (depth >= MAX_AGENT_DEPTH prevents recursive spawning).
    from .agents.runner import MAX_AGENT_DEPTH
    _exclude_spawn = not enable_agents or agent_depth >= MAX_AGENT_DEPTH
    if _exclude_spawn:
        base = effective_tools if effective_tools is not None else TOOL_DEFINITIONS
        effective_tools = [t for t in base if t["name"] != "spawn_agent"]

    # ── Build agent runner + inject subagent guidance into system prompt ───────
    _subagent_tokens: list[int] = []  # accumulates total_tokens per subagent
    if enable_agents and agent_depth < MAX_AGENT_DEPTH and agent_registry is not None:
        from .agents import SUBAGENT_GUIDANCE
        from .agents.runner import run_agent
        _agent_runner = lambda task, role: run_agent(  # noqa: E731
            task, role, agent_registry, client,
            parent_depth=agent_depth, mcp_manager=mcp_manager,
            _token_accumulator=_subagent_tokens,
        )
        system_prompt = system_prompt + "\n\n" + SUBAGENT_GUIDANCE
    else:
        _agent_runner = None

    # ── Build remote task runner + inject A2A guidance into system prompt ──────
    if a2a_manager is not None and a2a_manager.has_agents():
        from .a2a import A2A_REMOTE_GUIDANCE
        _remote_task_runner = lambda agent, task: a2a_manager.send_task(agent, task)  # noqa: E731
        # Build dynamic guidance listing available agent names
        names = ", ".join(a2a_manager.agent_names())
        _a2a_guidance = A2A_REMOTE_GUIDANCE + f"\nConfigured agent names: {names}"
        system_prompt = system_prompt + "\n\n" + _a2a_guidance
        # Filter send_remote_task out of any explicitly-provided tool list so the
        # dynamic description (with agent names) in the system prompt is authoritative.
    else:
        _remote_task_runner = None

    # Filter send_remote_task when no A2A manager is available.
    if _remote_task_runner is None:
        base_et = effective_tools if effective_tools is not None else TOOL_DEFINITIONS
        effective_tools = [t for t in base_et if t["name"] != "send_remote_task"]

    executor = ToolExecutor(
        dry_run=dry_run, mcp_manager=mcp_manager,
        agent_runner=_agent_runner, agent_label=agent_label,
        remote_task_runner=_remote_task_runner,
        confirm_callback=confirm_callback,
    )
    prompt = _resolve_mentions(prompt, Path.cwd())
    conversation.add_user(prompt)
    final_usage: Optional[LLMResponse] = None
    side_effects_occurred = False

    for _ in range(limit):
        # ── One LLM call ──────────────────────────────────────────────────────
        result = _stream_one_iteration(
            client, conversation, system_prompt, tools=effective_tools,
            silent=render_markdown,
            flush_narration=render_markdown,
            spinner_label=spinner_label,
            agent_label=agent_label,
        )
        if result is None:
            return  # error already displayed and user message popped

        if result.usage:
            final_usage = result.usage

        # ── End turn: model finished responding (or hit output token limit) ─────
        if result.stop_reason not in ("end_turn", "tool_use"):
            # max_tokens or other unexpected stop — treat partial response as final.
            # Do NOT loop: the model stopped generating, continuing would just burn
            # tokens repeating context without new information.
            conversation.add_assistant(result.full_text, result.usage)
            if _get_slot_cb() is None:
                console.print(f"\n[muted]  ↳ stopped: {result.stop_reason}[/]")
            if capture_output:
                return result.full_text
            break

        if result.stop_reason == "end_turn":
            conversation.add_assistant(result.full_text, result.usage)
            if capture_output:
                # Subagent mode: suppress all terminal output, return text.
                return result.full_text
            if render_markdown and result.full_text:
                from rich.markdown import Markdown
                from rich.panel import Panel
                console.print(
                    Panel(
                        Markdown(result.full_text),
                        title=f"[bold {YELLOW}]{markdown_title or 'Response'}[/]",
                        expand=False,
                        border_style="dim",
                    )
                )
            if reflect_config and reflect_config.depth > 0:
                if side_effects_occurred:
                    console.print("[muted]  ↳ reflection skipped (side-effecting tools were used)[/]")
                else:
                    _run_reflection(
                        prompt=prompt,
                        response=result.full_text,
                        client=client,
                        config=reflect_config,
                        verbose=verbose,
                        conversation=conversation,
                    )
            break

        # ── Tool use: execute calls, inject observations, loop ────────────────
        if result.stop_reason == "tool_use":
            conversation.add_assistant_blocks(_build_content_blocks(result), result.usage)

            if dry_run:
                for tb in result.tool_blocks:
                    print_tool_call(tb.name, tb.input, dry_run=True)
                console.print(f"\n[muted]Dry-run complete. {len(result.tool_blocks)} tool call(s) shown.[/]")
                break

            for tb in result.tool_blocks:
                # MCP tools, delegation tools, and shell/write are treated as
                # side-effecting to prevent reflection after unknown operations.
                if tb.name in SIDE_EFFECTING_TOOLS or "__" in tb.name or tb.name in DELEGATION_TOOLS:
                    side_effects_occurred = True

            _execute_tools(result.tool_blocks, executor, conversation)
            if _get_slot_cb() is None:
                print()  # blank line before next iteration

    else:
        # for/else fires when the loop exhausted all iterations without a break
        print_iteration_limit(limit)

    # ── Post-loop: truncation, context snapshot, usage footer ─────────────────
    if not capture_output:
        system_prompt_tokens = len(system_prompt) // 4 - memory_tokens
        if final_usage:
            conversation.truncate_if_needed(final_usage.input_tokens, final_usage.output_tokens)
        snapshot = conversation.build_snapshot(final_usage, system_prompt_tokens, memory_tokens)
        print_usage(snapshot)
        if _subagent_tokens:
            total_sub = sum(_subagent_tokens)
            n_sub = len(_subagent_tokens)
            console.print(
                f"  [muted]subagents: {n_sub} agent{'s' if n_sub > 1 else ''}, "
                f"{total_sub:,} tokens total[/]"
            )
    return None


# ─── Async variants (Phase 12) ────────────────────────────────────────────────
# The async loop mirrors the sync loop exactly but uses client.async_stream()
# and asyncio.TaskGroup for parallel tool/agent execution. The sync run_prompt()
# and helpers above are kept for backward compatibility during the migration.


async def _stream_one_iteration_async(
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    tools: Optional[list] = None,
    silent: bool = False,
    flush_narration: bool = True,
    spinner_label: Optional[str] = None,
    agent_label: Optional[str] = None,
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
        tool_names=[t["name"] for t in effective_tools],
        model=getattr(client, "model_id", "unknown"),
        estimated_input_tokens=sum(len(str(m.content)) for m in conversation.messages) // 4,
    )
    effective_spinner = spinner_label or _SPINNER_LABEL

    gen = client.async_stream(conversation.messages, system=system_prompt, tools=effective_tools)
    _in_live = _get_slot_cb() is not None
    _first_cm = contextlib.nullcontext() if _in_live else console.status(effective_spinner, spinner="dots")

    try:
        with _first_cm:
            first_event = await gen.__anext__()
    except StopAsyncIteration:
        first_event = None
    except Exception as e:
        get_tracer().emit(
            "llm_error",
            error=str(e),
            latency_ms=int((_time.monotonic() - _llm_start) * 1000),
        )
        conversation.messages.pop()
        print_error(str(e))
        return None

    if first_event is None:
        conversation.messages.pop()
        print_error("Received an empty response from the model.")
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
                _slot_cb("text", text=event.text)
                return
            if not silent:
                if not printed_prefix:
                    display_name = agent_label or "minion"
                    console.print(f"[bold {BLUE}]{display_name}[/] › ", end="")
                    printed_prefix = True
                sys.stdout.write(event.text)
                sys.stdout.flush()
        elif isinstance(event, ToolUseBlock):
            tool_blocks.append(event)
        elif isinstance(event, StreamComplete):
            stop_reason = event.stop_reason
            usage = LLMResponse(
                content="",
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                model=event.model,
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

    if silent:
        _in_live2 = _get_slot_cb() is not None
        _silent_cm = contextlib.nullcontext() if _in_live2 else console.status(effective_spinner, spinner="dots")
        with _silent_cm:
            _process(first_event)
            try:
                async for event in gen:
                    _process(event)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        if flush_narration and stop_reason == "tool_use" and text_chunks:
            display_name = agent_label or "minion"
            console.print(f"[bold {BLUE}]{display_name}[/] › ", end="")
            sys.stdout.write("".join(text_chunks))
            print()
    else:
        _process(first_event)
        try:
            async for event in gen:
                _process(event)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    if text_chunks and not silent and _get_slot_cb() is None:
        print()

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
) -> None:
    """Async parallel agent dispatch using asyncio.TaskGroup.

    Each delegation tool gets its own slot in AgentLiveDisplay. Tool execution
    runs in asyncio.to_thread() so sync agent runners don't block the event loop.
    The callback is set inside the thread (thread-local) to route output correctly.
    """
    from .agents.display import AgentLiveDisplay, SlotSpec, set_agent_display_callback

    display = AgentLiveDisplay()
    slots = []
    for tb in tool_blocks:
        if tb.name == "spawn_agent":
            label = tb.input.get("role") or "researcher"
        else:
            label = tb.input.get("agent") or "remote"
        slots.append(SlotSpec(key=tb.id, tool_name=tb.name, inputs=tb.input, label=label))
    display.pre_register(slots)

    async def _run_spawn_async(tb: ToolUseBlock) -> str:
        callback = display.make_callback(tb.id)
        # ContextVar set here propagates into execute_async() and any threads it spawns.
        set_agent_display_callback(callback)
        return await executor.execute_async(tb)

    async def _run_remote_async(tb: ToolUseBlock) -> str:
        callback = display.make_callback(tb.id)
        callback("running")
        start = _time.monotonic()
        set_agent_display_callback(callback)
        result = await executor.execute_async(tb)
        latency_ms = int((_time.monotonic() - start) * 1000)
        preview = result.split("\n")[0][:100] if result else ""
        if result.startswith("Error:"):
            callback("error", error=result.removeprefix("Error: ")[:60])
        else:
            callback("complete", latency_ms=latency_ms, preview=preview)
        return result

    async def _run_one_async(tb: ToolUseBlock) -> str:
        if tb.name == "spawn_agent":
            return await _run_spawn_async(tb)
        return await _run_remote_async(tb)

    tasks_map: dict[str, asyncio.Task] = {}
    with display:
        async with asyncio.TaskGroup() as tg:
            for tb in tool_blocks:
                tasks_map[tb.id] = tg.create_task(_run_one_async(tb))

    for tb in tool_blocks:
        conversation.add_tool_result(tb.id, tasks_map[tb.id].result())


async def _execute_parallel_tools_async(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
) -> None:
    """Async parallel generic tool execution using asyncio.TaskGroup."""
    from .agents.display import AgentLiveDisplay, SlotSpec, set_agent_display_callback

    display = AgentLiveDisplay()
    slots = [
        SlotSpec(key=tb.id, tool_name=tb.name, inputs=tb.input, label=None)
        for tb in tool_blocks
    ]
    display.pre_register(slots)

    async def _run_tb_async(tb: ToolUseBlock) -> str:
        slot_cb = display.make_callback(tb.id)
        slot_cb("running")
        start = _time.monotonic()
        # ContextVar set here is visible inside execute_async() and any threads it spawns.
        set_agent_display_callback(slot_cb)
        try:
            result = await executor.execute_async(tb)
            latency_ms = int((_time.monotonic() - start) * 1000)
            first_line = result.split("\n")[0][:100]
            if result.startswith("Error:"):
                slot_cb("error", error=first_line.removeprefix("Error: "))
            else:
                slot_cb("complete", latency_ms=latency_ms, preview=first_line)
            return result
        except Exception as exc:
            slot_cb("error", error=str(exc))
            return f"Error: {exc}"

    tasks_map: dict[str, asyncio.Task] = {}
    with display:
        async with asyncio.TaskGroup() as tg:
            for tb in tool_blocks:
                tasks_map[tb.id] = tg.create_task(_run_tb_async(tb))

    for tb in tool_blocks:
        conversation.add_tool_result(tb.id, tasks_map[tb.id].result())


async def _execute_tools_async(
    tool_blocks: list[ToolUseBlock],
    executor: ToolExecutor,
    conversation: Conversation,
) -> None:
    """Async router for tool execution — mirrors _execute_tools()."""
    if len(tool_blocks) == 1:
        tb = tool_blocks[0]
        result = await asyncio.to_thread(executor.execute, tb)
        conversation.add_tool_result(tb.id, result)
        return

    if all(tb.name in DELEGATION_TOOLS for tb in tool_blocks):
        await _execute_parallel_agents_async(tool_blocks, executor, conversation)
        return

    await _execute_parallel_tools_async(tool_blocks, executor, conversation)


async def run_prompt_async(
    prompt: str,
    client: LLMClient,
    conversation: Conversation,
    system_prompt: str,
    dry_run: bool = False,
    reflect_config: Optional[ReflectionConfig] = None,
    verbose: bool = False,
    memory_tokens: int = 0,
    max_iterations: Optional[int] = None,
    tools: Optional[list] = None,
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
) -> Optional[str]:
    """Async version of run_prompt(). Same behaviour, runs in an asyncio event loop.

    Use this from async callers (e.g. run_repl_async). The sync run_prompt() is
    kept for backward-compatible callers (agents, one-shot CLI) during migration.
    """
    limit = max_iterations if max_iterations is not None else MAX_ITERATIONS

    if tools is None and mcp_manager is not None and mcp_manager.has_tools():
        effective_tools: Optional[list] = TOOL_DEFINITIONS + mcp_manager.get_tool_definitions()
    else:
        effective_tools = tools

    from .agents.runner import MAX_AGENT_DEPTH
    _exclude_spawn = not enable_agents or agent_depth >= MAX_AGENT_DEPTH
    if _exclude_spawn:
        base = effective_tools if effective_tools is not None else TOOL_DEFINITIONS
        effective_tools = [t for t in base if t["name"] != "spawn_agent"]

    _subagent_tokens: list[int] = []
    if enable_agents and agent_depth < MAX_AGENT_DEPTH and agent_registry is not None:
        from .agents import SUBAGENT_GUIDANCE
        from .agents.runner import run_agent
        _agent_runner = lambda task, role: run_agent(  # noqa: E731
            task, role, agent_registry, client,
            parent_depth=agent_depth, mcp_manager=mcp_manager,
            _token_accumulator=_subagent_tokens,
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
        effective_tools = [t for t in base_et if t["name"] != "send_remote_task"]

    executor = ToolExecutor(
        dry_run=dry_run, mcp_manager=mcp_manager,
        agent_runner=_agent_runner, agent_label=agent_label,
        remote_task_runner=_remote_task_runner,
        confirm_callback=confirm_callback,
    )
    prompt = _resolve_mentions(prompt, Path.cwd())
    conversation.add_user(prompt)
    final_usage: Optional[LLMResponse] = None
    side_effects_occurred = False

    for _ in range(limit):
        result = await _stream_one_iteration_async(
            client, conversation, system_prompt, tools=effective_tools,
            silent=render_markdown,
            flush_narration=render_markdown,
            spinner_label=spinner_label,
            agent_label=agent_label,
        )
        if result is None:
            return None

        if result.usage:
            final_usage = result.usage

        if result.stop_reason not in ("end_turn", "tool_use"):
            conversation.add_assistant(result.full_text, result.usage)
            if _get_slot_cb() is None:
                console.print(f"\n[muted]  ↳ stopped: {result.stop_reason}[/]")
            if capture_output:
                return result.full_text
            break

        if result.stop_reason == "end_turn":
            conversation.add_assistant(result.full_text, result.usage)
            if capture_output:
                return result.full_text
            if render_markdown and result.full_text:
                from rich.markdown import Markdown
                from rich.panel import Panel
                console.print(
                    Panel(
                        Markdown(result.full_text),
                        title=f"[bold {YELLOW}]{markdown_title or 'Response'}[/]",
                        expand=False,
                        border_style="dim",
                    )
                )
            if reflect_config and reflect_config.depth > 0:
                if side_effects_occurred:
                    console.print("[muted]  ↳ reflection skipped (side-effecting tools were used)[/]")
                else:
                    _run_reflection(
                        prompt=prompt,
                        response=result.full_text,
                        client=client,
                        config=reflect_config,
                        verbose=verbose,
                        conversation=conversation,
                    )
            break

        if result.stop_reason == "tool_use":
            conversation.add_assistant_blocks(_build_content_blocks(result), result.usage)

            if dry_run:
                for tb in result.tool_blocks:
                    print_tool_call(tb.name, tb.input, dry_run=True)
                console.print(f"\n[muted]Dry-run complete. {len(result.tool_blocks)} tool call(s) shown.[/]")
                break

            for tb in result.tool_blocks:
                if tb.name in SIDE_EFFECTING_TOOLS or "__" in tb.name or tb.name in DELEGATION_TOOLS:
                    side_effects_occurred = True

            await _execute_tools_async(result.tool_blocks, executor, conversation)
            if _get_slot_cb() is None:
                print()

    else:
        print_iteration_limit(limit)

    if not capture_output:
        system_prompt_tokens = len(system_prompt) // 4 - memory_tokens
        if final_usage:
            conversation.truncate_if_needed(final_usage.input_tokens, final_usage.output_tokens)
        snapshot = conversation.build_snapshot(final_usage, system_prompt_tokens, memory_tokens)
        print_usage(snapshot)
        if _subagent_tokens:
            total_sub = sum(_subagent_tokens)
            n_sub = len(_subagent_tokens)
            console.print(
                f"  [muted]subagents: {n_sub} agent{'s' if n_sub > 1 else ''}, "
                f"{total_sub:,} tokens total[/]"
            )
    return None
