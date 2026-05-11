"""Session bootstrap, TUI loop, and console loop.

run_repl_async() is the main async entry point:
  1. Builds project context and system prompt
  2. Initialises memory, permissions, hooks, and registries
  3. Prints greeting, then routes to TUI or console REPL path
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..context import build_project_context
from ..context.prompts import build_system_prompt
from ..llm.conversation import Conversation
from ..llm.reflection import ReflectionConfig
from ..memory.config import MemoryConfig
from ..memory.embedder import build_embedder
from ..memory.injection import inject_memories
from ..memory.store import MemoryStore
from ..output import ConsoleRenderer, TuiRenderer
from ..runner import run_prompt_async
from ..theme import SILVER, YELLOW, console
from ..tracing import get_tracer
from .agent_handlers import _handle_agent_direct, _handle_remote_command
from .commands import _get_last_response_text, _handle_slash_command
from .input import _CaptureBuf, _INPUT_STYLE, _InputLexer, _SlashCompleter, _kb
from .mcp import _extract_mcp_text, _handle_mcp_command, _inject_mcp_message
from .state import REPL_COMMANDS, CommandContext, ReplState

if TYPE_CHECKING:
    from ..llm.base import LLMClient


# ─── Public entry point ───────────────────────────────────────────────────────

async def run_repl_async(
    client: "LLMClient",
    dry_run: bool = False,
    reflect_depth: int = 0,
    verbose: bool = False,
    memory_enabled: bool = True,
    debug: bool = False,
    agents_enabled: bool = True,
) -> None:
    """Async REPL loop. Call via asyncio.run(run_repl_async(...)).

    Setup sequence:
      1. Build project context (MINION.md, file tree, manifest) and system prompt.
      2. Initialise memory store, permission store, and hook registry from config.
      3. Load skill / agent / A2A / MCP registries.
      4. Print greeting, then bifurcate:
           - TTY + no MINION_NO_TUI → _run_repl_tui() (prompt_toolkit full-screen TUI)
           - Otherwise             → _run_console_loop() (PromptSession)
      In both paths, each user message goes through:
        optional slash-command handling → memory injection → run_prompt_async() → memory extraction
    """
    history_path = Path.home() / ".minion" / "history"
    history_path.parent.mkdir(exist_ok=True)

    project_cwd = Path.cwd()
    project_context = build_project_context(project_cwd)
    base_system_prompt = build_system_prompt(project_context)

    get_tracer().emit(
        "session_start",
        model=getattr(client, "model_id", "unknown"),
        system_prompt=base_system_prompt,
        cwd=str(project_cwd),
    )

    from ..config import load_config as _load_cfg
    from ..memory.triggers import (
        AlwaysTrigger, EveryNTurnsTrigger, ManualOnlyTrigger, SubstantialContentTrigger,
    )
    _file_cfg = _load_cfg(cwd=project_cwd)
    _mcfg = _file_cfg.memory
    _trigger_map = {
        "substantial": SubstantialContentTrigger(min_words=_mcfg.extraction_min_words),
        "every_5":     EveryNTurnsTrigger(n=5),
        "manual":      ManualOnlyTrigger(),
        "always":      AlwaysTrigger(),
    }
    memory_config = MemoryConfig(
        top_k=_mcfg.top_k,
        similarity_threshold=_mcfg.similarity_threshold,
        consolidation_threshold=_mcfg.consolidation_threshold,
        trigger=_trigger_map.get(_mcfg.extraction_trigger, SubstantialContentTrigger()),
    )
    embedder = build_embedder() if memory_enabled else None
    memory_store = MemoryStore(
        config=memory_config,
        project_cwd=project_cwd,
        client=client,
        embedder=embedder,
    )

    from ..tools.permissions import PermissionStore
    permission_store = PermissionStore(project_cwd=project_cwd)

    from ..hooks.registry import HookRegistry
    hook_runner = HookRegistry.from_config(_file_cfg)

    conversation = Conversation(model=client.model_id)
    state = ReplState(
        reflect_depth=reflect_depth,
        verbose=verbose,
        memory_enabled=memory_enabled,
        debug=debug,
        agents_enabled=agents_enabled,
        approval_mode=_file_cfg.agent.approval_mode,
        markdown_enabled=os.getenv("MINION_MARKDOWN", "true").lower() != "false",
        system_prompt=base_system_prompt,
    )

    from ..skills import load_skill_registry
    skill_registry = load_skill_registry()
    REPL_COMMANDS["/skills"] = "List all available skills"
    for _skill_name, _skill in skill_registry.items():
        _cmd_key = f"/{_skill_name}"
        if _cmd_key not in REPL_COMMANDS:
            REPL_COMMANDS[_cmd_key] = _skill.description

    from ..agents import load_agent_registry
    agent_registry = load_agent_registry(project_cwd)
    from ..a2a import load_a2a_manager
    a2a_manager = load_a2a_manager(project_cwd)
    from ..mcp import load_mcp_manager_async
    mcp_manager = await load_mcp_manager_async(project_cwd)
    mcp_manager.set_llm_client(client)
    mcp_count = len(mcp_manager.server_summary())

    from ..theme import print_greeting, print_startup_warnings, startup_warnings
    print_greeting(
        model=client.model_id,
        provider=client.provider_name,
        project_name=project_context.label if project_context.manifest else "",
        cwd=str(project_cwd),
        agent_count=len(agent_registry),
        memory_enabled=state.memory_enabled,
        mcp_count=mcp_count,
        a2a_count=len(a2a_manager.agent_names()) if a2a_manager.has_agents() else 0,
    )
    _all_startup_warnings = startup_warnings[:] + mcp_manager.connection_warnings
    startup_warnings.clear()

    if not (project_cwd / "MINION.md").exists():
        if project_context and project_context.manifest:
            lang = project_context.manifest.language
            if project_context.manifest.framework:
                lang += f" / {project_context.manifest.framework}"
            _tip_body = f"This looks like a {lang} project with no MINION.md."
        elif (project_cwd / ".git").exists():
            _tip_body = "No MINION.md found in this git repository."
        else:
            _tip_body = f"No MINION.md found in {project_cwd.name}/."
        _all_startup_warnings = [
            f"  [{YELLOW}]Tip[/]  {_tip_body} "
            f"Run [bold]/init[/] to analyse your codebase and create one."
        ] + _all_startup_warnings

    print_startup_warnings(_all_startup_warnings)

    from ..hooks.events import SessionStartEvent
    _hook_session_id = get_tracer().session_id or ""
    await hook_runner.fire(SessionStartEvent(session_id=_hook_session_id, cwd=project_cwd))

    from ..tools.confirmation import ConfirmationManager
    confirmation_manager = ConfirmationManager(permission_store=permission_store)

    # Build the shared CommandContext used by both REPL paths
    ctx = CommandContext(
        client=client,
        conversation=conversation,
        state=state,
        project_context=project_context,
        memory_store=memory_store,
        skill_registry=skill_registry,
        agent_registry=agent_registry,
        cwd=project_cwd,
        permission_store=permission_store,
        hook_runner=hook_runner,
    )

    _use_tui = (
        sys.stdout.isatty()
        and os.getenv("MINION_NO_TUI", "").lower() not in ("1", "true")
    )

    if _use_tui:
        from ..tui import MinionApp, set_tui_app
        from minion import __version__ as _minion_ver
        loop = asyncio.get_event_loop()

        while True:
            tui_app = MinionApp(
                model_name=client.model_id,
                agent_registry=agent_registry,
                skill_registry=skill_registry,
                a2a_manager=a2a_manager,
            )
            tui_app.update_session(
                model=client.model_id,
                provider=getattr(client, "provider_name", ""),
                project=project_context.label if project_context and project_context.label else "",
                cwd=str(project_cwd),
                memory=state.memory_enabled,
                agents=len(agent_registry) if agent_registry else 0,
                version=_minion_ver,
                mcp_count=mcp_count,
                a2a_count=len(a2a_manager.agent_names()) if a2a_manager.has_agents() else 0,
            )
            tui_app.set_startup_warnings(_all_startup_warnings)
            confirmation_manager.set_tui(tui_app, loop)
            set_tui_app(tui_app)
            model_config_ran = await _run_repl_tui(
                tui_app=tui_app,
                ctx=ctx,
                mcp_manager=mcp_manager,
                confirmation_manager=confirmation_manager,
                dry_run=dry_run,
                _file_cfg=_file_cfg,
                _hook_session_id=_hook_session_id,
                project_cwd=project_cwd,
            )
            if not model_config_ran:
                break
            from dotenv import load_dotenv
            load_dotenv(Path.home() / ".minion" / ".env", override=True)
            load_dotenv(Path.cwd() / ".minion" / ".env", override=True)
            try:
                from ..llm.factory import get_client as _get_client
                client = _get_client()
            except ValueError:
                pass
        return

    await _run_console_loop(
        ctx=ctx,
        mcp_manager=mcp_manager,
        dry_run=dry_run,
        _file_cfg=_file_cfg,
        _hook_session_id=_hook_session_id,
        project_cwd=project_cwd,
        history_path=history_path,
        agent_registry=agent_registry,
        skill_registry=skill_registry,
        a2a_manager=a2a_manager,
    )


# ─── TUI REPL loop ────────────────────────────────────────────────────────────

async def _run_repl_tui(
    *,
    tui_app,
    ctx: CommandContext,
    mcp_manager,
    confirmation_manager,
    dry_run: bool,
    _file_cfg,
    _hook_session_id: str,
    project_cwd: Path,
) -> bool:
    """TUI REPL loop.

    Returns True if the loop exited to run /model (caller restarts the TUI
    with a refreshed client), False for a normal user-initiated quit.
    """
    import typer
    from ..config import run_model_config
    from ..tui import set_tui_app

    client       = ctx.client
    conversation = ctx.conversation
    state        = ctx.state
    memory_store = ctx.memory_store
    permission_store = ctx.permission_store
    hook_runner  = ctx.hook_runner
    agent_registry  = ctx.agent_registry
    skill_registry  = ctx.skill_registry
    a2a_manager_ref = getattr(ctx, '_a2a_manager', None)

    _renderer = TuiRenderer(tui_app)
    _post_exit_model_config: list[bool] = [False]

    async def on_submit(user_input: str) -> None:
        get_tracer().emit("user_turn", text=user_input)

        from ..hooks.events import UserPromptSubmitEvent
        _prompt_block = await hook_runner.fire_prompt(
            UserPromptSubmitEvent(session_id=_hook_session_id, cwd=project_cwd, prompt=user_input)
        )
        if _prompt_block is not None:
            tui_app.conversation.append_system(
                f"[muted]{_prompt_block.reason or 'Hook blocked this prompt.'}[/]"
            )
            tui_app.conversation.finalize_turn()
            tui_app.set_thinking(False)
            return

        if user_input.startswith("/mcp"):
            mcp_messages = await _handle_mcp_command(user_input, mcp_manager)
            if mcp_messages is None:
                tui_app.set_thinking(False)
                return
            for msg in mcp_messages[:-1]:
                _inject_mcp_message(msg, conversation)
            last = mcp_messages[-1]
            if last.get("role") == "user":
                user_input = _extract_mcp_text(last)
            else:
                _inject_mcp_message(last, conversation)
                tui_app.conversation.append_system(
                    "[muted]Conversation primed with prompt template. Ask a follow-up to continue.[/]"
                )
                tui_app.conversation.finalize_turn()
                tui_app.set_thinking(False)
                return

        if user_input.startswith("/agent "):
            await asyncio.to_thread(_handle_agent_direct, user_input, agent_registry, client)
            tui_app.set_thinking(False)
            return

        if user_input.startswith("/remote"):
            _handle_remote_command(user_input, ctx.agent_registry)
            tui_app.set_thinking(False)
            return

        if user_input.startswith("/") and user_input.strip() == "/model":
            _post_exit_model_config[0] = True
            await tui_app.flush_and_exit()
            return

        if user_input.startswith("/"):
            _buf = _CaptureBuf()
            _exit_requested = [False]

            def _exec_slash():
                _old_file = console._file
                console._file = _buf
                try:
                    _handle_slash_command(user_input, ctx)
                except (SystemExit, typer.Exit):
                    _exit_requested[0] = True
                finally:
                    console._file = _old_file

            await asyncio.to_thread(_exec_slash)

            if _exit_requested[0]:
                await tui_app.flush_and_exit()
                return
            ansi = _buf.getvalue().strip("\n")  # strip blank lines only; preserve indent
            if ansi.strip():
                tui_app.conversation.append_ansi(ansi + "\n")
                tui_app.scroll_to_bottom()
            tui_app.set_thinking(False)
            return

        # Memory injection
        memory_tokens = 0
        system_dynamic = ""
        if state and state.memory_enabled and memory_store is not None:
            memories = await asyncio.to_thread(memory_store.retrieve, user_input)
            mem_block = inject_memories("", memories)
            if mem_block:
                system_dynamic += mem_block
                memory_tokens = len(mem_block) // 4
                get_tracer().emit(
                    "context_inject",
                    memory_count=len(memories),
                    token_estimate=memory_tokens,
                    memories=[m.content for m in memories],
                )

        if state and state.active_plan and state.active_plan.exists():
            goal_hint = state.active_plan_goal or state.active_plan.stem
            system_dynamic += (
                f"\n\n## Recently Executed Plan\n"
                f"Goal: {goal_hint}\n"
                f"Path: {state.active_plan}\n"
                f"Use read_file on this path if it is relevant to the current request."
            )

        reflect_config = (
            ReflectionConfig(depth=state.reflect_depth)
            if state and state.reflect_depth > 0 else None
        )

        try:
            await run_prompt_async(
                user_input, client, conversation,
                state.system_prompt if state else "",
                system_dynamic=system_dynamic,
                dry_run=dry_run,
                reflect_config=reflect_config,
                verbose=state.verbose if state else False,
                memory_tokens=memory_tokens,
                mcp_manager=mcp_manager,
                enable_agents=state.agents_enabled if state else True,
                agent_registry=agent_registry,
                agent_depth=0,
                a2a_manager=a2a_manager_ref,
                auto_compact=_file_cfg.context.auto_compact,
                approval_mode=state.approval_mode if state else "off",
                permission_store=permission_store,
                stream_markdown=state.markdown_enabled if state else True,
                hook_runner=hook_runner,
                confirmation_manager=confirmation_manager,
                renderer=_renderer,
            )
        except Exception as _run_exc:
            tui_app.conversation.append_system(f"Error: {_run_exc}")
        finally:
            tui_app.conversation.finalize_turn()

        for _tip in hook_runner.drain_tips():
            tui_app.conversation.append_system(f"[bold #FFD700]⚡ Hook[/]  [#C0C0C0]{_tip}[/]")

        if state and state.memory_enabled and memory_store is not None:
            try:
                last_response = _get_last_response_text(conversation)
                if last_response:
                    extracted = await asyncio.to_thread(
                        memory_store.maybe_extract, user_input, last_response
                    )
                    if extracted and state.verbose:
                        tui_app.conversation.append_system(
                            f"[#C0C0C0]  ↳ remembered {len(extracted)} fact(s)[/]"
                        )
            except Exception:
                pass

        tui_app.set_thinking(False)
        tui_app.conversation.scroll_to_bottom()
        tui_app.invalidate()

    async def on_quit() -> None:
        if not _post_exit_model_config[0]:
            from ..hooks.events import SessionEndEvent
            await hook_runner.fire(SessionEndEvent(session_id=_hook_session_id, cwd=project_cwd))
            mcp_manager.shutdown()
            get_tracer().finalize()
        set_tui_app(None)

    await tui_app.run_async(on_submit=on_submit, on_quit=on_quit)

    if _post_exit_model_config[0]:
        await asyncio.to_thread(run_model_config, client)

    return _post_exit_model_config[0]


# ─── Console REPL loop ────────────────────────────────────────────────────────

async def _run_console_loop(
    *,
    ctx: CommandContext,
    mcp_manager,
    dry_run: bool,
    _file_cfg,
    _hook_session_id: str,
    project_cwd: Path,
    history_path: Path,
    agent_registry,
    skill_registry,
    a2a_manager,
) -> None:
    """Console (non-TUI) PromptSession REPL loop."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory
    from ..hooks.events import SessionEndEvent, UserPromptSubmitEvent

    client       = ctx.client
    conversation = ctx.conversation
    state        = ctx.state
    memory_store = ctx.memory_store
    permission_store = ctx.permission_store
    hook_runner  = ctx.hook_runner

    _renderer = ConsoleRenderer()
    prompt_session: PromptSession = PromptSession(
        history=FileHistory(str(history_path)),
        completer=_SlashCompleter(
            agent_registry=agent_registry,
            skill_registry=skill_registry,
            a2a_manager=a2a_manager,
        ),
        key_bindings=_kb,
        lexer=_InputLexer(),
        style=_INPUT_STYLE,
        multiline=True,
    )
    you_prompt = FormattedText([("bold #FFD700", "you"), ("", " › ")])

    while True:
        try:
            user_input = await prompt_session.prompt_async(you_prompt)
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{YELLOW}]Poopaye! 👋[/]")
            from rich.rule import Rule
            console.print(Rule(style=SILVER))
            await hook_runner.fire(SessionEndEvent(session_id=_hook_session_id, cwd=project_cwd))
            mcp_manager.shutdown()
            get_tracer().finalize()
            break

        user_input = user_input.strip()
        if not user_input:
            console.print()
            continue

        get_tracer().emit("user_turn", text=user_input)

        _prompt_block = await hook_runner.fire_prompt(
            UserPromptSubmitEvent(session_id=_hook_session_id, cwd=project_cwd, prompt=user_input)
        )
        if _prompt_block is not None:
            console.print(f"\n  [muted]{_prompt_block.reason or 'Hook blocked this prompt.'}[/]")
            console.print()
            continue

        if user_input.startswith("/mcp"):
            mcp_messages = await _handle_mcp_command(user_input, mcp_manager)
            console.print()
            if mcp_messages is None:
                continue
            for msg in mcp_messages[:-1]:
                _inject_mcp_message(msg, conversation)
            last = mcp_messages[-1]
            if last.get("role") == "user":
                user_input = _extract_mcp_text(last)
            else:
                _inject_mcp_message(last, conversation)
                console.print("[muted]Conversation primed with prompt template. Ask a follow-up to continue.[/]")
                continue

        if user_input.startswith("/agent "):
            await asyncio.to_thread(_handle_agent_direct, user_input, agent_registry, client)
            console.print()
            continue

        if user_input.startswith("/remote"):
            _handle_remote_command(user_input, a2a_manager)
            console.print()
            continue

        if await asyncio.to_thread(_handle_slash_command, user_input, ctx):
            console.print()
            continue

        # Memory injection
        memory_tokens = 0
        system_dynamic = ""
        if state and state.memory_enabled and memory_store is not None:
            with console.status("[muted]recalling memories...[/]", spinner="dots"):
                memories = await asyncio.to_thread(memory_store.retrieve, user_input)
            mem_block = inject_memories("", memories)
            if mem_block:
                system_dynamic += mem_block
                memory_tokens = len(mem_block) // 4
                get_tracer().emit(
                    "context_inject",
                    memory_count=len(memories),
                    token_estimate=memory_tokens,
                    memories=[m.content for m in memories],
                )

        if state and state.active_plan and state.active_plan.exists():
            goal_hint = state.active_plan_goal or state.active_plan.stem
            system_dynamic += (
                f"\n\n## Recently Executed Plan\n"
                f"Goal: {goal_hint}\n"
                f"Path: {state.active_plan}\n"
                f"Use read_file on this path if it is relevant to the current request."
            )

        if state and state.debug:
            console.print(f"[muted]── debug: system prompt ───────────────────[/]")
            console.print(f"[muted]{state.system_prompt}[/]")
            if system_dynamic:
                console.print(f"[muted]── debug: dynamic context ──────────────────[/]")
                console.print(f"[muted]{system_dynamic}[/]")
            console.print(f"[muted]────────────────────────────────────────────[/]")

        reflect_config = (
            ReflectionConfig(depth=state.reflect_depth)
            if state and state.reflect_depth > 0 else None
        )
        console.print()
        await run_prompt_async(
            user_input, client, conversation,
            state.system_prompt if state else "",
            system_dynamic=system_dynamic,
            dry_run=dry_run,
            reflect_config=reflect_config,
            verbose=state.verbose if state else False,
            memory_tokens=memory_tokens,
            mcp_manager=mcp_manager,
            enable_agents=state.agents_enabled if state else True,
            agent_registry=agent_registry,
            agent_depth=0,
            a2a_manager=a2a_manager,
            auto_compact=_file_cfg.context.auto_compact,
            approval_mode=state.approval_mode if state else "off",
            permission_store=permission_store,
            stream_markdown=state.markdown_enabled if state else True,
            hook_runner=hook_runner,
            renderer=_renderer,
        )

        for _tip in hook_runner.drain_tips():
            console.print(f"\n  [{YELLOW}]Hook[/]  {_tip}")

        console.print()

        if state and state.memory_enabled and memory_store is not None:
            last_response = _get_last_response_text(conversation)
            if last_response:
                with console.status("[muted]saving memories...[/]", spinner="dots"):
                    extracted = await asyncio.to_thread(
                        memory_store.maybe_extract, user_input, last_response
                    )
                if extracted and state.verbose:
                    console.print(f"[muted]  ↳ remembered {len(extracted)} fact(s)[/]")
                    if state.debug:
                        for r in extracted:
                            tag = f"[{r.category}·{r.type}·{r.scope}]"
                            console.print(f"[muted]     · {tag} {r.content}[/]")
                    console.print()
