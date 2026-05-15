"""config/file.py — persistent user preferences from config.toml.

Loading priority (lowest → highest):
    hardcoded defaults → ~/.minion/config.toml → <cwd>/.minion/config.toml → session (ReplState)

Only the two config.toml files are handled here.  .env is loaded by cli.py
via python-dotenv before this module is imported.  CLI flags override in cli.py.

Schema (all sections optional — missing keys fall back to defaults):

    [llm]
    provider = "anthropic"
    model    = "claude-sonnet-4-6"

    [agent]
    reflect_depth       = 0
    verbose             = false
    debug               = false
    agents_enabled      = true
    max_subagent_depth  = 2
    approval_mode       = "off"          # "off" | "edits" | "yolo"
    markdown_enabled    = true

    [memory]
    enabled                  = true
    top_k                    = 5
    similarity_threshold     = 0.70
    consolidation_threshold  = 0.70
    extraction_trigger       = "substantial"   # "substantial" | "every_5" | "manual" | "always"
    extraction_min_words     = 50

    [context]
    auto_compact = true   # automatically compact on input-token rate limit (429)

    [a2a]
    auth_token = ""

    [tracing]
    enabled = true

    [hooks]
    enabled           = true
    builtin_minion_md = true
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


_GLOBAL_CONFIG_PATH = Path.home() / ".minion" / "config.toml"

_VALID_EXTRACTION_TRIGGERS = {"substantial", "every_5", "manual", "always"}


@dataclass
class LLMConfig:
    provider: Optional[str] = None   # None = auto-detect from env
    model: Optional[str] = None      # None = provider default


@dataclass
class AgentConfig:
    reflect_depth: int = 0
    verbose: bool = False
    debug: bool = False
    agents_enabled: bool = True
    max_subagent_depth: int = 2
    approval_mode: str = "off"       # "off" | "edits" | "yolo"
    markdown_enabled: bool = True    # render LLM responses as markdown


@dataclass
class MemoryFileConfig:
    enabled: bool = True
    top_k: int = 5
    similarity_threshold: float = 0.70
    consolidation_threshold: float = 0.70
    extraction_trigger: str = "substantial"
    extraction_min_words: int = 50


@dataclass
class ContextConfig:
    auto_compact: bool = True


@dataclass
class A2AConfig:
    auth_token: str = ""


@dataclass
class TracingConfig:
    enabled: bool = True


@dataclass
class HookDefinition:
    event: str              # "PreToolUse" | "PostToolUse" | "SessionStart" | ...
    command: str            # shell command; run in cwd at fire time
    tool: Optional[str] = None   # tool name matcher; None = all tools
    timeout: int = 30
    blocking: Optional[bool] = None  # None = event-default (True for Pre, False for Post)


@dataclass
class HooksBuiltinConfig:
    enabled: bool = True             # master on/off switch for all hooks
    builtin_minion_md: bool = True   # MINION.md staleness tip after write/edit


@dataclass
class MinionConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    memory: MemoryFileConfig = field(default_factory=MemoryFileConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    a2a: A2AConfig = field(default_factory=A2AConfig)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    hooks_config: HooksBuiltinConfig = field(default_factory=HooksBuiltinConfig)
    hooks: list = field(default_factory=list)  # list[HookDefinition]


def _get(data: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dict keys, returning default if any key is missing."""
    node = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _load_toml(path: Path) -> dict:
    """Load a TOML file, returning an empty dict on any error."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, FileNotFoundError):
        return {}


def _merge(base: dict, override: dict) -> dict:
    """Shallow-merge two TOML dicts: for each section, override keys win."""
    merged = dict(base)
    for section, values in override.items():
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section] = {**merged[section], **values}
        else:
            merged[section] = values
    return merged


def load_config(path: Path | None = None, cwd: Path | None = None) -> MinionConfig:
    """Load MinionConfig from config.toml with two-tier loading.

    Loading priority (lowest → highest):
        hardcoded defaults → ~/.minion/config.toml → <cwd>/.minion/config.toml

    Returns defaults if no files exist or are unreadable.
    Unknown keys are silently ignored (forward-compatible).
    Invalid values are ignored and the default is used.
    """
    global_path = path or _GLOBAL_CONFIG_PATH
    raw_global: dict = _load_toml(global_path)
    raw_project: dict = {}

    if cwd is not None:
        project_path = Path(cwd) / ".minion" / "config.toml"
        if project_path != global_path:
            raw_project = _load_toml(project_path)

    raw = _merge(raw_global, raw_project)

    def _bool(val: Any, default: bool) -> bool:
        return bool(val) if isinstance(val, bool) else default

    def _int(val: Any, default: int) -> int:
        return int(val) if isinstance(val, int) else default

    def _float(val: Any, default: float) -> float:
        return float(val) if isinstance(val, (int, float)) else default

    def _str(val: Any, default: str) -> str:
        return str(val) if isinstance(val, str) else default

    llm_raw = raw.get("llm", {})
    agent_raw = raw.get("agent", {})
    memory_raw = raw.get("memory", {})
    context_raw = raw.get("context", {})
    a2a_raw = raw.get("a2a", {})
    tracing_raw = raw.get("tracing", {})

    extraction_trigger = _str(memory_raw.get("extraction_trigger"), "substantial")
    if extraction_trigger not in _VALID_EXTRACTION_TRIGGERS:
        extraction_trigger = "substantial"

    # Hooks: builtin settings from merged config; user hook lists concatenated from both
    hooks_merged = raw.get("hooks", {})
    if not isinstance(hooks_merged, dict):
        hooks_merged = {}
    hooks_builtin_cfg = HooksBuiltinConfig(
        enabled=_bool(hooks_merged.get("enabled"), True),
        builtin_minion_md=_bool(hooks_merged.get("builtin_minion_md"), True),
    )

    def _parse_hook_list(data: dict) -> list:
        h_raw = data.get("hooks", {})
        if not isinstance(h_raw, dict):
            return []
        return [
            HookDefinition(
                event=h["event"],
                command=h["command"],
                tool=h.get("tool") or None,
                timeout=_int(h.get("timeout"), 30),
                blocking=h.get("blocking"),
            )
            for h in h_raw.get("user", [])
            if isinstance(h, dict) and "event" in h and "command" in h
        ]

    hooks = _parse_hook_list(raw_global) + _parse_hook_list(raw_project)

    return MinionConfig(
        llm=LLMConfig(
            provider=_str(llm_raw.get("provider"), None) if "provider" in llm_raw else None,  # type: ignore[arg-type]
            model=_str(llm_raw.get("model"), None) if "model" in llm_raw else None,  # type: ignore[arg-type]
        ),
        agent=AgentConfig(
            reflect_depth=_int(agent_raw.get("reflect_depth"), 0),
            verbose=_bool(agent_raw.get("verbose"), False),
            debug=_bool(agent_raw.get("debug"), False),
            agents_enabled=_bool(agent_raw.get("agents_enabled"), True),
            max_subagent_depth=_int(agent_raw.get("max_subagent_depth"), 2),
            approval_mode=_str(agent_raw.get("approval_mode"), "off") if agent_raw.get("approval_mode") in ("off", "edits", "yolo") else "off",
            markdown_enabled=_bool(agent_raw.get("markdown_enabled"), True),
        ),
        memory=MemoryFileConfig(
            enabled=_bool(memory_raw.get("enabled"), True),
            top_k=_int(memory_raw.get("top_k"), 5),
            similarity_threshold=_float(memory_raw.get("similarity_threshold"), 0.70),
            consolidation_threshold=_float(memory_raw.get("consolidation_threshold"), 0.70),
            extraction_trigger=extraction_trigger,
            extraction_min_words=_int(memory_raw.get("extraction_min_words"), 50),
        ),
        context=ContextConfig(
            auto_compact=_bool(context_raw.get("auto_compact"), True),
        ),
        a2a=A2AConfig(
            auth_token=_str(a2a_raw.get("auth_token"), ""),
        ),
        tracing=TracingConfig(
            enabled=_bool(tracing_raw.get("enabled"), True),
        ),
        hooks_config=hooks_builtin_cfg,
        hooks=hooks,
    )


def format_config(cfg: MinionConfig) -> str:
    """Return a human-readable summary of the effective config."""
    import os as _os
    env_provider = _os.getenv("MINION_PROVIDER") or cfg.llm.provider
    env_model = _os.getenv("MINION_MODEL") or cfg.llm.model
    lines = [
        "[llm]",
        f"  provider = {env_provider or '(auto)'}",
        f"  model    = {env_model or '(provider default)'}",
        "",
        "[agent]",
        f"  reflect_depth      = {cfg.agent.reflect_depth}",
        f"  verbose            = {cfg.agent.verbose}",
        f"  debug              = {cfg.agent.debug}",
        f"  agents_enabled     = {cfg.agent.agents_enabled}",
        f"  max_subagent_depth = {cfg.agent.max_subagent_depth}",
        f"  approval_mode      = {cfg.agent.approval_mode}",
        f"  markdown_enabled   = {cfg.agent.markdown_enabled}",
        "",
        "[memory]",
        f"  enabled                 = {cfg.memory.enabled}",
        f"  top_k                   = {cfg.memory.top_k}",
        f"  similarity_threshold    = {cfg.memory.similarity_threshold}",
        f"  consolidation_threshold = {cfg.memory.consolidation_threshold}",
        f"  extraction_trigger      = {cfg.memory.extraction_trigger}",
        f"  extraction_min_words    = {cfg.memory.extraction_min_words}",
        "",
        "[context]",
        f"  auto_compact = {cfg.context.auto_compact}",
        "",
        "[a2a]",
        f"  auth_token = {'(set)' if cfg.a2a.auth_token else '(not set)'}",
        "",
        "[tracing]",
        f"  enabled = {cfg.tracing.enabled}",
    ]
    return "\n".join(lines)


def load_config_levels(cwd: Path | None = None) -> tuple[dict, dict]:
    """Return raw (un-merged) TOML dicts for (global, project) config files.

    Used for source-tag display — neither dict is merged with the other.
    Either can be an empty dict if the corresponding file doesn't exist.
    """
    global_raw = _load_toml(_GLOBAL_CONFIG_PATH)
    project_raw: dict = {}
    if cwd is not None:
        project_raw = _load_toml(Path(cwd) / ".minion" / "config.toml")
    return global_raw, project_raw


def create_project_config(cwd: Path, base_raw: dict | None = None) -> Path:
    """Create .minion/config.toml with all settings written at their effective values.

    base_raw — raw TOML dict from the global config (used as the baseline so
    user's root-level preferences carry over).  Falls back to hardcoded
    defaults for any missing key.  auth_token is always written as empty for
    security — the user sets it explicitly.

    Returns the path of the created file.
    """
    b = base_raw or {}

    def _v(section: str, key: str, default: Any) -> Any:
        return b.get(section, {}).get(key, default)

    def _tv(val: Any) -> str:
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, str):
            return f'"{val}"'
        return str(val)

    llm = b.get("llm", {})
    provider_line = f'provider = "{llm["provider"]}"' if "provider" in llm else '# provider = "anthropic"  # anthropic | openai | openrouter'
    model_line    = f'model = "{llm["model"]}"'        if "model"    in llm else '# model = "claude-sonnet-4-6"'

    content = (
        "# minion project configuration\n"
        "# Priority: session (ephemeral) > this file > ~/.minion/config.toml > defaults\n"
        "# Edit this file to persist settings. Restart minion for changes to take effect.\n"
        "\n"
        "[llm]\n"
        f"{provider_line}\n"
        f"{model_line}\n"
        "\n"
        "[agent]\n"
        f"reflect_depth = {_v('agent', 'reflect_depth', 0)}\n"
        f"verbose = {_tv(_v('agent', 'verbose', False))}\n"
        f"debug = {_tv(_v('agent', 'debug', False))}\n"
        f"agents_enabled = {_tv(_v('agent', 'agents_enabled', True))}\n"
        f"max_subagent_depth = {_v('agent', 'max_subagent_depth', 2)}\n"
        f"approval_mode = {_tv(_v('agent', 'approval_mode', 'off'))}\n"
        f"markdown_enabled = {_tv(_v('agent', 'markdown_enabled', True))}\n"
        "\n"
        "[memory]\n"
        f"enabled = {_tv(_v('memory', 'enabled', True))}\n"
        f"top_k = {_v('memory', 'top_k', 5)}\n"
        f"similarity_threshold = {_v('memory', 'similarity_threshold', 0.70)}\n"
        f"consolidation_threshold = {_v('memory', 'consolidation_threshold', 0.70)}\n"
        f"extraction_trigger = {_tv(_v('memory', 'extraction_trigger', 'substantial'))}\n"
        f"extraction_min_words = {_v('memory', 'extraction_min_words', 50)}\n"
        "\n"
        "[context]\n"
        f"auto_compact = {_tv(_v('context', 'auto_compact', True))}\n"
        "\n"
        "[a2a]\n"
        'auth_token = ""\n'
        "\n"
        "[tracing]\n"
        f"enabled = {_tv(_v('tracing', 'enabled', True))}\n"
        "\n"
        "[hooks]\n"
        f"enabled = {_tv(_v('hooks', 'enabled', True))}\n"
        f"builtin_minion_md = {_tv(_v('hooks', 'builtin_minion_md', True))}\n"
    )

    project_dir = Path(cwd) / ".minion"
    project_dir.mkdir(parents=True, exist_ok=True)
    config_path = project_dir / "config.toml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def set_project_config_value(cwd: Path, section: str, key: str, value: Any) -> None:
    """Update or insert a single key in .minion/config.toml without altering other keys.

    Creates the project config from root-seeded defaults if it doesn't exist.
    Overwrites the matched line (even if commented out). Appends a new [section]
    block at the end if the section is missing entirely.
    """
    project_path = Path(cwd) / ".minion" / "config.toml"
    if not project_path.exists():
        global_raw, _ = load_config_levels()
        create_project_config(cwd, global_raw)

    if isinstance(value, bool):
        toml_val = "true" if value else "false"
    elif isinstance(value, str):
        toml_val = f'"{value}"'
    else:
        toml_val = str(value)

    lines = project_path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    updated = False
    result: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Detect entering the target section
        if stripped == f"[{section}]":
            in_section = True
        elif stripped.startswith("[") and not stripped.startswith("[#") and in_section and not updated:
            # Leaving section without finding the key — insert before next header
            result.append(f"{key} = {toml_val}\n")
            updated = True
            in_section = False

        # Replace the key line within the target section (handles commented-out lines too)
        if in_section and not updated and re.match(rf"^#?\s*{re.escape(key)}\s*=", stripped):
            result.append(f"{key} = {toml_val}\n")
            updated = True
            continue

        result.append(line)

    if not updated:
        # Section not found — append a new section block
        result.append(f"\n[{section}]\n{key} = {toml_val}\n")

    project_path.write_text("".join(result), encoding="utf-8")
