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
class LLMErrorData:
    error: str               # full error message
    latency_ms: int          # time elapsed before the error


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


# ─── Skill lifecycle events ───────────────────────────────────────────────────

@dataclass
class SkillStartData:
    skill_name: str
    arg: str        # user-provided argument string (may be empty)
    source: str     # "builtin" | "user" | "project"


@dataclass
class SkillCompleteData:
    skill_name: str
    arg: str


# ─── MCP lifecycle events ─────────────────────────────────────────────────────

@dataclass
class MCPServerConnectData:
    server_name: str
    command: list[str]      # subprocess argv (no secrets)
    tool_count: int         # 0 on failure
    success: bool
    latency_ms: int
    error: Optional[str] = None


@dataclass
class MCPToolCallData:
    server_name: str        # extracted from namespaced name (before __)
    tool_name: str          # raw tool name (after __)
    namespaced_name: str    # full "server__tool" as seen by executor
    inputs: dict


@dataclass
class MCPToolResultData:
    server_name: str
    tool_name: str
    output: str             # full result text
    success: bool
    latency_ms: int


@dataclass
class MCPErrorData:
    server_name: str        # "" if not server-specific
    tool_name: str          # "" if not tool-specific
    error: str
    context: str = ""       # "connect" | "call" | "shutdown"


@dataclass
class MCPResourceReadData:
    server_name: str
    uri: str                # full resource URI, e.g. "notes://ideas"


@dataclass
class MCPResourceResultData:
    server_name: str
    uri: str
    content_length: int     # byte length of returned content
    success: bool
    latency_ms: int


@dataclass
class MCPPromptGetData:
    server_name: str
    prompt_name: str        # raw (un-namespaced) prompt name
    arguments: dict         # arguments passed to prompts/get


@dataclass
class MCPPromptResultData:
    server_name: str
    prompt_name: str
    injected_text: str      # the text that will be sent to the LLM (truncated to 1000 chars)
    message_count: int      # number of MCP messages in the response
    success: bool
    latency_ms: int


@dataclass
class MCPLogData:
    server_name: str
    level: str              # MCP syslog severity: debug/info/notice/warning/error/critical/alert/emergency
    logger: str             # logger name from server (may be empty string)
    data: str               # the log message text


# ─── Agent lifecycle events ───────────────────────────────────────────────────

@dataclass
class AgentSpawnData:
    role: str               # e.g., "researcher"
    task: str               # full task text passed to the subagent
    depth: int              # nesting depth (1 for direct workers of the orchestrator)


@dataclass
class AgentCompleteData:
    role: str
    task: str               # first 120 chars of the task (for header display)
    result_length: int      # character count of the returned text
    latency_ms: int


@dataclass
class AgentErrorData:
    role: str
    task: str               # first 120 chars of the task
    error: str              # exception message
