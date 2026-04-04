"""Typed reference documentation for trace event data shapes.

Each dataclass here documents the kwargs expected by Tracer.emit() for the
corresponding event_type. Not enforced at runtime — emit() accepts **kwargs
and applies truncation internally. Use dataclasses.asdict() in tests to
build correctly-shaped payloads.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class SessionStartData:
    model: str
    system_prompt: str       # truncated to 500 chars at emit()
    cwd: str


@dataclass
class UserTurnData:
    text: str


@dataclass
class ContextInjectData:
    memory_count: int
    token_estimate: int
    memories: list[str]      # truncated to top-3, 80 chars each at emit()


@dataclass
class LLMRequestData:
    message_count: int
    tool_names: list[str]
    model: str
    estimated_input_tokens: int


@dataclass
class LLMResponseData:
    response: str            # truncated to 1000 chars at emit()
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    latency_ms: int


@dataclass
class ToolCallData:
    tool_name: str
    inputs: dict


@dataclass
class ToolResultData:
    tool_name: str
    output: str              # truncated to 500 chars at emit()
    success: bool
    error: Optional[str] = None


@dataclass
class MemoryRetrieveData:
    query: str
    num_retrieved: int
    memories: list[str]      # truncated to top-3, 80 chars each at emit()


@dataclass
class MemoryStoreData:
    content: str
    type: str
    category: str
    scope: str


@dataclass
class MemorySkipData:
    reason: str


@dataclass
class ReflectionStartData:
    initial_response_length: int
    reflection_enabled: bool


@dataclass
class ReflectionCritiqueData:
    score: int
    critique: str            # truncated to 500 chars at emit()


@dataclass
class ReflectionRevisionData:
    was_revised: bool
    new_response_length: int


@dataclass
class SessionEndData:
    total_turns: int
    total_input_tokens: int
    total_output_tokens: int
    total_tool_calls: int
    duration_seconds: float


# ─── Plan lifecycle events ────────────────────────────────────────────────────

@dataclass
class PlanStartData:
    goal: str


@dataclass
class PlanGeneratedData:
    plan_path: str
    plan_length_chars: int
    generation_time_ms: int


@dataclass
class PlanRefinedData:
    plan_path: str
    feedback: str
    refinement_round: int


@dataclass
class PlanExecuteStartData:
    plan_path: str
    plan_length_chars: int


@dataclass
class PlanCompleteData:
    plan_path: str
