"""Handler for the /config slash command and all its subcommands."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..theme import BLUE, SILVER, YELLOW, console, print_error, print_mode_toggle
from .state import CommandContext

_MISSING = object()   # sentinel for "no session state available"
_GOLD = "#FFD700"
_DIM  = "#666666"

# ── Handler type ──────────────────────────────────────────────────────────────

_ConfigHandler = Callable[[list[str], CommandContext], bool]


# ── Subcommand handlers ───────────────────────────────────────────────────────

def _handle_show(args: list[str], ctx: CommandContext) -> bool:
    import os as _os
    from ..config import create_project_config, load_config as _load_cfg, load_config_levels

    cwd = ctx.cwd or Path.cwd()
    state = ctx.state

    # Auto-create project config on first /config call
    project_config = cwd / ".minion" / "config.toml"
    if not project_config.exists():
        global_raw, _ = load_config_levels()   # root only — no project yet
        path = create_project_config(cwd, global_raw)
        console.print(f"[{YELLOW}]Created[/] [{_DIM}]{path}[/]\n")

    cfg = _load_cfg(cwd=cwd)
    global_raw, proj_raw = load_config_levels(cwd=cwd)

    # Determine source tag for a single setting.
    # Priority: session > project file > root file > default
    def _tag(section: str, key: str, state_val=_MISSING, cfg_val=None) -> str:
        if state_val is not _MISSING and state_val != cfg_val:
            return f"[bold {_GOLD}][session][/]"
        sec = proj_raw.get(section)
        if isinstance(sec, dict) and key in sec:
            return f"[{BLUE}][project][/]"
        sec = global_raw.get(section)
        if isinstance(sec, dict) and key in sec:
            return f"[{SILVER}][root][/]"
        return f"[{_DIM}][default][/]"

    W = 36  # fixed column width for "  key = value" — wide enough for all keys

    def row(key: str, val: object, section: str, *, state_val: object = _MISSING) -> str:
        kv  = f"  {key} = {val}"
        pad = " " * max(0, W - len(kv))
        return f"{kv}{pad}  {_tag(section, key, state_val, val)}"

    a = cfg.agent
    m = cfg.memory
    s = state   # ReplState or None

    env_provider = _os.getenv("MINION_PROVIDER") or cfg.llm.provider
    env_model    = _os.getenv("MINION_MODEL")    or cfg.llm.model

    console.print(
        f"\n[bold {YELLOW}]Effective configuration[/]  "
        f"[{_DIM}](session › project › root › default):[/]\n"
    )

    console.print(f"[bold][llm][/]")
    console.print(f"  provider = {env_provider or '(auto)'}")
    console.print(f"  model    = {env_model or '(provider default)'}")
    console.print()

    console.print(f"[bold][agent][/]")
    console.print(row("reflect_depth",      a.reflect_depth,      "agent", state_val=s.reflect_depth      if s else _MISSING))
    console.print(row("verbose",            a.verbose,            "agent", state_val=s.verbose            if s else _MISSING))
    console.print(row("debug",              a.debug,              "agent", state_val=s.debug              if s else _MISSING))
    console.print(row("agents_enabled",     a.agents_enabled,     "agent", state_val=s.agents_enabled     if s else _MISSING))
    console.print(row("max_subagent_depth", a.max_subagent_depth, "agent"))
    console.print(row("approval_mode",      a.approval_mode,      "agent", state_val=s.approval_mode      if s else _MISSING))
    console.print(row("markdown_enabled",   a.markdown_enabled,   "agent", state_val=s.markdown_enabled   if s else _MISSING))
    console.print()

    console.print(f"[bold][memory][/]")
    console.print(row("enabled",                 m.enabled,                "memory", state_val=s.memory_enabled if s else _MISSING))
    console.print(row("top_k",                   m.top_k,                  "memory"))
    console.print(row("similarity_threshold",    m.similarity_threshold,   "memory"))
    console.print(row("consolidation_threshold", m.consolidation_threshold,"memory"))
    console.print(row("extraction_trigger",      m.extraction_trigger,     "memory"))
    console.print(row("extraction_min_words",    m.extraction_min_words,   "memory"))
    console.print()

    console.print(f"[bold][context][/]")
    console.print(row("auto_compact", cfg.context.auto_compact, "context"))
    console.print()

    console.print(f"[bold][a2a][/]")
    console.print(f"  auth_token = {'(set)' if cfg.a2a.auth_token else '(not set)'}")
    console.print()

    console.print(f"[bold][tracing][/]")
    console.print(row("enabled", cfg.tracing.enabled, "tracing"))
    console.print()

    console.print(f"[bold][hooks][/]")
    console.print(row("enabled",           cfg.hooks_config.enabled,          "hooks"))
    console.print(row("builtin_minion_md", cfg.hooks_config.builtin_minion_md, "hooks"))
    console.print()

    return True


def _handle_reflect(args: list[str], ctx: CommandContext) -> bool:
    state = ctx.state
    if state is None:
        return True
    arg = args[0] if args else ""
    if not arg:
        status = "off" if state.reflect_depth == 0 else f"on (depth={state.reflect_depth})"
        console.print(f"[{YELLOW}]Reflection:[/] {status}")
    elif arg in ("--off", "off"):
        state.reflect_depth = 0
        console.print(f"[{YELLOW}]Reflection off.[/]")
    elif arg in ("--on", "on"):
        state.reflect_depth = 1
        console.print(f"[{YELLOW}]Reflection on[/] [muted](depth=1)[/]")
    else:
        try:
            state.reflect_depth = max(0, int(arg))
            console.print(f"[{YELLOW}]Reflection on[/] [muted](depth={state.reflect_depth})[/]")
        except ValueError:
            print_error("Usage: /config reflect [on | off | <depth 1-3>]")
    return True


def _handle_verbose(args: list[str], ctx: CommandContext) -> bool:
    state = ctx.state
    if state is None:
        return True
    arg = args[0] if args else ""
    if not arg:
        console.print(f"[{YELLOW}]Verbose:[/] {'on' if state.verbose else 'off'}")
    elif arg in ("--on", "on"):
        state.verbose = True
        console.print(f"[{YELLOW}]Verbose on.[/]")
    elif arg in ("--off", "off"):
        state.verbose = False
        console.print(f"[{YELLOW}]Verbose off.[/]")
    else:
        print_error("Usage: /config verbose [on | off]")
    return True


def _handle_debug(args: list[str], ctx: CommandContext) -> bool:
    state = ctx.state
    if state is None:
        return True
    arg = args[0] if args else ""
    if not arg:
        console.print(f"[{YELLOW}]Debug:[/] {'on' if state.debug else 'off'}")
    elif arg in ("--on", "on"):
        state.debug = True
        console.print(
            f"[{YELLOW}]Debug on.[/] [muted]System prompt and other debug info will be printed each turn.[/]"
        )
    elif arg in ("--off", "off"):
        state.debug = False
        console.print(f"[{YELLOW}]Debug off.[/]")
    else:
        print_error("Usage: /config debug [on | off]")
    return True


def _handle_markdown(args: list[str], ctx: CommandContext) -> bool:
    state = ctx.state
    if state is None:
        return True
    arg = args[0] if args else ""
    if not arg:
        console.print(f"[{YELLOW}]Markdown rendering:[/] {'on' if state.markdown_enabled else 'off'}")
    elif arg in ("on", "--on"):
        state.markdown_enabled = True
        console.print(f"[{YELLOW}]Markdown rendering on.[/]")
    elif arg in ("off", "--off"):
        state.markdown_enabled = False
        console.print(f"[{YELLOW}]Markdown rendering off.[/] [muted](plain text streaming)[/]")
    else:
        print_error("Usage: /config markdown [on | off]")
    return True


def _handle_agents(args: list[str], ctx: CommandContext) -> bool:
    state = ctx.state
    if state is None:
        return True
    arg = args[0] if args else ""
    if not arg:
        console.print(f"[{YELLOW}]Subagents:[/] {'on' if state.agents_enabled else 'off'}")
    elif arg in ("on", "--on"):
        state.agents_enabled = True
        console.print(f"[{YELLOW}]Subagents on.[/]")
    elif arg in ("off", "--off"):
        state.agents_enabled = False
        console.print(f"[{YELLOW}]Subagents off.[/] [muted](spawn_agent removed from tool list)[/]")
    else:
        print_error("Usage: /config agents [on | off]")
    return True


def _handle_approval(args: list[str], ctx: CommandContext) -> bool:
    state = ctx.state
    if state is None:
        return True
    arg = args[0] if args else ""
    if not arg:
        console.print(f"[{YELLOW}]Approval mode:[/] {state.approval_mode}")
    elif arg == "edits":
        state.approval_mode = "edits"
        print_mode_toggle("edits", True)
    elif arg == "yolo":
        state.approval_mode = "yolo"
        print_mode_toggle("yolo", True)
    elif arg == "off":
        state.approval_mode = "off"
        console.print(f"[{YELLOW}]Approval mode:[/] off")
    else:
        print_error("Usage: /config approval [off | edits | yolo]")
    return True


def _show_config_help() -> None:
    console.print(f"\n[bold {YELLOW}]/config subcommands:[/]")
    subcommands = [
        ("show",     "Show effective configuration"),
        ("reflect",  "Self-refine depth — /config reflect [on | off | <N>]"),
        ("verbose",  "Verbose critique output — /config verbose [on | off]"),
        ("debug",    "Debug mode — /config debug [on | off]"),
        ("markdown", "Markdown rendering — /config markdown [on | off]"),
        ("approval", "Tool approval mode — /config approval [off | edits | yolo]"),
        ("agents",   "Subagent spawning — /config agents [on | off]"),
    ]
    for sub, desc in subcommands:
        console.print(f"  [bold {YELLOW}]/config {sub:<10}[/]  {desc}")
    console.print()


# ── Router ────────────────────────────────────────────────────────────────────

_SUBCOMMANDS: dict[str, _ConfigHandler] = {
    "show":     _handle_show,
    "reflect":  _handle_reflect,
    "verbose":  _handle_verbose,
    "debug":    _handle_debug,
    "markdown": _handle_markdown,
    "approval": _handle_approval,
    "agents":   _handle_agents,
}


def handle_config_command(raw: str, ctx: CommandContext) -> bool:
    """Route /config [subcommand] [args] to the appropriate handler."""
    parts = raw.strip().split(maxsplit=2)
    sub  = parts[1].lower() if len(parts) > 1 else "show"
    args = parts[2].split() if len(parts) > 2 else []

    handler = _SUBCOMMANDS.get(sub)
    if handler is None:
        _show_config_help()
        return True
    return handler(args, ctx)
