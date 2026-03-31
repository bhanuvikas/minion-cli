"""Interactive REPL: session management, slash commands, completion, key bindings.

Single responsibility: own everything about the interactive loop —
how input is read, how slash commands are dispatched, how the session
persists history across restarts.

The actual LLM call is delegated to runner.run_prompt() so this file
stays focused on input/UX concerns.
"""

from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings

from .config import run_model_config
from .conversation import Conversation
from .llm.base import LLMClient
from .runner import run_prompt
from .session import list_sessions, load, save
from .theme import BLUE, YELLOW, console, print_error, print_greeting

# ─── Slash command registry ───────────────────────────────────────────────────
# Single source of truth for both the /help display and tab-completion.
# Add an entry here to make a new command available everywhere automatically.

REPL_COMMANDS = {
    "/help":  "Show available commands",
    "/model": "Interactively change provider, model, and API keys",
    "/clear": "Clear conversation history and start fresh",
    "/save":  "Save session: /save <name>",
    "/load":  "Load session: /load <name>",
    "/quit":  "Exit Minion",
    "/exit":  "Exit Minion (alias for /quit)",
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
        elif len(state.completions) == 1:
            buf.apply_completion(state.completions[0])
    buf.validate_and_handle()


# ─── Slash command handler ────────────────────────────────────────────────────

def _handle_slash_command(raw: str, client: LLMClient, conversation: Conversation) -> bool:
    """Dispatch a slash command. Returns True if the input was handled."""
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

    if cmd == "/model":
        run_model_config(client)
        return True

    if cmd == "/clear":
        conversation.clear()
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
        try:
            loaded = load(arg)
            # Replace conversation contents in-place so repl.py's reference stays valid
            conversation.messages = loaded.messages
            conversation.total_tokens = loaded.total_tokens
            conversation._model = loaded._model
            msg_count = len(loaded.messages)
            console.print(
                f"[{YELLOW}]Loaded session[/] [{BLUE}]{arg}[/] "
                f"[muted]({msg_count} messages, {loaded.total_tokens:,} tokens)[/]"
            )
        except FileNotFoundError as e:
            print_error(str(e))
        return True

    if cmd.startswith("/"):
        console.print(
            f"[muted]Unknown command '{cmd}'. "
            f"Type [bold]/help[/bold] for available commands.[/]"
        )
        return True

    return False


# ─── REPL entry point ─────────────────────────────────────────────────────────

def run_repl(client: LLMClient) -> None:
    """Start the interactive REPL loop."""
    print_greeting()
    console.print(
        f"[muted]Type [bold]/help[/bold] for commands · "
        f"[bold]/quit[/bold] to exit[/]\n"
    )

    history_path = Path.home() / ".minion" / "history"
    history_path.parent.mkdir(exist_ok=True)

    conversation = Conversation()

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
            continue

        if _handle_slash_command(user_input, client, conversation):
            console.print()
            continue

        console.print()
        run_prompt(user_input, client, conversation)
        console.print()
