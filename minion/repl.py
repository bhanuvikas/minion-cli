"""Interactive REPL: session management, slash commands, completion, key bindings.

Single responsibility: own everything about the interactive loop —
how input is read, how slash commands are dispatched, how the session
persists history across restarts.

The actual LLM call is delegated to runner.run_prompt() so this file
stays focused on input/UX concerns.
"""

import asyncio
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .mcp.manager import MCPManager
    from .skills.registry import SkillRegistry

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style

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
from .runner import run_prompt, run_prompt_async
from .session import list_sessions, load, save
from .theme import BLUE, SILVER, YELLOW, console, print_context, print_error, print_greeting, print_mode_toggle, print_startup_warnings, startup_warnings
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
    agents_enabled: bool = True   # False = exclude spawn_agent from tool list
    approval_mode: str = "off"   # "off" | "edits" | "yolo"
    markdown_enabled: bool = True  # render LLM responses as live markdown
    system_prompt: str = ""       # mutable override; updated by /init to hot-reload MINION.md


# ─── Slash command registry ───────────────────────────────────────────────────
# Single source of truth for both the /help display and tab-completion.
# Add an entry here to make a new command available everywhere automatically.

REPL_COMMANDS = {
    "/help":    "Show available commands",
    "/init":    "Create a MINION.md template in the current directory",
    "/model":   "Interactively change provider, model, and API keys",
    "/context": "Show context window usage and token breakdown",
    "/reflect": "Self-refine: /reflect --on | /reflect 2 | /reflect --off | /reflect",
    "/verbose": "Verbose output: /verbose --on | /verbose --off | /verbose",
    "/edits":   "Auto-approve file edits: /edits | /edits on | /edits off",
    "/yolo":    "Auto-approve all tools: /yolo | /yolo on | /yolo off",
    "/debug":   "Debug mode: /debug --on | /debug --off | /debug",
    "/memory":  "Memory status/toggle: /memory | /memory --on | /memory --off",
    "/remember": "Remember something: /remember [--global] [--category identity|preference|project|event] <text>",
    "/forget":  "Forget a memory: /forget <id or text>",
    "/recall":  "Show memories: /recall [query]",
    "/compact": "Compact conversation: /compact | /compact summary | /compact truncate [N]",
    "/clear":   "Clear conversation history and start fresh",
    "/save":    "Save session: /save <name>",
    "/load":    "Load session: /load <name>",
    "/resume":  "Pick a saved session from a dropdown and load it",
    "/plan":    "Plan a task: /plan <goal> | /plan --execute [file] | /plan --list | /plan --clear",
    "/mcp":     "MCP servers: /mcp | /mcp resource <uri> | /mcp prompt <name> | /mcp reload",
    "/markdown": "Markdown rendering: /markdown | /markdown on | /markdown off",
    "/agents":  "Subagents: /agents | /agents on | /agents off",
    "/agent":   "Run a role directly: /agent <role> <task>",
    "/a2a":     "Remote agents: /a2a | /a2a list | /a2a run <agent> <task>",
    "/config":  "Show effective configuration (config.toml + CLI flags)",
    "/quit":    "Exit Minion",
    "/exit":    "Exit Minion (alias for /quit)",
}


# ─── Tab completion ───────────────────────────────────────────────────────────

class _SlashCompleter(Completer):
    """Completes slash commands from REPL_COMMANDS when input starts with '/'.

    When registries are provided, also completes second-argument values for:
        /agent <role>       — role names from agent_registry
        /skill <name>       — skill names from skill_registry
        /a2a run <agent>    — agent names from a2a_manager
    """

    def __init__(self, agent_registry=None, skill_registry=None, a2a_manager=None) -> None:
        self._agent_registry = agent_registry
        self._skill_registry = skill_registry
        self._a2a_manager = a2a_manager

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        parts = text.split()
        # Second-arg completion: "/agent <partial>" or "/skill <partial>" or "/a2a run <partial>"
        if len(parts) >= 2 or (len(parts) == 1 and text.endswith(" ")):
            cmd = parts[0].lower()
            prefix = parts[1] if len(parts) >= 2 else ""

            if cmd == "/agent" and self._agent_registry is not None:
                for name in sorted(self._agent_registry.keys()):
                    if name.startswith(prefix):
                        yield Completion(name[len(prefix):], display=name)
                return

            if cmd == "/skill" and self._skill_registry is not None:
                for name in sorted(self._skill_registry.keys()):
                    if name.startswith(prefix):
                        yield Completion(name[len(prefix):], display=f"/{name}")
                return

            if cmd == "/a2a" and len(parts) >= 2 and parts[1] == "run":
                # "/a2a run <partial>"
                agent_prefix = parts[2] if len(parts) >= 3 else ""
                if self._a2a_manager is not None:
                    for name in sorted(self._a2a_manager.agent_names()):
                        if name.startswith(agent_prefix):
                            yield Completion(name[len(agent_prefix):], display=name)
                return

        # First-arg: complete slash command name
        for cmd, description in REPL_COMMANDS.items():
            if cmd.startswith(text):
                yield Completion(
                    cmd[len(text):],
                    display=cmd,
                    display_meta=description,
                )


# ─── Input syntax highlighting ────────────────────────────────────────────────

_TOKEN_RE = re.compile(r'@[\w./\-]+|/\S+')

class _InputLexer(Lexer):
    """Highlight valid /commands (yellow) and @file mentions (blue) anywhere in the input."""

    def lex_document(self, document):
        lines = document.text.split("\n")

        def get_line(lineno):
            if lineno >= len(lines):
                return []
            line = lines[lineno]
            tokens = []
            cursor = 0
            for m in _TOKEN_RE.finditer(line):
                if m.start() > cursor:
                    tokens.append(('', line[cursor:m.start()]))
                text = m.group()
                if text.startswith('@'):
                    tokens.append(('class:at-mention', text))
                elif text.lower() in REPL_COMMANDS:
                    tokens.append(('class:slash-command', text))
                else:
                    tokens.append(('', text))
                cursor = m.end()
            if cursor < len(line):
                tokens.append(('', line[cursor:]))
            return tokens

        return get_line


_INPUT_STYLE = Style.from_dict({
    'slash-command': f'bold {YELLOW}',   # /command → gold, matches "you ›" prompt
    'at-mention':    f'bold {BLUE}',     # @file.py → blue, matches "minion ›" prefix
})


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


@_kb.add("escape", "enter")
def _insert_newline(event):
    """Option+Enter (Mac) / Alt+Enter inserts a newline for multi-line prompts."""
    event.app.current_buffer.insert_text("\n")


@_kb.add("c-j")
def _paste_newline(event):
    """Ctrl+J (raw LF) arrives as each newline in a pasted block on terminals
    that don't honour bracketed paste. Insert as newline rather than submit so
    the full pasted text accumulates in the buffer before the user hits Enter.
    """
    event.app.current_buffer.insert_text("\n")


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


def _generate_minion_md_llm(project_context: ProjectContext, client: LLMClient):
    """Yield text chunks from LLM generation. Raises on stream error.

    Caller is responsible for collecting chunks and rendering the display.
    """
    from .llm.base import Message, TextChunk

    messages = [Message(
        role="user",
        content=f"Generate a MINION.md for this project:\n\n{project_context.to_prompt_block()}",
    )]
    stream = client.stream(messages, system=_INIT_SYSTEM_PROMPT)
    for event in stream:
        if isinstance(event, TextChunk):
            yield event.text


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
    skill_registry: "SkillRegistry | None" = None,
    agent_registry=None,
    cwd: "Path | None" = None,
    permission_store=None,  # PermissionStore | None
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
        minion_md_path = Path.cwd() / "MINION.md"
        is_regen = minion_md_path.exists()
        if is_regen:
            import questionary
            from .setup_wizard import _MINION_STYLE
            regenerate = questionary.confirm(
                "MINION.md already exists. Regenerate it from the current codebase?",
                default=False,
                style=_MINION_STYLE,
            ).ask()
            if not regenerate:
                return True

        # Ensure .minion/ project dir exists
        minion_dir = Path.cwd() / ".minion"
        minion_dir.mkdir(exist_ok=True)

        content = None
        was_streamed = False
        if project_context:
            # Strip existing minion_md so the LLM analyses the codebase fresh
            from .context.project import ProjectContext as _PC
            fresh_context = _PC(
                cwd=project_context.cwd,
                manifest=project_context.manifest,
                file_tree=project_context.file_tree,
                minion_md=None,
            )
            try:
                from rich.live import Live
                from rich.markdown import Markdown as _MD
                gen = _generate_minion_md_llm(fresh_context, client)
                # Spinner while waiting for first token — erases itself on exit
                with console.status(f"[muted]Generating MINION.md...[/]", spinner="dots"):
                    first_chunk = next(gen, None)
                chunks: list[str] = []
                if first_chunk is not None:
                    chunks.append(first_chunk)
                    with Live(_MD(first_chunk), console=console, refresh_per_second=12,
                              vertical_overflow="visible") as live:
                        for chunk in gen:
                            chunks.append(chunk)
                            live.update(_MD("".join(chunks)))
                raw = "".join(chunks).strip()
                content = raw + "\n" if raw else None
                was_streamed = True
            except Exception as e:
                console.print(f"[muted]LLM generation failed: {e}[/]")

        if content is None:
            if project_context:
                console.print(f"[muted]Using static template.[/]")
            content = _generate_minion_md(project_context)

        minion_md_path.write_text(content, encoding="utf-8")

        # Hot-reload: rebuild system prompt so MINION.md takes effect immediately
        if state is not None:
            new_context = build_project_context(Path.cwd())
            state.system_prompt = build_system_prompt(new_context)

        action = "Regenerated" if is_regen else "Created"
        if not was_streamed:
            # Static fallback — show it since it wasn't streamed live
            console.print()
            from rich.markdown import Markdown
            console.print(Markdown(content))
        console.print()
        console.print(f"[{YELLOW}]{action} MINION.md[/] [muted]in {Path.cwd()}[/]")
        console.print(f"[muted]Edit MINION.md to refine — changes take effect in this session immediately.[/]")
        return True

    if cmd == "/reflect":
        if state is not None:
            if not arg:
                status = "off" if state.reflect_depth == 0 else f"on (depth={state.reflect_depth})"
                console.print(f"[{YELLOW}]Reflection:[/] {status}")
            elif arg == "--off":
                state.reflect_depth = 0
                console.print(f"[{YELLOW}]Reflection off.[/]")
            elif arg == "--on":
                state.reflect_depth = 1
                console.print(f"[{YELLOW}]Reflection on[/] [muted](depth=1)[/]")
            else:
                try:
                    state.reflect_depth = max(0, int(arg))
                    console.print(
                        f"[{YELLOW}]Reflection on[/] [muted](depth={state.reflect_depth})[/]"
                    )
                except ValueError:
                    print_error("Usage: /reflect [--on | --off | <depth 1-3>]")
        return True

    if cmd == "/verbose":
        if state is not None:
            if not arg:
                status = "on" if state.verbose else "off"
                console.print(f"[{YELLOW}]Verbose:[/] {status}")
            elif arg == "--on":
                state.verbose = True
                console.print(f"[{YELLOW}]Verbose on.[/]")
            elif arg == "--off":
                state.verbose = False
                console.print(f"[{YELLOW}]Verbose off.[/]")
            else:
                print_error("Usage: /verbose [--on | --off]")
        return True

    if cmd == "/edits":
        if state is not None:
            if not arg:
                console.print(f"[{YELLOW}]Edits mode:[/] {'on' if state.approval_mode == 'edits' else 'off'}")
            elif arg == "on":
                state.approval_mode = "edits"
                print_mode_toggle("edits", True)
            elif arg == "off":
                state.approval_mode = "off"
                print_mode_toggle("edits", False)
            else:
                print_error("Usage: /edits [on | off]")
        return True

    if cmd == "/yolo":
        if state is not None:
            if not arg:
                console.print(f"[{YELLOW}]Yolo mode:[/] {'on' if state.approval_mode == 'yolo' else 'off'}")
            elif arg == "on":
                state.approval_mode = "yolo"
                print_mode_toggle("yolo", True)
            elif arg == "off":
                state.approval_mode = "off"
                print_mode_toggle("yolo", False)
            else:
                print_error("Usage: /yolo [on | off]")
        return True

    if cmd == "/debug":
        if state is not None:
            if not arg:
                status = "on" if state.debug else "off"
                console.print(f"[{YELLOW}]Debug:[/] {status}")
            elif arg == "--on":
                state.debug = True
                console.print(f"[{YELLOW}]Debug on.[/] [muted]System prompt and other debug info will be printed each turn.[/]")
            elif arg == "--off":
                state.debug = False
                console.print(f"[{YELLOW}]Debug off.[/]")
            else:
                print_error("Usage: /debug [--on | --off]")
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
            elif arg == "--on":
                state.memory_enabled = True
                console.print(f"[{YELLOW}]Memory on.[/]")
            elif arg == "--off":
                state.memory_enabled = False
                console.print(f"[{YELLOW}]Memory off.[/]")
            else:
                print_error("Usage: /memory [--on | --off]")
        return True

    if cmd == "/agents":
        if state is not None:
            if not arg:
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
            elif arg in ("on", "--on"):
                state.agents_enabled = True
                console.print(f"[{YELLOW}]Subagents on.[/]")
            elif arg in ("off", "--off"):
                state.agents_enabled = False
                console.print(f"[{YELLOW}]Subagents off.[/] [muted](spawn_agent removed from tool list)[/]")
            else:
                print_error("Usage: /agents [on | off]")
        return True

    if cmd == "/markdown":
        if state is not None:
            if not arg:
                status = "on" if state.markdown_enabled else "off"
                console.print(f"[{YELLOW}]Markdown rendering:[/] {status}")
            elif arg in ("on", "--on"):
                state.markdown_enabled = True
                console.print(f"[{YELLOW}]Markdown rendering on.[/]")
            elif arg in ("off", "--off"):
                state.markdown_enabled = False
                console.print(f"[{YELLOW}]Markdown rendering off.[/] [muted](plain text streaming)[/]")
            else:
                print_error("Usage: /markdown [on | off]")
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

    if cmd == "/config":
        from .config_file import format_config, load_config as _load_cfg
        cfg = _load_cfg(cwd=cwd)
        console.print(f"\n[bold {YELLOW}]Effective configuration[/] [muted](config.toml + CLI flags):[/]\n")
        console.print(format_config(cfg))
        console.print()
        return True

    if cmd == "/model":
        run_model_config(client)
        return True

    if cmd == "/context":
        print_context(conversation.context_display())
        return True

    if cmd == "/compact":
        from .compact import DEFAULT_STRATEGY, STRATEGIES, get_strategy
        if not conversation.messages:
            console.print(f"[muted]Nothing to compact — conversation is empty.[/]")
            return True

        # Parse strategy name and optional keep_turns for truncate
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
        console.print(
            f"[{YELLOW}]Compacting[/] [muted]({msg_count} messages · strategy: {strategy_name})[/]"
        )
        with console.status(f"[muted]compacting...[/]", spinner="dots"):
            result = strategy.compact(conversation, client, base_system_prompt)
        saved = result.tokens_estimate_before - result.tokens_estimate_after
        console.print(
            f"[{YELLOW}]Compacted.[/] [muted]"
            f"{result.messages_before} → {result.messages_after} messages · "
            f"~{result.tokens_estimate_before:,} → ~{result.tokens_estimate_after:,} tokens "
            f"(saved ~{saved:,})[/]"
        )
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
        name = questionary.select(" Select a session:", choices=sessions, pointer="  ❯ ", style=MINION_STYLE).ask()
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
                console.print(f"[muted]Use /plan --execute to run · /plan --clear to discard.[/]")
            else:
                console.print(f"[muted]No active plan. Use /plan <goal> to create one.[/]")
            return True

        # /plan --clear
        if arg.lower() == "--clear":
            if state:
                state.active_plan = None
                state.active_plan_goal = None
            console.print(f"[muted]Plan cleared.[/]")
            return True

        # /plan --list
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

        # /plan --execute [filename]
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
            execute_plan(plan_path, client, conversation, base_system_prompt, state or ReplState(), permission_store=permission_store)
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
                    " What would you like to do?",
                    choices=_PLAN_CHOICES,
                    pointer="  ❯ ",
                    style=MINION_STYLE,
                ).ask()
            except (KeyboardInterrupt, EOFError):
                choice = None

            if choice is None or choice == "Save without executing":
                console.print(
                    f"[muted]Plan saved at {result.path}. "
                    f"Use /plan --execute to run later.[/]"
                )
                break

            if choice == "Execute plan":
                console.print()
                execute_plan(result.path, client, conversation, base_system_prompt, state or ReplState(), permission_store=permission_store)
                break

            # "Refine plan" — ask for feedback text
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

    if cmd == "/skills":
        if skill_registry:
            for name, skill in skill_registry.items():
                console.print(
                    f"  [bold {BLUE}]/{name:<14}[/] [{skill.source}] {skill.description}"
                )
        else:
            console.print("[muted]No skills loaded.[/]")
        return True

    # Skill dispatch — check registry before falling through to unknown-command
    if skill_registry:
        skill = skill_registry.get(cmd[1:])
        if skill is not None:
            from .skills.runner import execute_skill
            execute_skill(skill, arg, client, conversation, base_system_prompt, skill_registry, state)
            return True

    if cmd.startswith("/"):
        console.print(
            f"[muted]Unknown command '{cmd}'. "
            f"Type [bold]/help[/bold] for available commands.[/]"
        )
        return True

    return False


# ─── /mcp helpers ────────────────────────────────────────────────────────────

def _extract_mcp_text(msg: dict) -> str:
    """Extract plain text from an MCP message dict."""
    content = msg.get("content", {})
    if isinstance(content, dict) and content.get("type") == "text":
        return content.get("text", "")
    if isinstance(content, str):
        return content
    return ""


def _inject_mcp_message(msg: dict, conversation: Conversation) -> None:
    """Inject one MCP message into the conversation at the correct role."""
    text = _extract_mcp_text(msg)
    role = msg.get("role", "user")
    if role == "user":
        conversation.add_user(text)
    elif role == "assistant":
        conversation.add_assistant(text, usage=None)


# ─── /agent direct invocation handler ───────────────────────────────────────

def _handle_agent_direct(raw: str, agent_registry, client: "LLMClient") -> None:
    """Handle '/agent <role> <task>' — run a subagent role directly.

    Parses the role name from the second token, treats the remainder as the
    task, and calls run_agent() to execute it. The result is printed to the
    terminal.
    """
    from .agents.runner import run_agent

    parts = raw.split(None, 2)  # ["/agent", "<role>", "<task>"]
    if len(parts) < 3:
        if len(parts) == 2:
            print_error(f"Usage: /agent <role> <task>  (missing task for role '{parts[1]}')")
        else:
            print_error("Usage: /agent <role> <task>")
        return

    role_name = parts[1]
    task = parts[2].strip()
    if not task:
        print_error("Task cannot be empty.")
        return

    run_agent(task, role_name, agent_registry, client, parent_depth=0)


# ─── /a2a command handler ────────────────────────────────────────────────────

def _handle_a2a_command(raw: str, a2a_manager: "A2AManager | None") -> None:
    """Handle the /a2a slash command family.

    Subcommands:
        /a2a [list]          — list configured remote agents + their capabilities
        /a2a run <agent> <task> — send a task to a named remote agent directly
    """
    from .theme import BLUE, YELLOW, console, print_error

    parts = raw.strip().split(None, 3)
    # parts[0] = "/a2a", parts[1] = subcommand (optional), rest = args
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub in ("", "list", "status"):
        if a2a_manager is None or not a2a_manager.has_agents():
            console.print(
                "[muted]No remote A2A agents configured. "
                "Add agents to ~/.minion/a2a.json or .minion/a2a.json[/]"
            )
            return
        summary = a2a_manager.agent_summary()
        from rich.table import Table
        table = Table(show_header=True, header_style="bold", expand=False, box=None)
        table.add_column("agent", style=YELLOW)
        table.add_column("url")
        table.add_column("description", style="muted")
        for entry in summary:
            table.add_row(entry["name"], entry["url"], entry["card_description"])
        console.print(table)
        return

    if sub == "run":
        # /a2a run <agent> <task>
        if len(parts) < 4:
            if len(parts) == 3:
                print_error(f"Usage: /a2a run <agent> <task>  (missing task for agent '{parts[2]}')")
            else:
                print_error("Usage: /a2a run <agent> <task>")
            return
        agent_name = parts[2]
        task = parts[3].strip()
        if not task:
            print_error("Task cannot be empty.")
            return
        if a2a_manager is None or not a2a_manager.has_agents():
            print_error("No remote A2A agents configured.")
            return
        with console.status(f"[muted]  ⚙  [{agent_name}] running...[/]", spinner="dots"):
            result = a2a_manager.send_task(agent_name, task)
        console.print(result)
        return

    print_error(f"Unknown /a2a subcommand '{sub}'. Usage: /a2a [list | run <agent> <task>]")


# ─── /mcp command handler ────────────────────────────────────────────────────

async def _handle_mcp_command(raw: str, mcp_manager: "MCPManager") -> Optional[list[dict]]:
    """Handle the /mcp slash command family.

    Subcommands:
        /mcp [list|status]           — list servers, tools, resources, prompt templates
        /mcp resource <uri>          — read and display a resource by URI
        /mcp prompt <name> [k=v ...] — get a prompt template and inject it

    Returns None normally. Returns the raw MCP messages list when /mcp prompt
    succeeds — the REPL loop injects prefix messages into conversation history
    and uses the last user-role message as the run_prompt() input.
    """
    parts = raw.split(maxsplit=2)
    sub = parts[1].strip() if len(parts) > 1 else ""

    # ── /mcp resource <uri> ──────────────────────────────────────────────────
    if sub == "resource":
        if len(parts) < 3:
            console.print("[muted]Usage: /mcp resource <uri>  (e.g. /mcp resource notes://ideas)[/]")
            return None
        uri = parts[2].strip()
        content = await mcp_manager.read_resource(uri)
        console.print(f"[bold {YELLOW}]Resource:[/] [bold]{uri}[/]")
        console.print(content)
        return None

    # ── /mcp prompt <name> [key=value ...] ──────────────────────────────────
    if sub == "prompt":
        if len(parts) < 3:
            console.print(
                "[muted]Usage: /mcp prompt <server__name> [key=value ...]\n"
                "       e.g.  /mcp prompt notes__summarize_notes\n"
                "             /mcp prompt notes__find_related topic=AI\n"
                "             /mcp prompt notes__draft_note title=arch context=microkernel design[/]"
            )
            return None
        tokens = parts[2].strip().split()
        namespaced_name = tokens[0]

        # Smarter arg parsing: words after a key= are accumulated as the value
        # until the next key= token. This lets multi-word values work without quotes.
        # e.g.  context=microkernel design  →  {"context": "microkernel design"}
        arguments: dict = {}
        current_key: Optional[str] = None
        current_val_parts: list[str] = []
        for token in tokens[1:]:
            if "=" in token:
                if current_key is not None:
                    arguments[current_key] = " ".join(current_val_parts)
                k, _, v = token.partition("=")
                current_key = k.strip()
                current_val_parts = [v] if v else []
            elif current_key is not None:
                current_val_parts.append(token)
            # tokens before the first key= are silently ignored (shouldn't happen)
        if current_key is not None:
            arguments[current_key] = " ".join(current_val_parts)

        # Collect missing arguments interactively before calling the server.
        # Required args must have a non-empty value; optional args can be skipped with Enter.
        prompt_info = mcp_manager.get_prompt_info(namespaced_name)
        if prompt_info is not None:
            import questionary
            from .config import MINION_STYLE
            for arg in prompt_info.arguments:
                if arg.name in arguments:
                    continue  # already provided on the command line
                desc = f" ({arg.description})" if arg.description else ""
                label = f"  {arg.name}{desc}"
                if arg.required:
                    label += " [required]"
                else:
                    label += " [optional, Enter to skip]"
                value = questionary.text(f"{label}:", style=MINION_STYLE).ask()
                if value is None:  # Ctrl+C
                    console.print("[muted]Cancelled.[/]")
                    return None
                value = value.strip()
                if not value:
                    if arg.required:
                        console.print(f"[red]Required argument '{arg.name}' cannot be empty.[/]")
                        return None
                    # optional — skip it (don't add to arguments dict)
                else:
                    arguments[arg.name] = value

        messages = await mcp_manager.get_prompt(namespaced_name, arguments or None)
        if not messages:
            console.print(f"[muted]Prompt '{namespaced_name}' returned no messages.[/]")
            return None

        # Guard: if the server returned an error message, show it — don't inject into LLM.
        # Error messages come back as a single message whose text starts with "Error".
        if len(messages) == 1 and _extract_mcp_text(messages[0]).startswith("Error"):
            console.print(f"[red]Prompt error:[/] {_extract_mcp_text(messages[0])}")
            return None

        n = len(messages)
        console.print(
            f"[muted]Injecting {n} message{'s' if n > 1 else ''} "
            f"from '{namespaced_name}'…[/]"
        )
        return messages  # REPL loop handles role-aware injection

    # ── /mcp reload ──────────────────────────────────────────────────────────
    if sub == "reload":
        from pathlib import Path as _Path
        console.print(f"[muted]Reloading MCP servers…[/]")
        await mcp_manager.reconnect_all(cwd=_Path.cwd())
        for _warn in mcp_manager.connection_warnings:
            console.print(_warn)
        n = len(list(mcp_manager._states))
        console.print(f"[{YELLOW}]MCP reloaded: {n} server{'s' if n != 1 else ''} connected.[/]")
        return None

    # ── /mcp [list|status] ───────────────────────────────────────────────────
    if sub not in ("", "list", "status"):
        console.print(
            "[muted]Usage:\n"
            "  /mcp [list|status]              — list servers and capabilities\n"
            "  /mcp resource <uri>             — read a resource\n"
            "  /mcp prompt <name> [key=value]  — inject a prompt template\n"
            "  /mcp reload                     — reconnect to all MCP servers[/]"
        )
        return None

    summary = await mcp_manager.server_summary_async()
    if not summary:
        console.print(
            "[muted]No MCP servers connected. "
            "Add servers to ~/.minion/mcp.json or .minion/mcp.json[/]"
        )
        return None

    total_tools = sum(len(s["tools"]) for s in summary)
    total_resources = sum(len(s["resources"]) for s in summary)
    total_prompts = sum(len(s["prompts"]) for s in summary)
    console.print(
        f"[bold {YELLOW}]MCP servers[/] [muted]("
        f"{total_tools} tools, {total_resources} resources, {total_prompts} prompts):[/]"
    )
    for s in summary:
        console.print(
            f"  [bold {BLUE}]{s['name']}[/]  "
            f"[muted]{len(s['tools'])}t · {len(s['resources'])}r · {len(s['prompts'])}p[/]"
        )
        for t in s["tools"]:
            console.print(f"    [muted]tool[/]   {t}")
        for r in s["resources"]:
            label = f" — {r['description']}" if r.get("description") else ""
            console.print(f"    [muted]resource[/] {r['uri']}{label}")
        for p in s["prompts"]:
            args_str = ""
            if p.get("arguments"):
                args_str = "  [muted](" + ", ".join(
                    a["name"] + ("*" if a.get("required") else "") for a in p["arguments"]
                ) + ")[/]"
            console.print(f"    [muted]prompt[/]  {s['name']}__{p['name']}{args_str}")

    return None


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
    agents_enabled: bool = True,
) -> None:
    """Start the interactive REPL loop (thin wrapper — delegates to async implementation)."""
    asyncio.run(run_repl_async(
        client, dry_run=dry_run, reflect_depth=reflect_depth,
        verbose=verbose, memory_enabled=memory_enabled, debug=debug,
        agents_enabled=agents_enabled,
    ))

# ─── Async REPL entry point (Phase 12) ───────────────────────────────────────
# Mirrors run_repl() but uses prompt_async() so the event loop stays live
# between user inputs. Memory retrieval and extraction run in asyncio.to_thread()
# so they don't stall the loop. run_prompt_async() is called instead of run_prompt().

async def run_repl_async(
    client: LLMClient,
    dry_run: bool = False,
    reflect_depth: int = 0,
    verbose: bool = False,
    memory_enabled: bool = True,
    debug: bool = False,
    agents_enabled: bool = True,
) -> None:
    """Async REPL loop. Call via asyncio.run(run_repl_async(...))."""
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


    from .config_file import load_config as _load_cfg
    from .memory.triggers import (
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

    from .permissions import PermissionStore
    permission_store = PermissionStore(project_cwd=project_cwd)

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

    from .skills import load_skill_registry
    skill_registry = load_skill_registry()
    REPL_COMMANDS["/skills"] = "List all available skills"
    for _skill_name, _skill in skill_registry.items():
        _cmd_key = f"/{_skill_name}"
        if _cmd_key not in REPL_COMMANDS:
            REPL_COMMANDS[_cmd_key] = _skill.description

    from .agents import load_agent_registry
    agent_registry = load_agent_registry(project_cwd)
    from .a2a import load_a2a_manager
    a2a_manager = load_a2a_manager(project_cwd)
    from .mcp import load_mcp_manager_async
    mcp_manager = await load_mcp_manager_async(project_cwd)
    mcp_manager.set_llm_client(client)  # enables sampling/createMessage from MCP servers
    mcp_count = len(mcp_manager.server_summary())
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

    # Project init tip — shown when MINION.md hasn't been created yet
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

    session: PromptSession = PromptSession(
        history=FileHistory(str(history_path)),
        completer=_SlashCompleter(
            agent_registry=agent_registry,
            skill_registry=skill_registry,
            a2a_manager=a2a_manager,
        ),
        key_bindings=_kb,
        lexer=_InputLexer(),
        style=_INPUT_STYLE,
        multiline=True,  # renders \n in buffer as real line breaks; Enter still submits via our binding
    )
    you_prompt = FormattedText([("bold #FFD700", "you"), ("", " › ")])

    while True:
        try:
            user_input = await session.prompt_async(you_prompt)
        except (KeyboardInterrupt, EOFError):
            console.print(f"\n[{YELLOW}]Poopaye! 👋[/]")
            from rich.rule import Rule
            console.print(Rule(style=SILVER))
            mcp_manager.shutdown()
            get_tracer().finalize()
            break

        user_input = user_input.strip()
        if not user_input:
            console.print()
            continue

        get_tracer().emit("user_turn", text=user_input)

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
                console.print(
                    "[muted]Conversation primed with prompt template. "
                    "Ask a follow-up to continue.[/]"
                )
                continue

        if user_input.startswith("/agent "):
            await asyncio.to_thread(_handle_agent_direct, user_input, agent_registry, client)
            console.print()
            continue

        if user_input.startswith("/a2a"):
            _handle_a2a_command(user_input, a2a_manager)
            console.print()
            continue

        if await asyncio.to_thread(
            _handle_slash_command,
            user_input, client, conversation, project_context, state, memory_store,
            base_system_prompt=state.system_prompt,
            skill_registry=skill_registry,
            agent_registry=agent_registry,
            cwd=project_cwd,
            permission_store=permission_store,
        ):
            console.print()
            continue

        # Memory injection — off the hot path: runs in thread pool.
        # Dynamic content (memory + plan) is separated from the static base prompt
        # so the static prefix can be cached by the Anthropic API.
        memory_tokens = 0
        system_dynamic = ""
        if state.memory_enabled:
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

        if state.active_plan and state.active_plan.exists():
            goal_hint = state.active_plan_goal or state.active_plan.stem
            system_dynamic += (
                f"\n\n## Recently Executed Plan\n"
                f"Goal: {goal_hint}\n"
                f"Path: {state.active_plan}\n"
                f"Use read_file on this path if it is relevant to the current request."
            )

        if state.debug:
            console.print(f"[muted]── debug: system prompt ───────────────────[/]")
            console.print(f"[muted]{state.system_prompt}[/]")
            if system_dynamic:
                console.print(f"[muted]── debug: dynamic context ──────────────────[/]")
                console.print(f"[muted]{system_dynamic}[/]")
            console.print(f"[muted]────────────────────────────────────────────[/]")

        reflect_config = (
            ReflectionConfig(depth=state.reflect_depth)
            if state.reflect_depth > 0 else None
        )
        console.print()
        await run_prompt_async(
            user_input, client, conversation, state.system_prompt,
            system_dynamic=system_dynamic,
            dry_run=dry_run,
            reflect_config=reflect_config,
            verbose=state.verbose,
            memory_tokens=memory_tokens,
            mcp_manager=mcp_manager,
            enable_agents=state.agents_enabled,
            agent_registry=agent_registry,
            agent_depth=0,
            a2a_manager=a2a_manager,
            auto_compact=_file_cfg.context.auto_compact,
            approval_mode=state.approval_mode,
            permission_store=permission_store,
            stream_markdown=state.markdown_enabled,
        )
        console.print()

        # Memory extraction — off the hot path: runs in thread pool
        if state.memory_enabled:
            last_response = _get_last_response_text(conversation)
            if last_response:
                with console.status("[muted]saving memories...[/]", spinner="dots"):
                    extracted = await asyncio.to_thread(
                        memory_store.maybe_extract, user_input, last_response
                    )
                if extracted and state.verbose:
                    console.print(
                        f"[muted]  ↳ remembered {len(extracted)} fact(s)[/]"
                    )
                    if state.debug:
                        for r in extracted:
                            tag = f"[{r.category}·{r.type}·{r.scope}]"
                            console.print(f"[muted]     · {tag} {r.content}[/]")
                    console.print()
