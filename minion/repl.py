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
    "/memory":  "Memory status/toggle: /memory | /memory on | /memory off",
    "/remember": "Remember something: /remember <text>",
    "/forget":  "Forget a memory: /forget <id or text>",
    "/recall":  "Show memories: /recall [query]",
    "/clear":   "Clear conversation history and start fresh",
    "/save":    "Save session: /save <name>",
    "/load":    "Load session: /load <name>",
    "/resume":  "Pick a saved session from a dropdown and load it",
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


def _handle_slash_command(
    raw: str,
    client: LLMClient,
    conversation: Conversation,
    project_context: ProjectContext | None = None,
    state: ReplState | None = None,
    memory_store: MemoryStore | None = None,
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
            print_error("Usage: /remember <text>")
            return True
        if memory_store is not None:
            record = MemoryRecord(
                id=str(uuid.uuid4()),
                content=arg,
                type="semantic",
                scope="project",
                project_path=str(Path.cwd()),
                tags=[],
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                superseded_by=None,
            )
            memory_store.store(record)
            console.print(f"[{YELLOW}]Remembered:[/] {arg}")
        else:
            console.print(f"[muted]Memory not available in this session.[/]")
        return True

    if cmd == "/forget":
        if not arg:
            print_error("Usage: /forget <id or text>")
            return True
        if memory_store is not None:
            count = memory_store.delete(arg)
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
                        f"  [{BLUE}]{m.id[:8]}[/] [{m.type}·{m.scope}] "
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
            break

        user_input = user_input.strip()
        if not user_input:
            console.print()
            continue

        if _handle_slash_command(
            user_input, client, conversation, project_context, state, memory_store
        ):
            console.print()
            continue

        # ── Memory injection (before LLM call) ────────────────────────────────
        augmented_prompt = base_system_prompt
        if state.memory_enabled:
            memories = memory_store.retrieve(user_input)
            augmented_prompt = inject_memories(base_system_prompt, memories)

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
        )
        console.print()

        # ── Memory extraction (after LLM call) ────────────────────────────────
        if state.memory_enabled:
            last_response = _get_last_response_text(conversation)
            if last_response:
                extracted = memory_store.maybe_extract(user_input, last_response)
                if extracted and state.verbose:
                    console.print(
                        f"[muted]  ↳ remembered {len(extracted)} fact(s)[/]\n"
                    )
