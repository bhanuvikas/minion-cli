"""Interactive REPL: session management, slash commands, completion, key bindings.

Single responsibility: own everything about the interactive loop —
how input is read, how slash commands are dispatched, how the session
persists history across restarts.

The actual LLM call is delegated to runner.run_prompt() so this file
stays focused on input/UX concerns.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .config import MINION_STYLE, run_model_config
from .reflection import ReflectionConfig
from .context import ProjectContext, build_project_context
from .conversation import Conversation
from .llm.base import ContentTextBlock, LLMClient
from .memory.config import MemoryConfig
from .memory.embedder import build_embedder
from .memory.injection import _format_age, inject_memories
from .memory.record import MemoryRecord
from .memory.store import MemoryStore
from .prompts import build_system_prompt
from .runner import run_prompt
from .session import list_sessions, load, save
from .theme import BLUE, YELLOW, console, print_context, print_error, print_greeting
from .tracing import get_tracer

# ─── REPL session state ───────────────────────────────────────────────────────

@dataclass
class ReplState:
    """Mutable REPL session state passed through the command loop.

    Follows the same pattern as Conversation — a single instance is created
    at session startup and mutated in place by slash command handlers.
    """
    reflect_depth: int = 0    # 0 = off; N = max self-refine rounds
    verbose: bool = False     # show critique text and diffs when True
    memory_enabled: bool = True  # False = private mode: skip injection + extraction
    debug: bool = False       # print full system prompt before each LLM call
    active_plan: Optional[Path] = None         # path to the current plan file
    active_plan_goal: Optional[str] = None     # goal text stored alongside path


# ─── Slash command registry ───────────────────────────────────────────────────
# Single source of truth for both the /help display and tab-completion.
# Add an entry here to make a new command available everywhere automatically.

REPL_COMMANDS = {
    "/help":    "Show available commands",
    "/init":    "Create a MINION.md template in the current directory",
    "/model":   "Interactively change provider, model, and API keys",
    "/context": "Show context window usage and token breakdown",
    "/reflect": "Self-refine: /reflect on | /reflect 2 | /reflect off | /reflect",
    "/verbose": "Verbose output: /verbose on | /verbose off | /verbose",
    "/debug":   "Debug mode: /debug on | /debug off | /debug",
    "/memory":  "Memory status/toggle: /memory | /memory on | /memory off",
    "/remember": "Remember something: /remember [--global] [--category identity|preference|project|event] <text>",
    "/forget":  "Forget a memory: /forget <id or text>",
    "/recall":  "Show memories: /recall [query]",
    "/clear":   "Clear conversation history and start fresh",
    "/save":    "Save session: /save <name>",
    "/load":    "Load session: /load <name>",
    "/resume":  "Pick a saved session from a dropdown and load it",
    "/plan":    "Plan a task: /plan <goal> | /plan execute [file] | /plan list | /plan clear",
    "/quit":    "Exit Minion",
    "/exit":    "Exit Minion (alias for /quit)",
}


# ─── Tab completion ───────────────────────────────────────────────────────────

class _SlashCompleter(Completer):
    """Completes slash commands from REPL_COMMANDS when input starts with '/'."""

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd, description in REPL_COMMANDS.items():
            if cmd.startswith(text):
                yield Completion(
                    cmd[len(text):],
                    display=cmd,
                    display_meta=description,
                )


# ─── Key bindings ─────────────────────────────────────────────────────────────
# Override Enter so it applies the highlighted completion before submitting,
# rather than submitting the partially-typed text as-is.

_kb = KeyBindings()

@_kb.add("enter")
def _enter_with_completion(event):
    buf = event.app.current_buffer
    state = buf.complete_state
    if state:
        current = state.current_completion
        if current is not None:
            buf.apply_completion(current)
            return  # completion applied — wait for second Enter to submit
        elif len(state.completions) == 1:
            buf.apply_completion(state.completions[0])
            return  # same — let user add arguments before submitting
    buf.validate_and_handle()


# ─── /init template generator ────────────────────────────────────────────────

_INIT_SYSTEM_PROMPT = """\
You are generating a MINION.md file — project instructions for an AI coding assistant called Minion.
Output ONLY the markdown content. No preamble, no explanation, no code fences.
Include these sections: a one-line project summary, ## How to run, ## How to test, \
## Key directories, ## Notes for Minion.
Keep it concise (under 40 lines). Base everything on the project context provided.
Rules:
- Always use relative paths (e.g. src/main.py), never absolute paths.
- In ## How to test: if no test files are detected, say so explicitly rather than guessing.
- If a value is genuinely unknown, write a short placeholder comment."""


def _generate_minion_md_llm(project_context: ProjectContext, client: LLMClient) -> str | None:
    """Generate a project-specific MINION.md via client.complete().

    Returns the generated content string, or None on any failure so the caller
    can fall back to the static template.
    """
    from .llm.base import Message

    messages = [Message(
        role="user",
        content=f"Generate a MINION.md for this project:\n\n{project_context.to_prompt_block()}",
    )]
    try:
        response = client.complete(messages, system=_INIT_SYSTEM_PROMPT)
        content = response.content.strip()
        return content + "\n" if content else None
    except Exception:
        return None


def _generate_minion_md(project_context: ProjectContext | None) -> str:
    """Build a MINION.md starter template from detected project context.

    Pre-fills language/framework/entry point when a manifest was detected;
    falls back to generic placeholders for unrecognised projects.
    """
    manifest = project_context.manifest if project_context else None

    lines: list[str] = ["# MINION.md", ""]

    if manifest:
        label = manifest.language
        if manifest.framework:
            label += f" · {manifest.framework}"
        lines.append(f"Project instructions for Minion. This is a {label} project.")
    else:
        lines.append("Project instructions for Minion. Add anything the agent should know.")

    lines += ["", "## How to run"]
    if manifest and manifest.entry_point:
        lines.append(f"<!-- Entry point detected: {manifest.entry_point} -->")
    else:
        lines.append("<!-- e.g. python src/main.py / npm start / go run . -->")

    lines += [
        "",
        "## How to test",
        "<!-- e.g. pytest tests/ -q / npm test / go test ./... -->",
        "",
        "## Key directories",
        "<!-- Describe important directories and their purpose -->",
        "",
        "## Notes for Minion",
        "<!-- Conventions, things to avoid, important patterns -->",
        "<!-- Tip: create a .minionignore file to exclude paths from minion's file tree -->",
    ]

    return "\n".join(lines) + "\n"


# ─── Slash command handler ────────────────────────────────────────────────────

def _load_session(name: str, conversation: Conversation) -> None:
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


def _handle_slash_command(
    raw: str,
    client: LLMClient,
    conversation: Conversation,
    project_context: ProjectContext | None = None,
    state: ReplState | None = None,
    memory_store: MemoryStore | None = None,
    base_system_prompt: str = "",
) -> bool:
    """Dispatch a slash command. Returns True if the input was handled.

    state is optional for backward compatibility with tests that call this
    function without a ReplState. When state is None, /reflect, /verbose,
    and /memory return True but silently do nothing.
    memory_store is optional; memory commands no-op gracefully when None.
    """
    parts = raw.strip().split(maxsplit=1)
    if not parts:
        return False
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit"):
        console.print(f"[{YELLOW}]Poopaye! (Goodbye!) 👋[/]")
        raise typer.Exit()

    if cmd == "/help":
        console.print(f"\n[bold {YELLOW}]Available commands:[/]")
        for command, description in REPL_COMMANDS.items():
            console.print(f"  [{BLUE}]{command:<10}[/]  {description}")
        console.print()
        return True

    if cmd == "/init":
        minion_md_path = Path.cwd() / "MINION.md"
        if minion_md_path.exists():
            console.print(
                f"[{YELLOW}]MINION.md already exists.[/] "
                f"[muted]Edit it directly or delete it first.[/]"
            )
            return True
        content = None
        llm_attempted = False
        if project_context:
            with console.status(f"[muted]Generating MINION.md...[/]"):
                content = _generate_minion_md_llm(project_context, client)
            llm_attempted = True
        if content is None:
            if llm_attempted:
                console.print(f"[muted]LLM generation failed — using static template.[/]")
            content = _generate_minion_md(project_context)
        minion_md_path.write_text(content, encoding="utf-8")
        console.print(f"[{YELLOW}]Created MINION.md[/] [muted]in {Path.cwd()}[/]")
        console.print(
            f"[muted]MINION.md is for instructions you author: how to run, how to test, "
            f"conventions, things the agent should know.[/]"
        )
        console.print(f"[muted]Edit it to add project instructions, then restart minion to load them.[/]")
        return True

    if cmd == "/reflect":
        if state is not None:
            if not arg:
                status = "off" if state.reflect_depth == 0 else f"on (depth={state.reflect_depth})"
                console.print(f"[{YELLOW}]Reflection:[/] {status}")
            elif arg == "off":
                state.reflect_depth = 0
                console.print(f"[{YELLOW}]Reflection off.[/]")
            elif arg == "on":
                state.reflect_depth = 1
                console.print(f"[{YELLOW}]Reflection on[/] [muted](depth=1)[/]")
            else:
                try:
                    state.reflect_depth = max(0, int(arg))
                    console.print(
                        f"[{YELLOW}]Reflection on[/] [muted](depth={state.reflect_depth})[/]"
                    )
                except ValueError:
                    print_error("Usage: /reflect [on | off | <depth 1-3>]")
        return True

    if cmd == "/verbose":
        if state is not None:
            if not arg:
                status = "on" if state.verbose else "off"
                console.print(f"[{YELLOW}]Verbose:[/] {status}")
            elif arg == "on":
                state.verbose = True
                console.print(f"[{YELLOW}]Verbose on.[/]")
            elif arg == "off":
                state.verbose = False
                console.print(f"[{YELLOW}]Verbose off.[/]")
            else:
                print_error("Usage: /verbose [on | off]")
        return True

    if cmd == "/debug":
        if state is not None:
            if not arg:
                status = "on" if state.debug else "off"
                console.print(f"[{YELLOW}]Debug:[/] {status}")
            elif arg == "on":
                state.debug = True
                console.print(f"[{YELLOW}]Debug on.[/] [muted]System prompt and other debug info will be printed each turn.[/]")
            elif arg == "off":
                state.debug = False
                console.print(f"[{YELLOW}]Debug off.[/]")
            else:
                print_error("Usage: /debug [on | off]")
        return True

    if cmd == "/memory":
        if state is not None:
            if not arg:
                status = "on" if state.memory_enabled else "off"
                if memory_store is not None:
                    s = memory_store.stats()
                    console.print(
                        f"[{YELLOW}]Memory:[/] {status} · "
                        f"{s['global_count']} global, {s['project_count']} project"
                        + (" · embeddings on" if s["has_embeddings"] else " · keyword search only")
                    )
                else:
                    console.print(f"[{YELLOW}]Memory:[/] {status}")
            elif arg == "on":
                state.memory_enabled = True
                console.print(f"[{YELLOW}]Memory on.[/]")
            elif arg == "off":
                state.memory_enabled = False
                console.print(f"[{YELLOW}]Memory off.[/]")
            else:
                print_error("Usage: /memory [on | off]")
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
                    project_path = None
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

    if cmd == "/recall":
        if memory_store is not None:
            memories = memory_store.list_all(query=arg or None)
            if not memories:
                console.print(f"[muted]No memories stored yet.[/]")
            else:
                for m in memories:
                    age = _format_age(m.created_at)
                    console.print(
                        f"  [{BLUE}]{m.id[:8]}[/] [{m.category}·{m.scope}] "
                        f"{m.content} [muted]({age})[/]"
                    )
        else:
            console.print(f"[muted]Memory not available in this session.[/]")
        return True

    if cmd == "/model":
        run_model_config(client)
        return True

    if cmd == "/context":
        print_context(conversation.context_display())
        return True

    if cmd == "/clear":
        conversation.messages.clear()   # reset history only; total_tokens is billing history
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
        name = questionary.select("Select a session:", choices=sessions, style=MINION_STYLE).ask()
        if name:
            _load_session(name, conversation)
        return True

    if cmd == "/plan":
        from .planner import PlanResult, create_plan, execute_plan
        from .planner.creator import _refine_plan
        from .planner.storage import list_plans, plans_dir

        # /plan (bare) — show status
        if not arg:
            if state and state.active_plan:
                console.print(f"[{YELLOW}]Active plan:[/] {state.active_plan}")
                console.print(f"[muted]Goal: {state.active_plan_goal or '(unknown)'}[/]")
                console.print(f"[muted]Use /plan execute to run · /plan clear to discard.[/]")
            else:
                console.print(f"[muted]No active plan. Use /plan <goal> to create one.[/]")
            return True

        # /plan clear
        if arg.lower() == "clear":
            if state:
                state.active_plan = None
                state.active_plan_goal = None
            console.print(f"[muted]Plan cleared.[/]")
            return True

        # /plan list
        if arg.lower() == "list":
            plans = list_plans()
            if not plans:
                console.print(f"[muted]No saved plans in {plans_dir()}[/]")
            else:
                console.print(f"[{YELLOW}]Saved plans:[/]")
                for p in plans:
                    size_kb = p.stat().st_size / 1024
                    console.print(f"  [{BLUE}]{p.name}[/] [muted]({size_kb:.1f} KB)[/]")
            return True

        # /plan execute [filename]
        if arg.lower() == "execute" or arg.lower().startswith("execute "):
            filename = arg[8:].strip()
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
            execute_plan(plan_path, client, conversation, base_system_prompt, state or ReplState())
            return True

        # /plan <goal> — create a new plan
        goal = arg
        console.print()
        # Pass recent plain-text messages as context so the planner is aware
        # of what the user discussed before invoking /plan.
        recent = [m for m in conversation.messages if isinstance(m.content, str)][-8:]
        result = create_plan(goal, client, project_context, recent_messages=recent or None)
        if result is None:
            return True

        if state:
            state.active_plan = result.path
            state.active_plan_goal = goal

        _display_plan(result.content, result.path)

        # ── Refinement loop ───────────────────────────────────────────────────
        import questionary

        _PLAN_CHOICES = ["Execute plan", "Refine plan", "Save without executing"]
        refinement_round = 0
        while True:
            try:
                choice = questionary.select(
                    "What would you like to do?",
                    choices=_PLAN_CHOICES,
                ).ask()
            except (KeyboardInterrupt, EOFError):
                choice = None

            if choice is None or choice == "Save without executing":
                console.print(
                    f"[muted]Plan saved at {result.path}. "
                    f"Use /plan execute to run later.[/]"
                )
                break

            if choice == "Execute plan":
                console.print()
                execute_plan(result.path, client, conversation, base_system_prompt, state or ReplState())
                break

            # "Refine plan" — ask for feedback text
            try:
                feedback = console.input(f"[bold {YELLOW}]feedback[/] › ")
            except (KeyboardInterrupt, EOFError):
                console.print(f"\n[muted]Plan saved. Use /plan execute to run later.[/]")
                break
            feedback = feedback.strip()
            if not feedback:
                continue
            console.print()
            refinement_round += 1
            revised = _refine_plan(result.content, feedback, goal, client)
            if revised:
                from .planner.storage import save_plan as _save_plan
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

    if cmd.startswith("/"):
        console.print(
            f"[muted]Unknown command '{cmd}'. "
            f"Type [bold]/help[/bold] for available commands.[/]"
        )
        return True

    return False


# ─── REPL entry point ─────────────────────────────────────────────────────────

def _get_last_response_text(conversation: Conversation) -> Optional[str]:
    """Extract plain text from the last assistant message in conversation."""
    if not conversation.messages:
        return None
    last = conversation.messages[-1]
    if last.role != "assistant":
        return None
    if isinstance(last.content, str):
        return last.content
    # Handle ContentBlock lists (tool-use turns — extract text blocks only)
    parts = [b.text for b in last.content if isinstance(b, ContentTextBlock)]
    return "\n".join(parts) if parts else None


def run_repl(
    client: LLMClient,
    dry_run: bool = False,
    reflect_depth: int = 0,
    verbose: bool = False,
    memory_enabled: bool = True,
    debug: bool = False,
) -> None:
    """Start the interactive REPL loop."""
    print_greeting()
    console.print(
        f"[muted]Type [bold]/help[/bold] for commands · "
        f"[bold]/quit[/bold] to exit[/]\n"
    )

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

    if project_context.manifest:
        console.print(f"[muted]Project: {project_context.label}[/]\n")
    if project_context.minion_md:
        console.print(f"[muted]MINION.md loaded.[/]\n")

    # ── Memory setup ──────────────────────────────────────────────────────────
    memory_config = MemoryConfig()
    embedder = build_embedder() if memory_enabled else None
    memory_store = MemoryStore(
        config=memory_config,
        project_cwd=project_cwd,
        client=client,
        embedder=embedder,
    )

    conversation = Conversation()
    state = ReplState(
        reflect_depth=reflect_depth,
        verbose=verbose,
        memory_enabled=memory_enabled,
        debug=debug,
    )

    session: PromptSession = PromptSession(
        history=FileHistory(str(history_path)),
        completer=_SlashCompleter(),
        key_bindings=_kb,
    )
    you_prompt = FormattedText([("bold #FFD700", "you"), ("", " › ")])

    while True:
        try:
            user_input = session.prompt(you_prompt)
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{YELLOW}]Poopaye! 👋[/]")
            get_tracer().finalize()
            break

        user_input = user_input.strip()
        if not user_input:
            console.print()
            continue

        get_tracer().emit("user_turn", text=user_input)

        if _handle_slash_command(
            user_input, client, conversation, project_context, state, memory_store,
            base_system_prompt=base_system_prompt,
        ):
            console.print()
            continue

        # ── Memory injection (before LLM call) ────────────────────────────────
        augmented_prompt = base_system_prompt
        memory_tokens = 0
        if state.memory_enabled:
            with console.status("[muted]recalling memories...[/]", spinner="dots"):
                memories = memory_store.retrieve(user_input)
            augmented_prompt = inject_memories(base_system_prompt, memories)
            memory_tokens = (len(augmented_prompt) - len(base_system_prompt)) // 4
            if memories:
                get_tracer().emit(
                    "context_inject",
                    memory_count=len(memories),
                    token_estimate=memory_tokens,
                    memories=[m.content for m in memories],
                )

        # ── Plan reference injection (lightweight — 3 lines pointing to plan file) ──
        if state.active_plan and state.active_plan.exists():
            goal_hint = state.active_plan_goal or state.active_plan.stem
            augmented_prompt += (
                f"\n\n## Recently Executed Plan\n"
                f"Goal: {goal_hint}\n"
                f"Path: {state.active_plan}\n"
                f"Use read_file on this path if it is relevant to the current request."
            )

        if state.debug:
            console.print(f"[muted]── debug: system prompt ───────────────────[/]")
            console.print(f"[muted]{augmented_prompt}[/]")
            console.print(f"[muted]────────────────────────────────────────────[/]")

        reflect_config = (
            ReflectionConfig(depth=state.reflect_depth)
            if state.reflect_depth > 0 else None
        )
        console.print()
        run_prompt(
            user_input, client, conversation, augmented_prompt,
            dry_run=dry_run,
            reflect_config=reflect_config,
            verbose=state.verbose,
            memory_tokens=memory_tokens,
        )
        console.print()

        # ── Memory extraction (after LLM call) ────────────────────────────────
        if state.memory_enabled:
            last_response = _get_last_response_text(conversation)
            if last_response:
                extracted = memory_store.maybe_extract(user_input, last_response)
                if extracted and state.verbose:
                    console.print(
                        f"[muted]  ↳ remembered {len(extracted)} fact(s)[/]"
                    )
                    if state.debug:
                        for r in extracted:
                            tag = f"[{r.category}·{r.type}·{r.scope}]"
                            console.print(f"[muted]     · {tag} {r.content}[/]")
                    console.print()
