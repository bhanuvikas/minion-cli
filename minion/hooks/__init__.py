"""minion.hooks — lifecycle hooks for the agent loop.

Public API:
  HookRunner    — dispatches events, accumulates tips
  HookRegistry  — factory: HookRegistry.from_config(cfg) → HookRunner
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
from .registry import HookRegistry
from .result import HookResult
from .runner import HookRunner

__all__ = [
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
