"""Hook event dataclasses.

Each event type corresponds to a lifecycle point in the agent loop.
All events are frozen dataclasses — handlers receive them read-only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar, Union


@dataclass(frozen=True)
class PreToolUseEvent:
    event_name: ClassVar[str] = "PreToolUse"
    session_id: str
    cwd: Path
    tool_name: str
    tool_input: dict

    def to_json_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "event": "PreToolUse",
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
        }


@dataclass(frozen=True)
class PostToolUseEvent:
    event_name: ClassVar[str] = "PostToolUse"
    session_id: str
    cwd: Path
    tool_name: str
    tool_input: dict
    tool_result: str
    tool_success: bool

    def to_json_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "event": "PostToolUse",
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "tool_result": self.tool_result,
            "tool_success": self.tool_success,
        }


@dataclass(frozen=True)
class SessionStartEvent:
    event_name: ClassVar[str] = "SessionStart"
    session_id: str
    cwd: Path

    def to_json_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "event": "SessionStart",
        }


@dataclass(frozen=True)
class SessionEndEvent:
    event_name: ClassVar[str] = "SessionEnd"
    session_id: str
    cwd: Path

    def to_json_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "event": "SessionEnd",
        }


@dataclass(frozen=True)
class UserPromptSubmitEvent:
    event_name: ClassVar[str] = "UserPromptSubmit"
    session_id: str
    cwd: Path
    prompt: str

    def to_json_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "event": "UserPromptSubmit",
            "prompt": self.prompt,
        }


@dataclass(frozen=True)
class StopTurnEvent:
    event_name: ClassVar[str] = "StopTurn"
    session_id: str
    cwd: Path
    response_text: str

    def to_json_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "event": "StopTurn",
            "response_text": self.response_text,
        }


HookEvent = Union[
    PreToolUseEvent,
    PostToolUseEvent,
    SessionStartEvent,
    SessionEndEvent,
    UserPromptSubmitEvent,
    StopTurnEvent,
]
