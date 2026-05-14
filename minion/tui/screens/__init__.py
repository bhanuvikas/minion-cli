"""TUI modal screens — one module per slash command wizard."""

from .completion_setup import CompletionSetupScreen
from .model_config import ModelConfigScreen

__all__ = ["CompletionSetupScreen", "ModelConfigScreen"]
