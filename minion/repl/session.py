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
from typing import TYPE_CHECKING, Optional, Union

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
from .agent_handlers import _handle_remote_command
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
    first_run: bool = False,
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
    _env_md = os.getenv("MINION_MARKDOWN")
    _markdown = (_env_md.lower() != "false") if _env_md is not None else _file_cfg.agent.markdown_enabled
    state = ReplState(
        reflect_depth=reflect_depth,
        verbose=verbose,
        memory_enabled=memory_enabled,
        debug=debug,
        agents_enabled=agents_enabled,
        approval_mode=_file_cfg.agent.approval_mode,
        markdown_enabled=_markdown,
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
            f"  [bold {YELLOW}]Tip[/]      [#888888]{_tip_body} "
            f"Run [bold]/init[/] to analyse your codebase and create one.[/]"
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
        a2a_manager=a2a_manager,
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
        await _run_repl_tui(
            tui_app=tui_app,
            ctx=ctx,
            mcp_manager=mcp_manager,
            confirmation_manager=confirmation_manager,
            dry_run=dry_run,
            _file_cfg=_file_cfg,
            _hook_session_id=_hook_session_id,
            project_cwd=project_cwd,
            first_run=first_run,
        )
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


# ─── /model diff helper ───────────────────────────────────────────────────────

def _build_model_diff(
    old_provider: str, old_model: str,
    new_provider: str, new_model: str,
) -> list[str]:
    """Return Rich markup lines for the before→after diff card shown after /model saves."""
    from ..config.model_catalog import fmt_ctx, fmt_price, get_model, get_provider as _gp

    old_p = _gp(old_provider)
    new_p = _gp(new_provider)
    old_m = get_model(old_provider, old_model)
    new_m = get_model(new_provider, new_model)

    old_pname = old_p["name"]  if old_p else old_provider
    new_pname = new_p["name"]  if new_p else new_provider
    new_color = new_p["color"] if new_p else "#C0C0C0"

    lines: list[str] = [
        f"[#4CAF50]✓ model updated[/]  [muted]· saved to ~/.minion/.env[/]",
        "",
    ]

    old_color = old_p["color"] if old_p else "#C0C0C0"

    def _row(label: str, old_val: str, new_val: str) -> str:
        if old_val == new_val:
            return f"  [muted]{label:<10}[/]  [#888888]{old_val}[/]"
        return (
            f"  [muted]{label:<10}[/]"
            f"  [#666666]{old_val}[/]"
            f"  [muted]→[/]"
            f"  [bold #FFD700]{new_val}[/]"
        )

    # Provider row — use each provider's brand colour
    if old_pname == new_pname:
        lines.append(f"  [muted]{'provider':<10}[/]  [{old_color}]{old_pname}[/]")
    else:
        lines.append(
            f"  [muted]{'provider':<10}[/]"
            f"  [{old_color}]{old_pname}[/]"
            f"  [muted]→[/]"
            f"  [{new_color}]{new_pname}[/]"
        )

    # Model row — old dim, new gold
    if old_model == new_model:
        lines.append(f"  [muted]{'model':<10}[/]  [#FFD700]{old_model}[/]")
    else:
        lines.append(
            f"  [muted]{'model':<10}[/]"
            f"  [#666666]{old_model}[/]"
            f"  [muted]→[/]"
            f"  [bold #FFD700]{new_model}[/]"
        )

    if old_m or new_m:
        old_ctx   = fmt_ctx(old_m["ctx"])   if old_m else "?"
        new_ctx   = fmt_ctx(new_m["ctx"])   if new_m else "?"
        old_price = f"{fmt_price(old_m['in_price'])}/{fmt_price(old_m['out_price'])}" if old_m else "?"
        new_price = f"{fmt_price(new_m['in_price'])}/{fmt_price(new_m['out_price'])}" if new_m else "?"
        lines.append(_row("context", old_ctx, new_ctx))
        lines.append(_row("pricing", old_price, new_price))

    return lines


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
    first_run: bool = False,
) -> bool:
    """TUI REPL loop. Returns False on normal quit."""
    import typer
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

    # ── First-run onboarding + setup checklist ───────────────────────────────

    def _wire_checklist_callbacks() -> None:
        """Wire the three checklist row callbacks onto tui_app.checklist."""
        _cb_provider = getattr(client, "provider_name", "anthropic")
        _cb_model    = client.model_id

        async def _on_brain_model_result(updates: dict) -> None:
            nonlocal client
            if updates:
                from dotenv import load_dotenv
                from pathlib import Path as _Path
                load_dotenv(_Path.home() / ".minion" / ".env", override=True)
                load_dotenv(_Path.cwd() / ".minion" / ".env", override=True)
                try:
                    from ..llm.factory import get_client as _get_client
                    client = _get_client()
                    ctx.client = client
                    _new_provider = getattr(client, "provider_name", "")
                    _new_model    = client.model_id
                    tui_app.update_session(model=_new_model, provider=_new_provider)
                    _diff = _build_model_diff(_cb_provider, _cb_model, _new_provider, _new_model)
                    for _line in _diff:
                        tui_app.conversation.append_system(_line)
                    tui_app.conversation.finalize_turn()
                    tui_app.checklist.mark_done("brain", f"{_new_provider} · {_new_model}")
                except Exception as _e:
                    tui_app.conversation.append_system(f"[red]Error setting up client:[/] {_e}")
                    tui_app.conversation.finalize_turn()
            else:
                tui_app.conversation.append_system(
                    "[#666666]Brain not configured — press ↵ on row 1 to try again[/]"
                )
                tui_app.conversation.finalize_turn()
            tui_app._refresh_checklist()

        def _on_brain_activate() -> None:
            from ..tui.screens import ModelConfigScreen
            from ..config.model_catalog import has_key as _has_key
            tui_app.push_screen(
                ModelConfigScreen(
                    provider=_cb_provider,
                    model_id=_cb_model,
                    first_run=not _has_key(_cb_provider),
                ),
                _on_brain_model_result,
            )

        async def _on_completion_done(installed: bool) -> None:
            if installed:
                tui_app.checklist.mark_done("completion", "shell completion installed")
                tui_app.conversation.append_system(
                    "[#4CAF50]✓ tab completion installed[/]  "
                    "[#666666]· restart your terminal to activate[/]"
                )
                tui_app.conversation.finalize_turn()
            else:
                tui_app.checklist.mark_done("completion", "skipped")
            tui_app._refresh_checklist()

        def _on_completion_activate() -> None:
            from ..tui.screens import CompletionSetupScreen
            tui_app.push_screen(CompletionSetupScreen(), _on_completion_done)

        async def _do_init() -> None:
            from pathlib import Path as _Path
            minion_md_path = _Path.cwd() / "MINION.md"

            # If MINION.md already exists skip regeneration — questionary.confirm()
            # inside _handle_init would block waiting for terminal input that
            # Textual owns, causing an indefinite hang.
            if minion_md_path.exists():
                tui_app.conversation.append_system(
                    f"[#4CAF50]✓ MINION.md already exists[/]  [#666666]{minion_md_path}[/]"
                )
                tui_app.conversation.finalize_turn()
                tui_app.checklist.mark_done("init", "MINION.md exists")
                tui_app._refresh_checklist()
                return

            # Simulate the user typing /init — full normal path, no custom output.
            tui_app.conversation.append_user("/init")
            tui_app._set_thinking(True)
            await on_submit("/init")

            if minion_md_path.exists():
                tui_app.checklist.mark_done("init", "MINION.md written")
            else:
                tui_app.checklist.mark_done("init", "attempted")
            tui_app._refresh_checklist()

        def _on_init_activate() -> None:
            from ..llm.factory import _PlaceholderClient
            if isinstance(client, _PlaceholderClient):
                tui_app.conversation.append_system(
                    "[#666666]API key not configured — complete[/] "
                    "[bold #FFD700]Pick a brain[/] [#666666]first[/]"
                )
                tui_app.conversation.finalize_turn()
                return
            asyncio.ensure_future(_do_init())

        tui_app.checklist.on_brain      = _on_brain_activate
        tui_app.checklist.on_completion = _on_completion_activate
        tui_app.checklist.on_init       = _on_init_activate

    def _on_first_run() -> None:
        _wire_checklist_callbacks()
        tui_app.show_setup_checklist()

    # ─────────────────────────────────────────────────────────────────────────

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
            parts = user_input.split(None, 2)
            if len(parts) < 3 or not parts[2].strip():
                err = (
                    f"Usage: /agent <role> <task>  (missing task for role '{parts[1]}')"
                    if len(parts) == 2 else "Usage: /agent <role> <task>"
                )
                tui_app.conversation.append_system(f"[red]{err}[/]")
                tui_app.set_thinking(False)
                return
            _role = parts[1]
            _task = parts[2].strip()

            # Slot-based experience — mirrors _execute_parallel_agents_async
            import uuid
            from ..agents.runner import run_agent
            from ..agents.display import set_agent_display_callback
            from ..tui.agent_registry import get_registry as _get_agent_registry
            from ..output.base import SlotSpec

            _slot_id = str(uuid.uuid4())
            _slots = tui_app.slots
            await _slots.pre_register_async([
                SlotSpec(key=_slot_id, tool_name="spawn_agent",
                         inputs={"task": _task, "role": _role}, label=_role)
            ])
            _reg = _get_agent_registry()
            _reg.clear()
            _reg.register(_slot_id, label=_role, task=_task, role=_role)

            _base_cb = _slots.make_callback(_slot_id)
            def _agent_cb(event: str, **data) -> None:
                _base_cb(event, **data)
                _reg.update(_slot_id, event, **data)

            def _run_in_thread() -> str:
                set_agent_display_callback(_agent_cb)
                _agent_cb("running")
                try:
                    return run_agent(
                        _task, _role, agent_registry, client,
                        parent_depth=0,
                        confirmation_manager=confirmation_manager,
                    )
                finally:
                    set_agent_display_callback(None)

            _agent_result = await asyncio.to_thread(_run_in_thread)

            # Flush slot summary to scrollback then clear the live zone
            from ..output.formatter import format_agent_slot_summary
            _states = _slots.slot_results()
            _state = _states[0] if _states else {}
            for _line in format_agent_slot_summary(_role, _task, _state):
                tui_app.conversation.append_system(_line)
            tui_app.invalidate()
            _slots.clear()

            # Feed the subagent result back to minion so it can interpret it
            _handoff = (
                f"The user directly invoked the [{_role}] subagent for the following task: {_task}\n\n"
                f"Subagent output:\n{_agent_result}"
            )
            try:
                await run_prompt_async(
                    _handoff, client, conversation,
                    state.system_prompt if state else "",
                    system_dynamic="",
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

            tui_app.set_thinking(False)
            return

        if user_input.startswith("/") and user_input.strip() == "/model":
            from ..tui.screens import ModelConfigScreen

            _prev_provider = getattr(client, "provider_name", "anthropic")
            _prev_model    = client.model_id

            async def _on_model_result(updates: dict) -> None:
                if not updates:
                    # Discarded — show brief one-liner
                    from ..config.model_catalog import get_model, get_provider as _gp, fmt_ctx, fmt_price
                    _pp = _gp(_prev_provider)
                    _pm = get_model(_prev_provider, _prev_model)
                    _pname = _pp["name"] if _pp else _prev_provider
                    _mctx  = fmt_ctx(_pm["ctx"]) if _pm else "?"
                    _mprc  = f"{fmt_price(_pm['in_price'])}/{fmt_price(_pm['out_price'])}" if _pm else ""
                    tui_app.conversation.append_system(
                        f"[muted]Nothing changed · still on[/] "
                        f"[{_pp['color'] if _pp else '#C0C0C0'}]{_pname}[/] "
                        f"[muted]›[/] [bold #FFD700]{_prev_model}[/]"
                        + (f"  [muted]ctx {_mctx} · {_mprc} per Mtok[/]" if _mprc else "")
                    )
                    tui_app.conversation.finalize_turn()
                    tui_app.set_thinking(False)
                    return

                from dotenv import load_dotenv
                from pathlib import Path as _Path
                load_dotenv(_Path.home() / ".minion" / ".env", override=True)
                load_dotenv(_Path.cwd() / ".minion" / ".env", override=True)
                try:
                    from ..llm.factory import get_client as _get_client
                    nonlocal client
                    client = _get_client()
                    ctx.client = client
                    _new_provider = getattr(client, "provider_name", "")
                    _new_model    = client.model_id
                    tui_app.update_session(
                        model=_new_model,
                        provider=_new_provider,
                    )
                    # Before → after diff card
                    _diff = _build_model_diff(
                        _prev_provider, _prev_model,
                        _new_provider,  _new_model,
                    )
                    for _line in _diff:
                        tui_app.conversation.append_system(_line)
                except Exception as _e:
                    tui_app.conversation.append_system(f"[red]Error reloading client:[/] {_e}")
                tui_app.conversation.finalize_turn()
                tui_app.set_thinking(False)

            tui_app.push_screen(
                ModelConfigScreen(
                    provider=_prev_provider,
                    model_id=_prev_model,
                ),
                _on_model_result,
            )
            return

        if user_input.startswith("/") and user_input.strip() == "/config":
            from ..tui.screens import ConfigPanelScreen

            async def _on_config_done(result: dict) -> None:
                for _attr, _val in result.get("session_changes", {}).items():
                    if state is not None:
                        setattr(state, _attr, _val)
                tui_app.set_thinking(False)

            tui_app.push_screen(
                ConfigPanelScreen(cfg=_file_cfg, cwd=project_cwd),
                _on_config_done,
            )
            return

        if user_input.startswith("/") and user_input.strip() == "/help":
            from ..tui.screens import HelpScreen

            async def _on_help_done(result: Optional[str]) -> None:
                if result:
                    tui_app.prefill_input(result)
                tui_app.set_thinking(False)

            tui_app.push_screen(HelpScreen(skill_registry=skill_registry), _on_help_done)
            return

        if user_input.startswith("/") and user_input.strip().split()[0] == "/memories":
            from ..tui.screens import MemoriesScreen

            async def _on_memories_done(result: None) -> None:
                tui_app.set_thinking(False)

            _mem_query = " ".join(user_input.strip().split()[1:])
            tui_app.push_screen(
                MemoriesScreen(memory_store=memory_store, initial_query=_mem_query),
                _on_memories_done,
            )
            return

        if user_input.startswith("/") and user_input.strip().split()[0] == "/agents":
            from ..tui.screens import AgentsScreen

            async def _on_agents_done(result: "Union[bool, str]") -> None:
                nonlocal agent_registry
                if isinstance(result, str) and result:
                    # Run flow dispatched — submit directly as /agent <name> <task>.
                    # dispatch_input handles thinking state; don't call set_thinking(False).
                    tui_app.dispatch_input(result)
                    return
                if result is True:
                    # Registry changed (delete/duplicate) — reload from disk
                    from ..agents import load_agent_registry as _load_ar
                    agent_registry = _load_ar(project_cwd)
                    ctx.agent_registry = agent_registry
                tui_app.set_thinking(False)

            tui_app.push_screen(
                AgentsScreen(agent_registry=agent_registry, cwd=project_cwd),
                _on_agents_done,
            )
            return

        if user_input.startswith("/") and user_input.strip().split()[0] == "/skills":
            from ..tui.screens import SkillsScreen

            async def _on_skills_done(result: "Union[bool, str]") -> None:
                if isinstance(result, str) and result:
                    tui_app.dispatch_input(result)
                    return
                if result is True:
                    from ..skills.registry import load_skill_registry as _load_sr
                    ctx.skill_registry = _load_sr(project_cwd)
                tui_app.set_thinking(False)

            _active_skill_registry = skill_registry or __import__(
                "minion.skills.registry", fromlist=["load_skill_registry"]
            ).load_skill_registry(project_cwd)
            tui_app.push_screen(
                SkillsScreen(skill_registry=_active_skill_registry, cwd=project_cwd),
                _on_skills_done,
            )
            return

        if user_input.startswith("/") and user_input.strip() == "/setup":
            _wire_checklist_callbacks()
            tui_app.show_setup_checklist()
            tui_app.set_thinking(False)
            return

        if user_input.startswith("/"):
            _buf = _CaptureBuf()
            _exit_requested = [False]

            def _exec_slash():
                _old_file = console._file
                _old_width = console._width
                # Read the real terminal width while the original file is still
                # active (before redirecting to _buf whose fileno() raises).
                # Subtract 6: padding 0 1 (2 cols) + stable scrollbar gutter (1 col)
                # + SetupChecklistZone margin 0 1 (2 cols, applied by Textual to the
                # whole container while visible) + 1 spare.
                _safe_width = max(60, console.width - 6)
                console._file  = _buf
                console._width = _safe_width
                try:
                    _handle_slash_command(user_input, ctx)
                except (SystemExit, typer.Exit):
                    _exit_requested[0] = True
                finally:
                    console._file  = _old_file
                    console._width = _old_width

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
        from ..hooks.events import SessionEndEvent
        await hook_runner.fire(SessionEndEvent(session_id=_hook_session_id, cwd=project_cwd))
        mcp_manager.shutdown()
        get_tracer().finalize()
        set_tui_app(None)

    await tui_app.run_async(
        on_submit=on_submit,
        on_quit=on_quit,
        on_first_run=_on_first_run if first_run else None,
    )
    return False


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
            _parts = user_input.split(None, 2)
            if len(_parts) < 3 or not _parts[2].strip():
                from ..theme import print_error as _pe
                _pe(
                    f"Usage: /agent <role> <task>  (missing task for role '{_parts[1]}')"
                    if len(_parts) == 2 else "Usage: /agent <role> <task>"
                )
                console.print()
                continue
            _role, _task = _parts[1], _parts[2].strip()

            import uuid as _uuid
            from ..agents.runner import run_agent as _run_agent
            from ..agents.display import ParallelDisplay, set_agent_display_callback
            from ..output.base import SlotSpec

            _slot_id = str(_uuid.uuid4())
            _display = ParallelDisplay()
            _display.pre_register([
                SlotSpec(key=_slot_id, tool_name="spawn_agent",
                         inputs={"task": _task, "role": _role}, label=_role)
            ])
            _slot_cb = _display.make_callback(_slot_id)

            def _run_in_thread() -> str:
                set_agent_display_callback(_slot_cb)
                _slot_cb("running")
                try:
                    return _run_agent(_task, _role, agent_registry, client, parent_depth=0)
                finally:
                    set_agent_display_callback(None)

            console.print()
            with _display:
                _display.render_now()
                _agent_result = await asyncio.to_thread(_run_in_thread)
            # ParallelDisplay.__exit__ prints the final done/error state permanently

            # Feed the subagent result back to minion so it can interpret it
            _handoff = (
                f"The user directly invoked the [{_role}] subagent for the following task: {_task}\n\n"
                f"Subagent output:\n{_agent_result}"
            )
            await run_prompt_async(
                _handoff, client, conversation,
                state.system_prompt if state else "",
                system_dynamic="",
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
