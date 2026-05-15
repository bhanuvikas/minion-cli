"""HelpScreen — /help modal command palette for the Textual TUI.

Two visual states:
  State A  — browsing categories. Tab strip shows active category. Full-width
             command list. Right chrome shows total command count.
  State B  — command highlighted. Left list narrows; right detail pane appears
             with long description, usage, and related commands. Right chrome
             shows the command queued for prompt insertion.

Enter closes the modal and inserts the highlighted command into the prompt
buffer. Esc closes without inserting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from ..theme import BLUE, DIM, GOLD

if TYPE_CHECKING:
    from ...skills.registry import SkillRegistry

# ── Color tokens ──────────────────────────────────────────────────────────────

_BRIGHT   = "#f5d76e"   # highlighted command name
_CYAN     = BLUE        # command names in list, related links — minion blue
_GREEN    = "#6ed084"   # usage example lines
_CHROME   = "#3a3a3a"   # barely-visible chrome
_DIM_YEL  = "#b8a030"   # dim yellow for USAGE / RELATED section headers
_SILVER   = "#c0c0c0"   # key hint foreground
_KEYCAP   = "#4a4a4a"   # keycap pill background (visible on dark panel)

# ── Data layer ────────────────────────────────────────────────────────────────


@dataclass
class CmdInfo:
    name: str
    short_desc: str
    long_desc: str
    usage: list[str]
    related: list[str]
    category: str


@dataclass
class Category:
    key: str
    title: str
    subtitle: str
    cmd_keys: list[str] = field(default_factory=list)


CATEGORIES: list[Category] = [
    Category(
        key="session",
        title="SESSION",
        subtitle="manage the conversation window — inspect, compress, persist, restore.",
        cmd_keys=["/context", "/compact", "/clear", "/save", "/load", "/resume"],
    ),
    Category(
        key="memory",
        title="MEMORY",
        subtitle="pin facts you want minion to remember across sessions.",
        cmd_keys=["/remember", "/recall", "/forget"],
    ),
    Category(
        key="planning",
        title="PLANNING",
        subtitle="explore → structured plan → execute. for non-trivial tasks.",
        cmd_keys=["/plan"],
    ),
    Category(
        key="agents",
        title="AGENTS & EXTENSIONS",
        subtitle="outbound delegation — local subagents, remote A2A agents, and MCP server tools.",
        cmd_keys=["/agents", "/agent", "/remote", "/mcp"],
    ),
    Category(
        key="skills",
        title="SKILLS",
        subtitle="built-in workflows — grows as you add skills.",
        cmd_keys=["/skills"],  # extended at on_mount with dynamic skills
    ),
    Category(
        key="setup",
        title="SETUP",
        subtitle="one-time or infrequent configuration.",
        cmd_keys=["/setup", "/model", "/config", "/hooks", "/init"],
    ),
]

COMMAND_DETAIL: dict[str, CmdInfo] = {
    # ── session ────────────────────────────────────────────────────────────────
    "/context": CmdInfo(
        name="/context", category="session",
        short_desc="Show context window usage and token breakdown.",
        long_desc=(
            "Display a detailed breakdown of the context window: token usage\n"
            "per message, system prompt size, and remaining budget before\n"
            "compaction kicks in."
        ),
        usage=["$ /context    show full token breakdown"],
        related=["/compact", "/clear"],
    ),
    "/compact": CmdInfo(
        name="/compact", category="session",
        short_desc="Compact conversation history to free context space.",
        long_desc=(
            "Reduce context size by summarising or truncating older messages.\n"
            "Summary mode uses the LLM to write a condensed recap. Truncate\n"
            "mode drops oldest messages to stay under a token budget."
        ),
        usage=[
            "$ /compact                summarise conversation",
            "$ /compact summary        explicit summarise mode",
            "$ /compact truncate N     keep only the last N tokens",
        ],
        related=["/context", "/save", "/clear"],
    ),
    "/clear": CmdInfo(
        name="/clear", category="session",
        short_desc="Clear conversation history and start fresh.",
        long_desc=(
            "Discard all messages in the current conversation and reset to a\n"
            "blank slate. The session file is not deleted — use /save first\n"
            "if you want to preserve the history."
        ),
        usage=["$ /clear    wipe conversation history"],
        related=["/compact", "/save"],
    ),
    "/save": CmdInfo(
        name="/save", category="session",
        short_desc="Save current session to a named file.",
        long_desc=(
            "Persist the conversation to ~/.minion/sessions/<name>.json so\n"
            "it can be reloaded with /load. Useful before a /clear or when\n"
            "switching between different work contexts."
        ),
        usage=["$ /save <name>    save session as 'name'"],
        related=["/load", "/resume"],
    ),
    "/load": CmdInfo(
        name="/load", category="session",
        short_desc="Load a previously saved session.",
        long_desc=(
            "Replace the current conversation with a previously saved session.\n"
            "The current conversation is not auto-saved — run /save first if\n"
            "you want to preserve it."
        ),
        usage=["$ /load <name>    restore session named 'name'"],
        related=["/save", "/resume"],
    ),
    "/resume": CmdInfo(
        name="/resume", category="session",
        short_desc="Pick a saved session from a dropdown and load it.",
        long_desc=(
            "Open an interactive picker listing all saved sessions with\n"
            "timestamps. Arrow-key navigate, Enter to load the selection.\n"
            "Equivalent to /load but with discovery built in."
        ),
        usage=["$ /resume    open session picker"],
        related=["/save", "/load"],
    ),
    # ── memory ─────────────────────────────────────────────────────────────────
    "/remember": CmdInfo(
        name="/remember", category="memory",
        short_desc="Pin a fact for Minion to recall in future sessions.",
        long_desc=(
            "Store a piece of information in persistent memory. Minion will\n"
            "retrieve relevant memories and inject them into its context at\n"
            "the start of each session."
        ),
        usage=[
            "$ /remember <text>                          project memory",
            "$ /remember --global <text>                 global memory",
            "$ /remember --category preference <text>    with category tag",
        ],
        related=["/recall", "/forget"],
    ),
    "/recall": CmdInfo(
        name="/recall", category="memory",
        short_desc="Browse and search stored memories.",
        long_desc=(
            "Show all memories, optionally filtered by a search query. Also\n"
            "displays memory stats: count, embedding status, and whether\n"
            "memory extraction is currently enabled."
        ),
        usage=[
            "$ /recall             show all memories + stats",
            "$ /recall <query>     search memories by text",
        ],
        related=["/remember", "/forget"],
    ),
    "/forget": CmdInfo(
        name="/forget", category="memory",
        short_desc="Delete a memory by ID or text match.",
        long_desc=(
            "Remove a stored memory. Pass the memory ID (shown by /recall)\n"
            "or a text fragment — Minion finds the closest match and\n"
            "confirms before deleting."
        ),
        usage=["$ /forget <id or text>    delete matching memory"],
        related=["/recall", "/remember"],
    ),
    # ── planning ───────────────────────────────────────────────────────────────
    "/plan": CmdInfo(
        name="/plan", category="planning",
        short_desc="Create, execute, or manage a structured task plan.",
        long_desc=(
            "Run an explore-first loop that produces a structured markdown\n"
            "plan before any code is written. Review the plan, then execute\n"
            "it. Good for non-trivial multi-file or multi-step tasks."
        ),
        usage=[
            "$ /plan <goal>             explore and write a plan",
            "$ /plan --execute [file]   execute an approved plan",
            "$ /plan --list             list saved plans",
            "$ /plan --clear            discard the active plan",
        ],
        related=["/agent", "/compact"],
    ),
    # ── agents ─────────────────────────────────────────────────────────────────
    "/agents": CmdInfo(
        name="/agents", category="agents",
        short_desc="List available agent roles.",
        long_desc=(
            "Show all agent roles loaded from the builtin, user, and project\n"
            "tiers. Each role defines a specialised system prompt and tool\n"
            "set. Toggle spawning on/off with /config agents."
        ),
        usage=["$ /agents    list available roles"],
        related=["/agent", "/remote"],
    ),
    "/agent": CmdInfo(
        name="/agent", category="agents",
        short_desc="Run a specific agent role directly.",
        long_desc=(
            "Spawn a subagent with a named role and hand it a task. The\n"
            "subagent runs in isolation with its own conversation and returns\n"
            "its result as a tool result in the current session."
        ),
        usage=["$ /agent <role> <task>    run role with task"],
        related=["/agents", "/remote"],
    ),
    "/remote": CmdInfo(
        name="/remote", category="agents",
        short_desc="Delegate tasks to remote A2A agents.",
        long_desc=(
            "List or invoke remote agents configured in .minion/a2a.json.\n"
            "Remote agents are other Minion (or A2A-compatible) instances\n"
            "reachable over HTTP."
        ),
        usage=[
            "$ /remote                        list configured agents",
            "$ /remote list                   same as bare /remote",
            "$ /remote run <agent> <task>     delegate task to agent",
        ],
        related=["/agent", "/mcp"],
    ),
    "/mcp": CmdInfo(
        name="/mcp", category="agents",
        short_desc="Manage MCP server connections and call MCP tools.",
        long_desc=(
            "List connected MCP servers, fetch resources, invoke prompts,\n"
            "or reload server configs. MCP tools are available to the agent\n"
            "automatically once a server is connected."
        ),
        usage=[
            "$ /mcp                       list MCP servers",
            "$ /mcp resource <uri>        fetch a resource",
            "$ /mcp prompt <name>         invoke an MCP prompt",
            "$ /mcp reload                reload server configs",
        ],
        related=["/remote", "/agent"],
    ),
    # ── skills ─────────────────────────────────────────────────────────────────
    "/skills": CmdInfo(
        name="/skills", category="skills",
        short_desc="List all available skills.",
        long_desc=(
            "Show every skill loaded from the builtin, user (~/.minion/skills/),\n"
            "and project (.minion/skills/) tiers. Skills are YAML-defined prompt\n"
            "workflows invoked as slash commands."
        ),
        usage=["$ /skills    list all skills"],
        related=["/agent"],
    ),
    # ── setup ──────────────────────────────────────────────────────────────────
    "/setup": CmdInfo(
        name="/setup", category="setup",
        short_desc="Run the setup wizard: model, completion, and preferences.",
        long_desc=(
            "Guided first-run wizard covering: provider/model selection, API\n"
            "key entry, shell tab-completion install, and initial preferences.\n"
            "Safe to re-run — existing settings are preserved."
        ),
        usage=["$ /setup    launch setup wizard"],
        related=["/model", "/config"],
    ),
    "/model": CmdInfo(
        name="/model", category="setup",
        short_desc="Interactively change provider, model, and API keys.",
        long_desc=(
            "Open the 3-step model wizard: pick a provider, select a model,\n"
            "then paste your API key. Changes take effect immediately without\n"
            "restarting."
        ),
        usage=["$ /model    open model wizard"],
        related=["/setup", "/config"],
    ),
    "/config": CmdInfo(
        name="/config", category="setup",
        short_desc="View and edit all settings in an interactive panel.",
        long_desc=(
            "Open the settings panel — a scrollable list of every config key\n"
            "with inline editors. Changes are written to .minion/config.toml\n"
            "immediately. Subcommands work in console mode."
        ),
        usage=[
            "$ /config                     interactive settings panel",
            "$ /config reflect on          enable self-refine (console)",
            "$ /config verbose on          enable verbose output (console)",
        ],
        related=["/model", "/hooks"],
    ),
    "/hooks": CmdInfo(
        name="/hooks", category="setup",
        short_desc="Enable, disable, or list lifecycle hooks.",
        long_desc=(
            "Hooks are shell commands that fire on lifecycle events (pre/post\n"
            "tool use, session start/end). Define them in .minion/config.toml\n"
            "under [hooks.user]."
        ),
        usage=[
            "$ /hooks         show hook status",
            "$ /hooks list    list all configured hooks",
            "$ /hooks on      enable all hooks",
            "$ /hooks off     disable all hooks",
        ],
        related=["/config"],
    ),
    "/init": CmdInfo(
        name="/init", category="setup",
        short_desc="Create a MINION.md project context file.",
        long_desc=(
            "Generate a MINION.md template in the current directory. Minion\n"
            "reads this file at startup and injects it into the system prompt\n"
            "as project-specific context."
        ),
        usage=["$ /init    scaffold MINION.md in current directory"],
        related=["/config"],
    ),
}

_GENERAL_CMDS = ["/help", "/quit", "/exit"]

# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = f"""
HelpScreen {{
    align: center middle;
    background: #000000 40%;
}}
#help-panel {{
    width: 85%;
    height: 88%;
    background: #0d0d0d;
    border: round #3a3a3a;
}}
#help-title {{
    height: auto;
    padding: 0 2;
    background: #0d0d0d;
    border-bottom: solid #2e2e2e;
}}
#help-greeting {{
    height: auto;
    padding: 0 2;
    border-bottom: solid #2e2e2e;
}}
#help-tabs {{
    height: auto;
    padding: 0 2;
    border-bottom: solid #2e2e2e;
}}
#help-section {{
    height: auto;
    padding: 1 2 0 2;
}}
#help-body {{
    height: 1fr;
    layout: horizontal;
}}
#cmd-list {{
    width: 1fr;
    height: 1fr;
    scrollbar-size-vertical: 1;
    scrollbar-background: #111111;
    scrollbar-color: #2a2a2a;
    scrollbar-color-hover: #444444;
    scrollbar-color-active: {DIM};
}}
#cmd-list-content {{
    height: auto;
    padding: 1 2;
}}
#cmd-detail {{
    width: 1fr;
    height: 1fr;
    padding: 1 2;
    border-left: solid #2e2e2e;
}}
#help-general {{
    height: auto;
    padding: 1 2;
    border-top: solid #2e2e2e;
}}
#help-foot {{
    height: 2;
    padding: 0 2;
    background: #0d0d0d;
    color: {DIM};
    border-top: solid #2e2e2e;
}}
"""


# ── HelpScreen ────────────────────────────────────────────────────────────────


class HelpScreen(ModalScreen):  # type: ignore[type-arg]
    """Command palette modal opened by /help in TUI mode."""

    CSS = _CSS

    BINDINGS = [
        Binding("escape", "cancel",    show=False, priority=True),
        Binding("left",   "nav_left",  show=False, priority=True),
        Binding("right",  "nav_right", show=False, priority=True),
        Binding("up",     "nav_up",    show=False, priority=True),
        Binding("down",   "nav_down",  show=False, priority=True),
        Binding("enter",  "confirm",   show=False, priority=True),
    ]

    def __init__(self, skill_registry: "Optional[SkillRegistry]" = None) -> None:
        super().__init__()
        self._skill_registry = skill_registry
        self._cat_idx: int = 0
        self._cmd_idx: Optional[int] = None
        self._skill_cmds: list[CmdInfo] = []
        # Deep-copy cmd_keys lists so on_mount mutations don't affect module data.
        self._cats: list[Category] = [
            Category(c.key, c.title, c.subtitle, list(c.cmd_keys))
            for c in CATEGORIES
        ]

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Vertical(id="help-panel"):
            yield Static("", id="help-title")
            yield Static("", id="help-greeting")
            yield Static("", id="help-tabs")
            yield Static("", id="help-section")
            with Horizontal(id="help-body"):
                with VerticalScroll(id="cmd-list"):
                    yield Static("", id="cmd-list-content")
                yield Static("", id="cmd-detail")
            yield Static("", id="help-general")
            yield Static("", id="help-foot")

    def on_mount(self) -> None:
        self.query_one("#cmd-list", VerticalScroll).can_focus = False
        # Focus the panel so arrow-key bindings reach the modal instead of the
        # InputArea behind it. Without an explicit focus() here, Textual leaves
        # focus on whatever had it before the modal opened (the input box), and
        # the input box's priority bindings swallow ↑↓ before ours fire.
        panel = self.query_one("#help-panel", Vertical)
        panel.can_focus = True
        panel.focus()

        # Extend skills category with dynamic skills from the registry.
        if self._skill_registry is not None:
            skills_cat = next(c for c in self._cats if c.key == "skills")
            for skill_name, skill in self._skill_registry.items():
                cmd_key = f"/{skill_name}"
                if cmd_key not in skills_cat.cmd_keys:
                    skills_cat.cmd_keys.append(cmd_key)
                if cmd_key not in COMMAND_DETAIL:
                    self._skill_cmds.append(CmdInfo(
                        name=cmd_key,
                        category="skills",
                        short_desc=skill.description,
                        long_desc=skill.description,
                        usage=[f"$ {cmd_key}    {skill.description}"],
                        related=["/skills"],
                    ))

        self._refresh()

    # ── Data helpers ──────────────────────────────────────────────────────────

    def _total_commands(self) -> int:
        return sum(len(c.cmd_keys) for c in self._cats)

    def _current_cmds(self) -> list[CmdInfo]:
        cat = self._cats[self._cat_idx]
        result: list[CmdInfo] = []
        for key in cat.cmd_keys:
            if key in COMMAND_DETAIL:
                result.append(COMMAND_DETAIL[key])
            else:
                for sc in self._skill_cmds:
                    if sc.name == key:
                        result.append(sc)
                        break
        return result

    # ── Markup builders ───────────────────────────────────────────────────────

    def _build_title(self) -> Table:
        table = Table.grid(expand=True, padding=0)
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True, justify="right")
        left = Text.from_markup(
            f"[{DIM}]┌─[/] [bold]/help[/] [{DIM}]— commands[/]"
        )
        if self._cmd_idx is not None:
            cmds = self._current_cmds()
            if 0 <= self._cmd_idx < len(cmds):
                cmd_name = cmds[self._cmd_idx].name
                right = Text.from_markup(
                    f"[{DIM}]queue for prompt:[/] [bold {_GREEN}]{cmd_name}[/]"
                )
            else:
                right = Text.from_markup(f"[{DIM}]{self._total_commands()} commands[/]")
        else:
            right = Text.from_markup(f"[{DIM}]{self._total_commands()} commands[/]")
        table.add_row(left, right)
        return table

    def _build_greeting(self) -> str:
        pill_down  = f"[bold white on {_KEYCAP}] ↓ [/]"
        pill_enter = f"[bold white on {_KEYCAP}] ⏎ [/]"
        if self._cmd_idx is None:
            return (
                f"[bold {GOLD}]Bello![/]  "
                f"[{DIM}]Pick a category, then {pill_down} into a command to inspect or insert it.[/]"
            )
        return (
            f"[bold {GOLD}]Bello![/]  "
            f"[{DIM}]Press {pill_enter} to drop the highlighted command into your prompt.[/]"
        )

    def _build_tabs(self) -> str:
        parts: list[str] = []
        for i, cat in enumerate(self._cats):
            if i == self._cat_idx:
                parts.append(f"[bold black on {GOLD}] {cat.key} [/]")
            else:
                parts.append(f"[{DIM}]{cat.key}[/]")
        return "   ".join(parts)

    def _build_section_header(self) -> str:
        cat = self._cats[self._cat_idx]
        if cat.key == "skills":
            n = len(cat.cmd_keys)
            title_part = f"[bold {GOLD}]{cat.title}[/]  [{DIM}]{n} skills loaded[/]"
        else:
            title_part = f"[bold {GOLD}]{cat.title}[/]"
        subtitle_part = f"[{DIM}]{cat.subtitle}[/]"
        return f"{title_part}  {subtitle_part}"

    def _build_cmd_list(self) -> Text:
        cmds = self._current_cmds()
        out = Text()
        for i, cmd in enumerate(cmds):
            highlighted = (self._cmd_idx is not None and i == self._cmd_idx)
            if highlighted:
                out.append("  ▸ ", style=f"bold {GOLD}")
                out.append(cmd.name, style=f"bold {_BRIGHT} on #1a1400")
                pad = max(0, 22 - len(cmd.name))
                out.append(" " * pad, style="on #1a1400")
                out.append(cmd.short_desc, style=f"default on #1a1400")
            else:
                out.append("      ")
                out.append(cmd.name, style=_CYAN)
                pad = max(0, 22 - len(cmd.name))
                out.append(" " * pad)
                out.append(cmd.short_desc, style=DIM)
            out.append("\n")
        return out

    def _build_detail(self, cmd: CmdInfo) -> str:
        cat = self._cats[self._cat_idx]
        badge = f"[{DIM} on #1a1a1a]  {cat.key}  [/]"
        lines = [
            f"[bold {_BRIGHT}]{cmd.name}[/]  {badge}",
            "",
            f"{cmd.long_desc}",
            "",
            f"[bold {_DIM_YEL}]USAGE[/]",
        ]
        for usage_line in cmd.usage:
            lines.append(f"[{_GREEN}]  {usage_line}[/]")
        if cmd.related:
            lines.append("")
            lines.append(f"[bold {_DIM_YEL}]RELATED[/]")
            related_parts = [f"[{_CYAN}]{r}[/]" for r in cmd.related]
            lines.append("  " + f"[{DIM}] · [/]".join(related_parts))
        return "\n".join(lines)

    def _build_general(self) -> str:
        cmd_parts = [f"[{_CYAN}]{c}[/]" for c in _GENERAL_CMDS]
        cmds_str = f"  [{DIM}]·[/]  ".join(cmd_parts)
        return (
            f"[bold {DIM}]GENERAL[/]  {cmds_str}  "
            f"[{DIM}]meta commands · always available[/]"
        )

    def _build_footer(self) -> str:
        dot = f"[{DIM}]·[/]"
        if self._cmd_idx is None:
            parts = [
                f"[bold {_SILVER}]↓[/] [{DIM}]inspect a command[/]",
                f"[bold {_SILVER}]← →[/] [{DIM}]switch category[/]",
                f"[bold {_SILVER}]esc[/] [{DIM}]close[/]",
            ]
        else:
            parts = [
                f"[bold {_SILVER}]↑ ↓[/] [{DIM}]navigate[/]",
                f"[bold {_SILVER}]↵[/] [{DIM}]insert command[/]",
                f"[bold {_SILVER}]esc[/] [{DIM}]close[/]",
            ]
        return "  " + f"  {dot}  ".join(parts)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        cmds = self._current_cmds()
        try:
            self.query_one("#help-title", Static).update(self._build_title())
            self.query_one("#help-greeting", Static).update(self._build_greeting())
            self.query_one("#help-tabs", Static).update(self._build_tabs())
            self.query_one("#help-section", Static).update(self._build_section_header())
            self.query_one("#cmd-list-content", Static).update(self._build_cmd_list())
            self.query_one("#help-general", Static).update(self._build_general())
            self.query_one("#help-foot", Static).update(self._build_footer())

            detail = self.query_one("#cmd-detail", Static)
            if self._cmd_idx is not None and 0 <= self._cmd_idx < len(cmds):
                detail.display = True
                detail.update(self._build_detail(cmds[self._cmd_idx]))
            else:
                detail.display = False
        except Exception:
            pass

    # ── Key actions ───────────────────────────────────────────────────────────

    def action_nav_left(self) -> None:
        self._cat_idx = (self._cat_idx - 1) % len(self._cats)
        self._cmd_idx = None
        self._refresh()

    def action_nav_right(self) -> None:
        self._cat_idx = (self._cat_idx + 1) % len(self._cats)
        self._cmd_idx = None
        self._refresh()

    def action_nav_down(self) -> None:
        cmds = self._current_cmds()
        if not cmds:
            return
        if self._cmd_idx is None:
            self._cmd_idx = 0
        else:
            self._cmd_idx = min(len(cmds) - 1, self._cmd_idx + 1)
        self._refresh()

    def action_nav_up(self) -> None:
        if self._cmd_idx is None:
            return
        if self._cmd_idx == 0:
            self._cmd_idx = None
        else:
            self._cmd_idx -= 1
        self._refresh()

    def action_confirm(self) -> None:
        if self._cmd_idx is None:
            return
        cmds = self._current_cmds()
        if 0 <= self._cmd_idx < len(cmds):
            self.dismiss(cmds[self._cmd_idx].name + " ")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "tab":
            event.prevent_default()
            event.stop()
