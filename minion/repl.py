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
from .llm.base import LLMClient
from .runner import run_prompt
from .theme import BLUE, YELLOW, console, print_greeting

# ─── Slash command registry ───────────────────────────────────────────────────
# Single source of truth for both the /help display and tab-completion.
# Add an entry here to make a new command available everywhere automatically.

REPL_COMMANDS = {
    "/help":  "Show available commands",
    "/model": "Interactively change provider, model, and API keys",
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

def _handle_slash_command(raw: str, client: LLMClient) -> bool:
    """Dispatch a slash command. Returns True if the input was handled."""
    cmd = raw.strip().lower()

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

        if _handle_slash_command(user_input, client):
            console.print()
            continue

        console.print()
        run_prompt(user_input, client)
        console.print()
