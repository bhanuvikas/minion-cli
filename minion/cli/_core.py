"""CLI entry point — argument parsing and delegation only.

Single responsibility: define the typer app, parse CLI arguments,
and hand off to the right module. No business logic lives here.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from .. import __version__
from ..llm import get_client
from ..repl import run_repl_async
from ..theme import YELLOW, console, print_error

load_dotenv(Path.home() / ".minion" / ".env")                 # user-level defaults
load_dotenv(Path.cwd() / ".minion" / ".env", override=True)  # project-level overrides

from ..config import load_config as _load_config  # noqa: E402 — after dotenv

# Subcommand names registered with typer — used by _entry() to distinguish
# "minion doctor" (subcommand) from "minion 'fix the bug'" (one-shot prompt).
_KNOWN_SUBCOMMANDS = frozenset({
    "doctor", "skills", "mcp", "agents", "remote",
    "setup", "config", "model", "memory",
})

# Options that consume the next token as their value.
_OPTS_WITH_VALUE = frozenset({
    "-p", "--provider",
    "-m", "--model",
    "--reflect",
    "--install-completion",
    "--show-completion",
})

# ─── Root app ─────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="minion",
    help="Minion — your agentic coding assistant.",
    add_completion=True,
    rich_markup_mode="rich",
    no_args_is_help=False,
    invoke_without_command=True,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p",
        help="LLM provider: anthropic | openai | openrouter",
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m",
        help="Model ID (overrides MINION_MODEL env var)",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show tool calls without executing them.",
    ),
    reflect: Optional[int] = typer.Option(
        None, "--reflect",
        help="Enable self-refine reflection. Pass depth (1-3). Example: --reflect 1",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Show critique text and response diffs during reflection.",
    ),
    no_memory: bool = typer.Option(
        False, "--no-memory",
        help="Disable memory retrieval and extraction for this session (private mode).",
    ),
    debug: bool = typer.Option(
        False, "--debug",
        help="Print system prompt and debug info before each turn.",
    ),
    no_trace: bool = typer.Option(
        False, "--no-trace",
        help="Disable session tracing (no .jsonl written to ~/.minion/traces/).",
    ),
    version: bool = typer.Option(
        False, "--version",
        help="Show version and exit.",
        is_eager=True,
    ),
) -> None:
    """[bold yellow]Minion[/bold yellow] — your agentic coding assistant.

    Run without arguments to start interactive REPL mode.
    Pass a prompt to run a single task and exit: [bold]minion "what is 2+2?"[/bold]
    """
    if ctx.invoked_subcommand is not None:
        return

    if version:
        console.print(f"minion-cli [bold {YELLOW}]v{__version__}[/]")
        raise typer.Exit()

    cfg = _load_config()

    effective_provider = provider or cfg.llm.provider
    effective_model    = model    or cfg.llm.model
    effective_reflect  = reflect  if reflect is not None else cfg.agent.reflect_depth
    effective_verbose  = verbose  or cfg.agent.verbose
    effective_debug    = debug    or cfg.agent.debug
    effective_memory   = not no_memory and cfg.memory.enabled
    effective_trace    = not no_trace  and cfg.tracing.enabled

    try:
        client = get_client(effective_provider, effective_model)
    except ValueError as e:
        if _first_run:
            from ..llm.factory import _PlaceholderClient
            client = _PlaceholderClient()
        else:
            print_error(str(e))
            raise typer.Exit(code=1)

    if effective_trace:
        from ..tracing import init_tracer
        init_tracer(session_id=str(uuid.uuid4()))

    asyncio.run(run_repl_async(
        client, dry_run=dry_run, reflect_depth=effective_reflect,
        verbose=effective_verbose, memory_enabled=effective_memory,
        debug=effective_debug,
        first_run=_first_run,
    ))


# ─── Mount sub-apps ───────────────────────────────────────────────────────────

from . import agents, mcp, memory, remote, skills  # noqa: E402
from .config import config_cmd, model_cmd, setup_cmd  # noqa: E402
from .doctor import doctor_cmd  # noqa: E402

app.add_typer(skills.app, name="skills")
app.add_typer(mcp.app,    name="mcp")
app.add_typer(agents.app, name="agents")
app.add_typer(remote.app, name="remote")
app.add_typer(memory.app, name="memory")

app.command("config")(config_cmd)
app.command("model")(model_cmd)
app.command("setup")(setup_cmd)
app.command("doctor")(doctor_cmd)


# ─── First-run detection ──────────────────────────────────────────────────────

_first_run: bool = False  # Set by _entry() when TUI first-run onboarding should run


def _needs_setup() -> bool:
    """Return True if no API key is configured anywhere."""
    import os
    return not any(
        os.environ.get(k)
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY")
    )


def _is_tui_mode() -> bool:
    """Return True if the session will use the Textual TUI (not console/pipe)."""
    import os
    import sys
    return (
        sys.stdout.isatty()
        and os.environ.get("MINION_NO_TUI", "").lower() not in ("1", "true")
    )


# ─── One-shot runner ──────────────────────────────────────────────────────────

def _run_one_shot(prompt: str, raw_argv: list) -> None:
    """Run a single prompt and exit. Called by _entry() before typer routing."""
    import sys as _sys

    cfg = _load_config()

    provider = model = None
    reflect = cfg.agent.reflect_depth
    dry_run = False
    verbose = cfg.agent.verbose

    i = 0
    while i < len(raw_argv):
        tok = raw_argv[i]
        if tok in ("-p", "--provider") and i + 1 < len(raw_argv):
            provider = raw_argv[i + 1]; i += 2
        elif tok in ("-m", "--model") and i + 1 < len(raw_argv):
            model = raw_argv[i + 1]; i += 2
        elif tok == "--reflect" and i + 1 < len(raw_argv):
            try:
                reflect = int(raw_argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif tok == "--dry-run":
            dry_run = True; i += 1
        elif tok in ("-v", "--verbose"):
            verbose = True; i += 1
        else:
            i += 1

    try:
        client = get_client(provider or cfg.llm.provider, model or cfg.llm.model)
    except ValueError as e:
        print_error(str(e))
        _sys.exit(1)

    import os as _os
    from ..context import build_project_context
    from ..context.prompts import build_system_prompt
    from ..llm.conversation import Conversation
    from ..llm.reflection import ReflectionConfig
    from ..runner import run_prompt_async
    from ..tools.permissions import PermissionStore

    project_cwd = Path.cwd()
    system_prompt = build_system_prompt(build_project_context(project_cwd))
    reflect_cfg = ReflectionConfig(depth=reflect) if reflect else None
    stream_markdown = _os.getenv("MINION_MARKDOWN", "true").lower() != "false"

    asyncio.run(run_prompt_async(
        prompt, client, Conversation(), system_prompt,
        dry_run=dry_run, reflect_config=reflect_cfg,
        verbose=verbose, permission_store=PermissionStore(project_cwd=project_cwd),
        stream_markdown=stream_markdown,
    ))


# ─── Smart entry point ────────────────────────────────────────────────────────

def _entry() -> None:
    """Intercepts one-shot prompts before typer's subcommand routing.

    typer/click groups always try to resolve the first positional arg as a
    subcommand, so `minion "fix the bug"` would fail with "No such command".
    This wrapper scans sys.argv first: if the first non-option positional arg
    is not a known subcommand, it extracts it as a one-shot prompt and bypasses
    typer entirely.
    """
    import sys

    raw = sys.argv[1:]
    prefix: list = []
    i = 0

    while i < len(raw):
        tok = raw[i]
        if tok in _OPTS_WITH_VALUE and i + 1 < len(raw):
            prefix += [tok, raw[i + 1]]
            i += 2
        elif tok.startswith("-"):
            prefix.append(tok)
            i += 1
        elif tok in _KNOWN_SUBCOMMANDS:
            app()
            return
        else:
            prompt = " ".join(raw[i:])
            if _needs_setup():
                from ..config import run_setup_wizard
                asyncio.run(run_setup_wizard())
            _run_one_shot(prompt, prefix)
            return

    # No positional args → REPL / --help / --version via typer
    if _needs_setup() and not any(a.startswith("-") for a in raw):
        if _is_tui_mode():
            # Let the TUI handle first-run setup via the /model wizard + completion modal.
            global _first_run
            _first_run = True
        else:
            from ..config import run_setup_wizard
            asyncio.run(run_setup_wizard())
    app()
