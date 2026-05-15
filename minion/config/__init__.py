"""Config package — user preferences, interactive configuration, and setup wizard.

Re-exports everything so callers can use `from .config import X` regardless
of which submodule X lives in.
"""

from .interactive import (
    MINION_STYLE,
    PROVIDER_KEY_MAP,
    PROVIDERS,
    update_env_values,
    run_model_config,
)
from .file import (
    load_config,
    load_config_levels,
    create_project_config,
    set_project_config_value,
    format_config,
    MinionConfig,
    LLMConfig,
    AgentConfig,
    MemoryFileConfig,
    ContextConfig,
    A2AConfig,
    TracingConfig,
    HookDefinition,
    HooksBuiltinConfig,
)
from .wizard import run_setup_wizard, _MINION_STYLE

__all__ = [
    "MINION_STYLE",
    "PROVIDER_KEY_MAP",
    "PROVIDERS",
    "update_env_values",
    "run_model_config",
    "load_config",
    "load_config_levels",
    "create_project_config",
    "set_project_config_value",
    "format_config",
    "MinionConfig",
    "LLMConfig",
    "AgentConfig",
    "MemoryFileConfig",
    "ContextConfig",
    "A2AConfig",
    "TracingConfig",
    "HookDefinition",
    "HooksBuiltinConfig",
    "run_setup_wizard",
    "_MINION_STYLE",
]
