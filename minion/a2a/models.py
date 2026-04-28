"""A2A protocol data model — Task, Artifact, AgentCard.

Simplified subset of the Agent-to-Agent (A2A) spec for Phase 11:
- Text-only artifacts (file/structured parts deferred)
- Four task states: submitted → working → completed / failed
- Agent Card with name, description, URL, version, capabilities, and skills
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    SUBMITTED      = "submitted"
    WORKING        = "working"
    INPUT_REQUIRED = "input-required"   # agent needs human approval to proceed
    COMPLETED      = "completed"
    FAILED         = "failed"


@dataclass
class Artifact:
    """Text result produced by a remote agent for a task."""
    text: str

    def to_dict(self) -> dict:
        return {"text": self.text}

    @classmethod
    def from_dict(cls, data: dict) -> "Artifact":
        return cls(text=data.get("text", ""))


@dataclass
class Task:
    """Unit of work in the A2A protocol.

    Lifecycle: submitted → working → completed / failed
    """
    id: str
    status: TaskStatus
    input_message: str
    artifacts: list[Artifact] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "status": self.status.value,
            "input": {"message": self.input_message},
        }
        if self.artifacts:
            d["artifacts"] = [a.to_dict() for a in self.artifacts]
        if self.error is not None:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        status_str = data.get("status", "submitted")
        try:
            status = TaskStatus(status_str)
        except ValueError:
            status = TaskStatus.FAILED

        artifacts = [Artifact.from_dict(a) for a in data.get("artifacts", [])]
        input_data = data.get("input", {})
        input_message = input_data.get("message", "") if isinstance(input_data, dict) else ""

        return cls(
            id=data.get("id", ""),
            status=status,
            input_message=input_message,
            artifacts=artifacts,
            error=data.get("error"),
        )


@dataclass
class AgentCard:
    """Capability advertisement for an A2A agent.

    Served at /.well-known/agent.json. Describes what the agent can do,
    how to reach it, and what streaming capabilities it supports.
    """
    name: str
    description: str
    url: str
    version: str
    capabilities: dict = field(default_factory=lambda: {"streaming": True})
    skills: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": self.capabilities,
            "skills": self.skills,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCard":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            url=data.get("url", ""),
            version=data.get("version", ""),
            capabilities=data.get("capabilities", {"streaming": True}),
            skills=data.get("skills", []),
        )
