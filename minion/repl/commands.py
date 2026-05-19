"""Slash command dispatcher and supporting helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from ..config import MINION_STYLE, run_model_config
from ..memory.injection import _format_age
from ..memory.record import MemoryRecord
from ..runner.session import list_sessions, load, save
from ..theme import BLUE, SILVER, YELLOW, console, print_context, print_error
from ..tracing import get_tracer
from .state import CommandContext, REPL_COMMANDS, ReplState


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_last_response_text(conversation) -> Optional[str]:
    """Extract plain text from the last assistant message in conversation."""
    from ..llm.base import ContentTextBlock
    if not conversation.messages:
        return None
    last = conversation.messages[-1]
    if last.role != "assistant":
        return None
    if isinstance(last.content, str):
        return last.content
    parts = [b.text for b in last.content if isinstance(b, ContentTextBlock)]
    return "\n".join(parts) if parts else None


def _load_session(name: str, conversation) -> None:
    """Load a named session into conversation in-place."""
    try:
        loaded = load(name)
        conversation.messages = loaded.messages
        conversation.total_tokens = loaded.total_tokens
        conversation._model = loaded._model
        msg_count = len(loaded.messages)
        console.print(
            f"[{YELLOW}]Loaded session[/] [{BLUE}]{name}[/] "
            f"[muted]({msg_count} messages, {loaded.total_tokens:,} tokens)[/]"
        )
    except FileNotFoundError as e:
        print_error(str(e))


def _maybe_create_project_config(cwd: Path) -> None:
    """Create .minion/config.toml from root config values if it doesn't exist yet."""
    if (cwd / ".minion" / "config.toml").exists():
        return
    from ..config import create_project_config, load_config_levels
    global_raw, _ = load_config_levels()
    path = create_project_config(cwd, global_raw)
    console.print(f"[{YELLOW}]Created[/] [muted]{path}[/]")


def _display_plan(content: str, path: Path) -> None:
    """Render a plan document in a Rich panel with Markdown formatting."""
    from rich.markdown import Markdown
    from rich.panel import Panel

    console.print(
        Panel(
            Markdown(content),
            title=f"[bold {YELLOW}]Mission Plan[/]",
            subtitle=f"[muted]{path}[/]",
            expand=False,
            border_style="dim",
        )
    )


# ─── Main dispatcher ──────────────────────────────────────────────────────────

def _handle_slash_command(raw: str, ctx: CommandContext) -> bool:
    """Dispatch a slash command. Returns True if the input was handled.

    ctx.state is optional; when None, toggle commands (/reflect, /verbose, etc.)
    return True but silently do nothing — preserves backward compat with tests.
    """
    parts = raw.strip().split(maxsplit=1)
    if not parts:
        return False
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    client       = ctx.client
    conversation = ctx.conversation
    state        = ctx.state
    memory_store = ctx.memory_store
    project_context = ctx.project_context
    skill_registry  = ctx.skill_registry
    agent_registry  = ctx.agent_registry
    cwd             = ctx.cwd
    permission_store = ctx.permission_store
    hook_runner     = ctx.hook_runner

    if cmd == "/remote":
        from .agent_handlers import _handle_remote_command
        _handle_remote_command(raw, ctx.a2a_manager)
        return True

    if cmd in ("/quit", "/exit"):
        console.print(f"[{YELLOW}]Poopaye! (Goodbye!) 👋[/]")
        from rich.rule import Rule
        console.print(Rule(style=SILVER))
        raise typer.Exit()

    if cmd == "/help":
        console.print(f"\n[bold {YELLOW}]Available commands:[/]")
        for command, description in REPL_COMMANDS.items():
            console.print(f"  [{BLUE}]{command:<10}[/]  {description}")
        console.print()
        return True

    if cmd == "/init":
        result = _handle_init(arg, client, state, project_context)
        if result and cwd:
            _maybe_create_project_config(cwd)
        return result


    if cmd == "/hooks":
        if hook_runner is None:
            return True
        sub = arg.strip().lower()
        if sub in ("", "list"):
            from rich.table import Table
            rows = hook_runner.describe()
            if not rows:
                console.print(f"[{YELLOW}]Hooks:[/] none registered")
                return True
            tbl = Table(show_header=True, header_style="bold", expand=False, box=None)
            tbl.add_column("Name", style=YELLOW)
            tbl.add_column("Type", style="dim")
            tbl.add_column("Event")
            tbl.add_column("Tool")
            tbl.add_column("Source", style="dim")
            tbl.add_column("Detail")
            for r in rows:
                tbl.add_row(
                    r.get("name", ""),
                    r["type"],
                    r["event"],
                    r["tool"],
                    r.get("source", ""),
                    r["detail"],
                )
            status = "on" if hook_runner.enabled else "off"
            console.print(f"[{YELLOW}]Hooks:[/] {status} · {hook_runner.handler_count} registered")
            console.print(tbl)
        elif sub == "off":
            hook_runner.disable()
            console.print(f"[{YELLOW}]Hooks:[/] disabled for this session")
        elif sub == "on":
            hook_runner.enable()
            console.print(f"[{YELLOW}]Hooks:[/] enabled")
        else:
            print_error("Usage: /hooks [list | on | off]")
        return True

    if cmd == "/agents":
        if state is not None:
            status = "on" if state.agents_enabled else "off"
            console.print(f"[{YELLOW}]Subagents:[/] {status}")
            if agent_registry:
                from rich.table import Table
                table = Table(show_header=True, header_style="bold", expand=False, box=None)
                table.add_column("role", style=YELLOW)
                table.add_column("description")
                table.add_column("tools", style="muted")
                for name, role in sorted(agent_registry.items()):
                    tools_str = ", ".join(role.tools) if role.tools else "all"
                    table.add_row(name, role.description, tools_str)
                console.print(table)
            else:
                console.print("[muted]No agent roles loaded.[/]")
        return True

    if cmd == "/remember":
        if not arg:
            print_error("Usage: /remember [--global] [--category identity|preference|project|event] <text>")
            return True
        if memory_store is not None:
            tokens = arg.split()
            scope = "project"
            project_path = str(Path.cwd())
            category = "project"
            while tokens:
                if tokens[0] == "--global":
                    scope = "global"
                    project_path = None  # type: ignore[assignment]
                    tokens.pop(0)
                elif tokens[0] == "--category" and len(tokens) > 1:
                    cat = tokens[1].lower()
                    if cat not in ("identity", "preference", "project", "event"):
                        print_error("--category must be one of: identity, preference, project, event")
                        return True
                    category = cat
                    tokens.pop(0)
                    tokens.pop(0)
                else:
                    break
            content = " ".join(tokens).strip("\"'")
            if not content:
                print_error("Usage: /remember [--global] [--category identity|preference|project|event] <text>")
                return True
            record = MemoryRecord(
                id=str(uuid.uuid4()),
                content=content,
                type="semantic",
                scope=scope,
                project_path=project_path,
                tags=[],
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                superseded_by=None,
                category=category,
            )
            memory_store.store(record)
            console.print(f"[{YELLOW}]Remembered[/] [muted]({scope}·{category}):[/] {content}")
        else:
            console.print(f"[muted]Memory not available in this session.[/]")
        return True

    if cmd == "/forget":
        if not arg:
            print_error("Usage: /forget <id or text>")
            return True
        if memory_store is not None:
            count = memory_store.delete(arg.strip("\"'"))
            if count:
                console.print(f"[{YELLOW}]Forgotten.[/] [muted]({count} memory removed)[/]")
            else:
                console.print(f"[muted]No matching memory found.[/]")
        else:
            console.print(f"[muted]Memory not available in this session.[/]")
        return True

    if cmd == "/memories":
        if memory_store is not None:
            s = memory_store.stats()
            status = "on" if (state is not None and state.memory_enabled) else "off"
            embeddings = "embeddings on" if s["has_embeddings"] else "keyword search only"
            console.print(
                f"[{YELLOW}]Memory:[/] {status} · "
                f"[bold]{s['global_count']}[/] global, [bold]{s['project_count']}[/] project · "
                f"[muted]{embeddings}[/]"
            )
            memories = memory_store.list_all(query=arg or None)
            if memories:
                console.print()
                for m in memories:
                    age = _format_age(m.created_at)
                    console.print(
                        f"  [{BLUE}]{m.id[:8]}[/] [{m.category}·{m.scope}] "
                        f"{m.content} [muted]({age})[/]"
                    )
        else:
            console.print(f"[muted]Memory not available in this session.[/]")
        return True

    if cmd == "/config":
        from .config_cmd import handle_config_command
        return handle_config_command(raw, ctx)

    if cmd == "/model":
        run_model_config(client)
        return True

    if cmd == "/setup":
        run_model_config(client)
        try:
            import shellingham
            shell_name, _ = shellingham.detect_shell()
            import questionary
            do_install = questionary.confirm(
                f" Install tab completion for {shell_name}?",
                default=True,
                style=MINION_STYLE,
            ).ask()
            if do_install:
                from typer._completion_shared import install as _ti
                _, comp_path = _ti(shell=shell_name, prog_name="minion")
                console.print(f"[#4CAF50]✓ tab completion installed[/]  [muted]→ {comp_path}[/]")
                console.print(f"[muted]Restart your terminal to activate.[/]")
        except Exception:
            pass
        return True

    if cmd == "/context":
        print_context(conversation.context_display())
        return True

    if cmd == "/compact":
        from ..compact import DEFAULT_STRATEGY, STRATEGIES, get_strategy
        if not conversation.messages:
            console.print(f"[muted]Nothing to compact — conversation is empty.[/]")
            return True
        strategy_name = DEFAULT_STRATEGY
        kwargs: dict = {}
        if arg:
            parts = arg.split()
            strategy_name = parts[0].lower()
            if strategy_name not in STRATEGIES:
                available = ", ".join(STRATEGIES)
                console.print(f"[muted]Unknown strategy '{strategy_name}'. Available: {available}[/]")
                return True
            if strategy_name == "truncate" and len(parts) > 1:
                try:
                    kwargs["keep_turns"] = int(parts[1])
                except ValueError:
                    console.print(f"[muted]Usage: /compact truncate [N turns to keep][/]")
                    return True
        strategy = get_strategy(strategy_name, **kwargs)
        msg_count = len(conversation.messages)
        console.print(f"[{YELLOW}]Compacting[/] [muted]({msg_count} messages · strategy: {strategy_name})[/]")
        with console.status(f"[muted]compacting...[/]", spinner="dots"):
            result = strategy.compact(conversation, client, ctx.base_system_prompt)
        saved = result.tokens_estimate_before - result.tokens_estimate_after
        console.print(
            f"[{YELLOW}]Compacted.[/] [muted]"
            f"{result.messages_before} → {result.messages_after} messages · "
            f"~{result.tokens_estimate_before:,} → ~{result.tokens_estimate_after:,} tokens "
            f"(saved ~{saved:,})[/]"
        )
        return True

    if cmd == "/clear":
        conversation.messages.clear()
        conversation._snapshot = None
        console.print(f"[{YELLOW}]Conversation cleared.[/]")
        return True

    if cmd == "/save":
        if not arg:
            print_error("Usage: /save <name>")
            return True
        path = save(conversation, arg)
        console.print(f"[{YELLOW}]Session saved to[/] [{BLUE}]{path}[/]")
        return True

    if cmd == "/load":
        if not arg:
            sessions = list_sessions()
            if sessions:
                console.print(f"[{YELLOW}]Available sessions:[/] {', '.join(sessions)}")
            else:
                console.print(f"[muted]No saved sessions found.[/]")
            print_error("Usage: /load <name>")
            return True
        _load_session(arg, conversation)
        return True

    if cmd == "/resume":
        sessions = list_sessions()
        if not sessions:
            console.print(f"[muted]No saved sessions found.[/]")
            return True
        import questionary
        name = questionary.select(" Select a session:", choices=sessions, pointer="  ❯ ", style=MINION_STYLE).ask()
        if name:
            _load_session(name, conversation)
        return True

    if cmd == "/plan":
        return _handle_plan(arg, ctx)

    if cmd == "/skills":
        if skill_registry:
            for name, skill in skill_registry.items():
                console.print(f"  [bold {BLUE}]/{name:<14}[/] [{skill.source}] {skill.description}")
        else:
            console.print("[muted]No skills loaded.[/]")
        return True

    # Skill dispatch — check registry before falling through to unknown-command
    if skill_registry:
        skill = skill_registry.get(cmd[1:])
        if skill is not None:
            from ..skills.runner import execute_skill
            execute_skill(skill, arg, client, conversation, ctx.base_system_prompt, skill_registry, state,
                          confirmation_manager=ctx.confirmation_manager, hook_runner=ctx.hook_runner,
                          permission_store=ctx.permission_store, approval_mode=state.approval_mode if state else "off")
            return True

    if cmd.startswith("/"):
        console.print(
            f"[muted]Unknown command '{cmd}'. "
            f"Type [bold]/help[/bold] for available commands.[/]"
        )
        return True

    return False


# ─── Sub-handlers for complex commands ────────────────────────────────────────

def _handle_init(arg: str, client, state, project_context) -> bool:
    """Handle the /init command — generate or regenerate MINION.md."""
    from ..context.project import ProjectContext as _PC
    from ..context.prompts import build_system_prompt
    from ..context import build_project_context
    from .init_md import _generate_minion_md, _generate_minion_md_llm

    minion_md_path = Path.cwd() / "MINION.md"
    is_regen = minion_md_path.exists()
    if is_regen:
        import questionary
        from ..config import _MINION_STYLE
        regenerate = questionary.confirm(
            "MINION.md already exists. Regenerate it from the current codebase?",
            default=False,
            style=_MINION_STYLE,
        ).ask()
        if not regenerate:
            return True

    minion_dir = Path.cwd() / ".minion"
    minion_dir.mkdir(exist_ok=True)

    content = None
    was_streamed = False
    # _CaptureBuf (TUI slash-command capture) marks itself so we skip Live/status
    # rendering — Live.update() writes every intermediate frame to the buffer
    # which can't erase previous frames, producing duplicate panels.
    _capture_mode = getattr(console._file, "is_capture_buf", False)
    if project_context:
        fresh_context = _PC(
            cwd=project_context.cwd,
            manifest=project_context.manifest,
            file_tree=project_context.file_tree,
            minion_md=None,
        )
        try:
            from rich.live import Live
            from rich.markdown import Markdown as _MD
            from rich.panel import Panel
            _panel = lambda text: Panel(
                _MD(text),
                title=f"[bold {YELLOW}]MINION.md[/]",
                subtitle=f"[muted]{minion_md_path}[/]",
                expand=False,
                border_style="dim",
            )
            gen = _generate_minion_md_llm(fresh_context, client)
            if _capture_mode:
                # Collect all chunks silently; render once at the end
                chunks = list(gen)
            else:
                with console.status(f"[muted]Generating MINION.md...[/]", spinner="dots"):
                    first_chunk = next(gen, None)
                chunks = []
                if first_chunk is not None:
                    chunks.append(first_chunk)
                    with Live(_panel(first_chunk), console=console, refresh_per_second=12,
                              vertical_overflow="visible") as live:
                        for chunk in gen:
                            chunks.append(chunk)
                            live.update(_panel("".join(chunks)))
            _generated = "".join(chunks).strip()
            content = _generated + "\n" if _generated else None
            was_streamed = not _capture_mode
        except Exception as e:
            console.print(f"[muted]LLM generation failed: {e}[/]")

    if content is None:
        if project_context:
            console.print(f"[muted]Using static template.[/]")
        content = _generate_minion_md(project_context)

    minion_md_path.write_text(content, encoding="utf-8")

    if state is not None:
        new_context = build_project_context(Path.cwd())
        state.system_prompt = build_system_prompt(new_context)

    action = "Regenerated" if is_regen else "Created"
    if not was_streamed:
        from rich.markdown import Markdown
        from rich.panel import Panel
        console.print(Panel(
            Markdown(content),
            title=f"[bold {YELLOW}]MINION.md[/]",
            subtitle=f"[muted]{minion_md_path}[/]",
            expand=False,
            border_style="dim",
        ))
    console.print()
    console.print(f"[{YELLOW}]{action} MINION.md[/] [muted]in {Path.cwd()}[/]")
    console.print(f"[muted]Edit MINION.md to refine — changes take effect in this session immediately.[/]")
    return True


def _handle_plan(arg: str, ctx: CommandContext) -> bool:
    """Handle the /plan command family."""
    from ..planner import PlanResult, create_plan, execute_plan
    from ..planner.creator import _refine_plan
    from ..planner.storage import list_plans, plans_dir

    client = ctx.client
    conversation = ctx.conversation
    state = ctx.state
    project_context = ctx.project_context
    permission_store = ctx.permission_store

    if not arg:
        if state and state.active_plan:
            console.print(f"[{YELLOW}]Active plan:[/] {state.active_plan}")
            console.print(f"[muted]Goal: {state.active_plan_goal or '(unknown)'}[/]")
            console.print(f"[muted]Use /plan --execute to run · /plan --clear to discard.[/]")
        else:
            console.print(f"[muted]No active plan. Use /plan <goal> to create one.[/]")
        return True

    if arg.lower() == "--clear":
        if state:
            state.active_plan = None
            state.active_plan_goal = None
        console.print(f"[muted]Plan cleared.[/]")
        return True

    if arg.lower() == "--list":
        plans = list_plans()
        if not plans:
            console.print(f"[muted]No saved plans in {plans_dir()}[/]")
        else:
            console.print(f"[{YELLOW}]Saved plans:[/]")
            for p in plans:
                size_kb = p.stat().st_size / 1024
                console.print(f"  [{BLUE}]{p.name}[/] [muted]({size_kb:.1f} KB)[/]")
        return True

    if arg.lower() == "--execute" or arg.lower().startswith("--execute "):
        filename = arg[9:].strip()
        if filename:
            plan_path = plans_dir() / filename
            if not plan_path.exists():
                print_error(f"Plan not found: {plan_path}")
                return True
            plan_goal = filename
        elif state and state.active_plan:
            plan_path = state.active_plan
            plan_goal = state.active_plan_goal or str(plan_path.stem)
        else:
            print_error("No active plan. Use /plan <goal> to create one first.")
            return True
        console.print()
        execute_plan(plan_path, client, conversation, ctx.base_system_prompt, state or ReplState(), permission_store=permission_store)
        return True

    # /plan <goal> — create a new plan
    goal = arg
    console.print()
    recent = [m for m in conversation.messages if isinstance(m.content, str)][-8:]
    result = create_plan(goal, client, project_context, recent_messages=recent or None)
    if result is None:
        return True

    if state:
        state.active_plan = result.path
        state.active_plan_goal = goal

    _display_plan(result.content, result.path)

    import questionary

    _PLAN_CHOICES = ["Execute plan", "Refine plan", "Save without executing"]
    refinement_round = 0
    while True:
        try:
            choice = questionary.select(
                " What would you like to do?",
                choices=_PLAN_CHOICES,
                pointer="  ❯ ",
                style=MINION_STYLE,
            ).ask()
        except (KeyboardInterrupt, EOFError):
            choice = None

        if choice is None or choice == "Save without executing":
            console.print(f"[muted]Plan saved at {result.path}. Use /plan --execute to run later.[/]")
            break

        if choice == "Execute plan":
            console.print()
            execute_plan(result.path, client, conversation, ctx.base_system_prompt, state or ReplState(), permission_store=permission_store)
            break

        try:
            feedback = console.input(f"[bold {YELLOW}]feedback[/] › ")
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[muted]Plan saved. Use /plan --execute to run later.[/]")
            break
        feedback = feedback.strip()
        if not feedback:
            continue
        console.print()
        refinement_round += 1
        revised = _refine_plan(result.content, feedback, goal, client)
        if revised:
            from ..planner.storage import save_plan as _save_plan
            _save_plan(revised, goal)
            result = PlanResult(path=result.path, content=revised, goal=goal)
            _display_plan(revised, result.path)
            get_tracer().emit(
                "plan_refined",
                plan_path=str(result.path),
                feedback=feedback,
                refinement_round=refinement_round,
            )
    return True
