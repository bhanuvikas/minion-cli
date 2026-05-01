"""A2A protocol data model — Task, Artifact, AgentCard.

Spec-compliant implementation of the Agent-to-Agent (A2A) protocol:
- Artifacts use {parts: [{type, text}]} wire format
- Task status uses {state, timestamp} object
- AgentCard includes defaultInputModes/defaultOutputModes and full capabilities
- Message uses {role, parts: [{type, text}]} wire format
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_text_from_message(msg) -> str:
    """Extract plain text from a spec Message object or bare string."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        parts = msg.get("parts", [])
        if parts:
            texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
            return "\n".join(t for t in texts if t) or msg.get("text", "")
        return msg.get("text", "")
    return str(msg)


def _make_message(text: str, role: str = "user") -> dict:
    """Build a spec-compliant Message object."""
    return {
        "role": role,
        "parts": [{"type": "text", "text": text}],
        "messageId": str(uuid.uuid4()),
    }


class TaskStatus(str, Enum):
    SUBMITTED      = "submitted"
    WORKING        = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED      = "completed"
    FAILED         = "failed"
    CANCELED       = "canceled"


@dataclass
class Artifact:
    """Text result produced by a remote agent for a task.

    Wire format (spec): {"artifactId": "...", "parts": [{"type": "text", "text": "..."}]}
    """
    text: str
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "artifactId": self.artifact_id,
            "parts": [{"type": "text", "text": self.text}],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Artifact":
        # Handle spec format: {parts: [{type, text}]}
        parts = data.get("parts", [])
        if parts:
            text = "\n".join(
                p.get("text", "") for p in parts if p.get("type") == "text"
            )
        else:
            # Fallback for older non-spec format
            text = data.get("text", "")
        return cls(text=text, artifact_id=data.get("artifactId", str(uuid.uuid4())))


@dataclass
class Task:
    """Unit of work in the A2A protocol.

    Wire format (spec): status is {state, timestamp}, not a bare string.
    Lifecycle: submitted → working → completed / failed / canceled
    contextId groups related tasks into a session (multi-turn conversation).
    """
    id: str
    status: TaskStatus
    input_message: str
    artifacts: list[Artifact] = field(default_factory=list)
    error: Optional[str] = None
    created_at: str = field(default_factory=_iso_now)
    context_id: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "status": {
                "state": self.status.value,
                "timestamp": _iso_now(),
            },
            "input": _make_message(self.input_message),
        }
        if self.context_id is not None:
            d["contextId"] = self.context_id
        if self.artifacts:
            d["artifacts"] = [a.to_dict() for a in self.artifacts]
        if self.error is not None:
            d["error"] = self.error
        return d

    def status_event(self, final: bool = False) -> dict:
        """Build a spec TaskStatusUpdateEvent for SSE streaming."""
        d: dict = {
            "id": self.id,
            "status": {
                "state": self.status.value,
                "timestamp": _iso_now(),
            },
            "final": final,
        }
        if self.context_id is not None:
            d["contextId"] = self.context_id
        return d

    def artifact_event(self, artifact: Artifact, final: bool = True) -> dict:
        """Build a spec TaskArtifactUpdateEvent for SSE streaming."""
        d: dict = {
            "id": self.id,
            "artifact": artifact.to_dict(),
            "final": final,
        }
        if self.context_id is not None:
            d["contextId"] = self.context_id
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        # Parse status — handle both spec {state, timestamp} and legacy bare string
        raw_status = data.get("status", "submitted")
        if isinstance(raw_status, dict):
            status_str = raw_status.get("state", "submitted")
        else:
            status_str = raw_status
        try:
            status = TaskStatus(status_str)
        except ValueError:
            status = TaskStatus.FAILED

        artifacts = [Artifact.from_dict(a) for a in data.get("artifacts", [])]

        # Parse input — handle both spec Message and legacy {message: str}
        input_data = data.get("input", {})
        if isinstance(input_data, dict):
            input_message = _extract_text_from_message(input_data)
        else:
            input_message = str(input_data)

        return cls(
            id=data.get("id", ""),
            status=status,
            input_message=input_message,
            artifacts=artifacts,
            error=data.get("error"),
            context_id=data.get("contextId"),
        )


@dataclass
class AgentCard:
    """Capability advertisement for an A2A agent.

    Served at /.well-known/agent.json.
    """
    name: str
    description: str
    url: str
    version: str
    capabilities: dict = field(default_factory=lambda: {
        "streaming": True,
        "pushNotifications": False,
        "stateTransitionHistory": False,
    })
    skills: list[dict] = field(default_factory=list)
    default_input_modes: list[str] = field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = field(default_factory=lambda: ["text"])
    # Optional spec fields — omitted from wire format when None
    provider: Optional[dict] = None          # {"organization": "...", "url": "..."}
    authentication: Optional[dict] = None    # {"schemes": ["Bearer"]}

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": self.capabilities,
            "defaultInputModes": self.default_input_modes,
            "defaultOutputModes": self.default_output_modes,
            "skills": self.skills,
        }
        if self.provider is not None:
            d["provider"] = self.provider
        if self.authentication is not None:
            d["authentication"] = self.authentication
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCard":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            url=data.get("url", ""),
            version=data.get("version", ""),
            capabilities=data.get("capabilities", {
                "streaming": True,
                "pushNotifications": False,
                "stateTransitionHistory": False,
            }),
            skills=data.get("skills", []),
            default_input_modes=data.get("defaultInputModes", ["text"]),
            default_output_modes=data.get("defaultOutputModes", ["text"]),
            provider=data.get("provider"),
            authentication=data.get("authentication"),
        )
