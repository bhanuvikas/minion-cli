"""prompt_toolkit input UI: tab-completion, syntax highlighting, key bindings."""

from __future__ import annotations

import io
import re

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style

from ..theme import BLUE, YELLOW
from .state import REPL_COMMANDS

# ─── Regex constants ──────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r'@[\w./\-]+|/\S+')

# Strip cursor-movement ANSI codes (A-Z, lowercase except 'm', and \r) while
# preserving SGR color codes (which always end in 'm').
_CURSOR_ANSI_RE = re.compile(
    r'\r'
    r'|\x1b\[[\d;?]*[A-Z]'
    r'|\x1b\[[\d;?]*[a-ln-z]'
)


# ─── Tab completion ───────────────────────────────────────────────────────────

class _SlashCompleter(Completer):
    """Completes slash commands from REPL_COMMANDS when input starts with '/'.

    When registries are provided, also completes second-argument values for:
        /agent <role>       — role names from agent_registry
        /skill <name>       — skill names from skill_registry
        /remote run <agent> — agent names from a2a_manager
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

            if cmd == "/remote" and len(parts) >= 2 and parts[1] == "run":
                agent_prefix = parts[2] if len(parts) >= 3 else ""
                if self._a2a_manager is not None:
                    for name in sorted(self._a2a_manager.agent_names()):
                        if name.startswith(agent_prefix):
                            yield Completion(name[len(agent_prefix):], display=name)
                return

        for cmd, description in REPL_COMMANDS.items():
            if cmd.startswith(text):
                yield Completion(
                    cmd[len(text):],
                    display=cmd,
                    display_meta=description,
                )


# ─── Syntax highlighting ──────────────────────────────────────────────────────

class _InputLexer(Lexer):
    """Highlight valid /commands (yellow) and @file mentions (blue) in the input."""

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
    'slash-command': f'bold {YELLOW}',
    'at-mention':    f'bold {BLUE}',
})


# ─── TUI console capture ──────────────────────────────────────────────────────

class _CaptureBuf(io.StringIO):
    """StringIO that claims to be a TTY so Rich emits color codes, while
    filtering cursor-movement escape sequences on write."""

    # Sentinel checked by _handle_init to skip Live/status rendering
    is_capture_buf: bool = True

    def isatty(self) -> bool:
        return True

    def write(self, s: str) -> int:
        return super().write(_CURSOR_ANSI_RE.sub('', s))


# ─── Key bindings ─────────────────────────────────────────────────────────────

_kb = KeyBindings()


@_kb.add("enter")
def _enter_with_completion(event):
    buf = event.app.current_buffer
    state = buf.complete_state
    if state:
        current = state.current_completion
        if current is not None:
            buf.apply_completion(current)
            return
        elif len(state.completions) == 1:
            buf.apply_completion(state.completions[0])
            return
    buf.validate_and_handle()


@_kb.add("escape", "enter")
def _insert_newline(event):
    """Option+Enter (Mac) / Alt+Enter inserts a newline for multi-line prompts."""
    event.app.current_buffer.insert_text("\n")


@_kb.add("c-j")
def _paste_newline(event):
    """Ctrl+J (raw LF) inserts a newline so pasted multi-line text accumulates."""
    event.app.current_buffer.insert_text("\n")
