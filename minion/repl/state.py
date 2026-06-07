"""REPL session state, command registry, and command context bundle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm.base import LLMClient
    from ..llm.conversation import Conversation
    from ..context import ProjectContext
    from ..memory.store import MemoryStore
    from ..skills.registry import SkillRegistry
    from ..agents.manifest import AgentRoleManifest


# ─── Session state ────────────────────────────────────────────────────────────

@dataclass
class ReplState:
    """Mutable REPL session state passed through the command loop.

    A single instance is created at session startup and mutated in place
    by slash command handlers.
    """
    reflect_depth: int = 0       # 0 = off; N = max self-refine rounds
    verbose: bool = False        # show critique text and diffs when True
    memory_enabled: bool = True  # False = private mode: skip injection + extraction
    debug: bool = False          # print full system prompt before each LLM call
    active_plan: Optional[Path] = None       # path to the current plan file
    active_plan_goal: Optional[str] = None   # goal text stored alongside path
    agents_enabled: bool = True  # False = exclude spawn_agent from tool list
    approval_mode: str = "off"   # "off" | "edits" | "yolo"
    markdown_enabled: bool = True  # render LLM responses as live markdown
    system_prompt: str = ""      # mutable override; updated by /init to hot-reload MINION.md
    # Agent chat mode — all None means normal minion mode
    active_agent_role: Optional[str] = None
    active_agent_conversation: Optional["Conversation"] = None
    active_agent_manifest: Optional["AgentRoleManifest"] = None


# ─── Slash command registry ───────────────────────────────────────────────────
# Single source of truth for both the /help display and tab-completion.
# Add an entry here to make a new command available everywhere automatically.

REPL_COMMANDS: dict[str, str] = {
    "/help":    "Show available commands",
    "/init":    "Create a MINION.md template in the current directory",
    "/model":   "Interactively change provider, model, and API keys",
    "/setup":   "Run setup wizard: model, tab completion, and preferences",
    "/context": "Show context window usage and token breakdown",
    "/config":  "Settings: /config [show|reflect|verbose|debug|markdown|approval|agents] [args]",
    "/hooks":   "Hooks: /hooks | /hooks list | /hooks on | /hooks off",
    "/remember": "Remember something: /remember [--global] [--category identity|preference|project|event] <text>",
    "/forget":  "Forget a memory: /forget <id or text>",
    "/memories": "Browse, search, edit and delete memories: /memories [query]",
    "/compact": "Compact conversation: /compact | /compact summary | /compact truncate [N]",
    "/clear":   "Clear conversation history and start fresh",
    "/save":    "Save session: /save <name>",
    "/load":    "Load session: /load <name>",
    "/resume":  "Pick a saved session from a dropdown and load it",
    "/plan":    "Plan a task: /plan <goal> | /plan --execute [file] | /plan --list | /plan --clear",
    "/mcp":     "MCP servers: /mcp | /mcp resource <uri> | /mcp prompt <name> | /mcp reload",
    "/agents":  "List available agent roles (toggle: /config agents [on|off])",
    "/agent":   "Run or chat with a role: /agent <role> [task]  (no task = persistent chat)",
    "/back":    "Exit agent chat mode (silent — agent conversation not shared with minion)",
    "/handoff": "Exit agent chat mode and share conversation summary with minion",
    "/skills":  "Browse, run, create and edit skill workflows",
    "/remote":  "Remote agents: /remote | /remote list | /remote run <agent> <task>",
    "/quit":    "Exit Minion",
    "/exit":    "Exit Minion (alias for /quit)",
}


# ─── Command context ──────────────────────────────────────────────────────────

@dataclass
class CommandContext:
    """Runtime dependencies bundle passed to slash command handlers.

    Replaces the 12-parameter signature on _handle_slash_command.
    Build once per session and mutate state in place as commands run.
    """
    client: "LLMClient"
    conversation: "Conversation"
    state: Optional[ReplState] = None
    project_context: "Optional[ProjectContext]" = None
    memory_store: "Optional[MemoryStore]" = None
    skill_registry: "Optional[SkillRegistry]" = None
    agent_registry: Any = None
    a2a_manager: Any = None
    cwd: Optional[Path] = None
    permission_store: Any = None
    hook_runner: Any = None
    confirmation_manager: Any = None
    mcp_manager: Any = None
    renderer: Any = None

    @property
    def base_system_prompt(self) -> str:
        """Current system prompt — reads from state so /init hot-reloads are reflected."""
        return self.state.system_prompt if self.state else ""
