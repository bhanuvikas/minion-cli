"""minion.hooks — lifecycle hooks for the agent loop.

Public API:
  HookManifest  — dataclass for a single YAML-defined hook
  HookRunner    — dispatches events, accumulates tips
  HookRegistry  — 3-tier YAML loader: HookRegistry.load(cwd, cfg) → HookRegistry
  Events        — PreToolUseEvent, PostToolUseEvent, SessionStartEvent,
                  SessionEndEvent, UserPromptSubmitEvent, StopTurnEvent
"""

from .events import (
    HookEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    SessionEndEvent,
    SessionStartEvent,
    StopTurnEvent,
    UserPromptSubmitEvent,
)
from .manifest import HookManifest
from .registry import HookRegistry
from .result import HookResult
from .runner import HookRunner

__all__ = [
    "HookManifest",
    "HookRunner",
    "HookRegistry",
    "HookResult",
    "HookEvent",
    "PreToolUseEvent",
    "PostToolUseEvent",
    "SessionStartEvent",
    "SessionEndEvent",
    "UserPromptSubmitEvent",
    "StopTurnEvent",
]
