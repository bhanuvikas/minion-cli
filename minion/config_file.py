"""config_file.py — persistent user preferences from ~/.minion/config.toml.

Loading priority (lowest → highest):
    hardcoded defaults → ~/.minion/config.toml → .env file → CLI flags

Only ~/.minion/config.toml is handled here. .env is loaded by cli.py via
python-dotenv before this module is imported. CLI flags override in cli.py.

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
    approval_mode       = "off"   # "off" | "edits" | "yolo"

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
"""

from __future__ import annotations

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
    approval_mode: str = "off"   # "off" | "edits" | "yolo"


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
            provider=_str(llm_raw.get("provider"), None) if "provider" in llm_raw else None,
            model=_str(llm_raw.get("model"), None) if "model" in llm_raw else None,
        ),
        agent=AgentConfig(
            reflect_depth=_int(agent_raw.get("reflect_depth"), 0),
            verbose=_bool(agent_raw.get("verbose"), False),
            debug=_bool(agent_raw.get("debug"), False),
            agents_enabled=_bool(agent_raw.get("agents_enabled"), True),
            max_subagent_depth=_int(agent_raw.get("max_subagent_depth"), 2),
            approval_mode=_str(agent_raw.get("approval_mode"), "off") if agent_raw.get("approval_mode") in ("off", "edits", "yolo") else "off",
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
    lines = [
        "[llm]",
        f"  provider = {cfg.llm.provider or '(auto)'}",
        f"  model    = {cfg.llm.model or '(provider default)'}",
        "",
        "[agent]",
        f"  reflect_depth      = {cfg.agent.reflect_depth}",
        f"  verbose            = {cfg.agent.verbose}",
        f"  debug              = {cfg.agent.debug}",
        f"  agents_enabled     = {cfg.agent.agents_enabled}",
        f"  max_subagent_depth = {cfg.agent.max_subagent_depth}",
        f"  approval_mode      = {cfg.agent.approval_mode}",
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
