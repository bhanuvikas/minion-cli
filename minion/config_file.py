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

    [memory]
    enabled                  = true
    top_k                    = 5
    similarity_threshold     = 0.70
    consolidation_threshold  = 0.70
    extraction_trigger       = "substantial"   # "substantial" | "every_5" | "manual" | "always"
    extraction_min_words     = 50

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


_CONFIG_PATH = Path.home() / ".minion" / "config.toml"

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


@dataclass
class MemoryFileConfig:
    enabled: bool = True
    top_k: int = 5
    similarity_threshold: float = 0.70
    consolidation_threshold: float = 0.70
    extraction_trigger: str = "substantial"
    extraction_min_words: int = 50


@dataclass
class A2AConfig:
    auth_token: str = ""


@dataclass
class TracingConfig:
    enabled: bool = True


@dataclass
class MinionConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    memory: MemoryFileConfig = field(default_factory=MemoryFileConfig)
    a2a: A2AConfig = field(default_factory=A2AConfig)
    tracing: TracingConfig = field(default_factory=TracingConfig)


def _get(data: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dict keys, returning default if any key is missing."""
    node = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def load_config(path: Path | None = None) -> MinionConfig:
    """Load MinionConfig from config.toml.

    Returns defaults if the file doesn't exist or is unreadable.
    Unknown keys are silently ignored (forward-compatible).
    Invalid values are ignored and the default is used.
    """
    config_path = path or _CONFIG_PATH
    raw: dict = {}

    if config_path.exists():
        try:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            pass  # fall back to defaults silently

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
    a2a_raw = raw.get("a2a", {})
    tracing_raw = raw.get("tracing", {})

    extraction_trigger = _str(memory_raw.get("extraction_trigger"), "substantial")
    if extraction_trigger not in _VALID_EXTRACTION_TRIGGERS:
        extraction_trigger = "substantial"

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
        ),
        memory=MemoryFileConfig(
            enabled=_bool(memory_raw.get("enabled"), True),
            top_k=_int(memory_raw.get("top_k"), 5),
            similarity_threshold=_float(memory_raw.get("similarity_threshold"), 0.70),
            consolidation_threshold=_float(memory_raw.get("consolidation_threshold"), 0.70),
            extraction_trigger=extraction_trigger,
            extraction_min_words=_int(memory_raw.get("extraction_min_words"), 50),
        ),
        a2a=A2AConfig(
            auth_token=_str(a2a_raw.get("auth_token"), ""),
        ),
        tracing=TracingConfig(
            enabled=_bool(tracing_raw.get("enabled"), True),
        ),
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
        "",
        "[memory]",
        f"  enabled                 = {cfg.memory.enabled}",
        f"  top_k                   = {cfg.memory.top_k}",
        f"  similarity_threshold    = {cfg.memory.similarity_threshold}",
        f"  consolidation_threshold = {cfg.memory.consolidation_threshold}",
        f"  extraction_trigger      = {cfg.memory.extraction_trigger}",
        f"  extraction_min_words    = {cfg.memory.extraction_min_words}",
        "",
        "[a2a]",
        f"  auth_token = {'(set)' if cfg.a2a.auth_token else '(not set)'}",
        "",
        "[tracing]",
        f"  enabled = {cfg.tracing.enabled}",
    ]
    return "\n".join(lines)
