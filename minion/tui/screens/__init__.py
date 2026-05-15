"""TUI modal screens — one module per slash command wizard."""

from .completion_setup import CompletionSetupScreen
from .config_panel import ConfigPanelScreen
from .model_config import ModelConfigScreen

__all__ = ["CompletionSetupScreen", "ConfigPanelScreen", "ModelConfigScreen"]
