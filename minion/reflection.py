"""Self-Refine reflection loop.

Implements the Self-Refine pattern (Madaan et al., 2023): after the agent
produces an initial response, a critic LLM call scores it. If the score is
below the threshold and rounds remain, a refiner LLM call rewrites it based
on the critique. The loop repeats until the response is good enough or the
round limit is hit.

Responsibilities:
  ReflectionConfig  — user-facing settings (depth, threshold)
  CritiqueResult    — structured output from one critique call
  ReflectionResult  — everything produced by a completed reflect() pass
  reflect()         — run the critique/refine loop; return ReflectionResult

Isolation guarantee: all LLM calls use a freshly-built list[Message] that
is never added to the main Conversation. This module has no access to
Conversation and never imports it. Side effects: none beyond return value.
"""

import json
import time as _time
from dataclasses import dataclass, field
from typing import Optional

from .llm.base import LLMClient, LLMResponse, Message
from .tracing import get_tracer


def _ser_msgs(messages: list) -> list:
    """Serialize Message objects to JSON-serializable dicts for tracing."""
    return [{"role": m.role, "content": m.content if isinstance(m.content, str) else str(m.content)}
            for m in messages]

# ─── Constants ────────────────────────────────────────────────────────────────

SCORE_THRESHOLD = 10   # scores >= this exit the loop immediately


# ─── Prompts ─────────────────────────────────────────────────────────────────

CRITIC_SYSTEM_PROMPT = """\
You are a precise critic evaluating an AI assistant's response.

First, classify the response as exactly one of:
  CODE_GENERATION   — the response primarily writes or modifies code
  CODE_EXPLANATION  — the response primarily explains or analyzes code or a concept
  GENERAL           — conversational, factual lookup, or non-technical response

Then score the response 1-10 using these criteria:
  GENERAL:          Score 8 if the answer is clear and relevant, 9-10 if exceptional.
                    Do not penalise GENERAL responses for missing code.
  CODE_GENERATION:  Evaluate: syntactic correctness, edge case handling,
                    idiomatic style for the language, security issues, completeness.
  CODE_EXPLANATION: Evaluate: technical accuracy, clarity, whether the full
                    question is actually answered.

Output EXACTLY this JSON object — no preamble, no trailing text, no code fences:
{"score": <integer 1-10>, "type": "<CODE_GENERATION|CODE_EXPLANATION|GENERAL>", "critique": "<one focused paragraph naming the most important improvement needed, or null if the response is satisfactory>"}"""


REFINER_SYSTEM_PROMPT = """\
You are revising your previous response based on a critique.

Rules:
- Address every point raised in the critique.
- Do not change parts of the response that were not criticised.
- Do not acknowledge or reference the critique in your output.
- Output only the revised response — no preamble, no meta-commentary.
- If the critique is null or "None", return the original response unchanged.
- Preserve the original tone, formatting, and code language."""


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ReflectionConfig:
    """User-controllable settings for the reflection loop.

    depth=0  means reflection is disabled (reflect() returns immediately).
    depth=1  is the default when /reflect on or --reflect is used.
    depth=N  allows up to N refinement rounds before returning.
    threshold defaults to SCORE_THRESHOLD (7).
    """
    depth: int = 0
    threshold: int = SCORE_THRESHOLD


@dataclass
class CritiqueResult:
    """Parsed output from one critique call."""
    score: int
    response_type: str    # "CODE_GENERATION" | "CODE_EXPLANATION" | "GENERAL"
    critique: str         # actionable critique text, or "None"
    raw: str              # unparsed LLM response, for fallback display


@dataclass
class ReflectionResult:
    """Everything produced by a completed reflect() call.

    final_response equals original_response when:
      - depth == 0 (reflection disabled)
      - the first critique scores >= threshold
      - refinement was attempted but produced no textual change

    rounds counts how many critique+refine cycles completed (0 when skipped).
    was_refined is True only if at least one refine call was made and the
    response text actually changed.
    """
    original_response: str
    final_response: str
    rounds: int
    final_score: int
    critiques: list[CritiqueResult] = field(default_factory=list)
    was_refined: bool = False


# ─── Message builders ─────────────────────────────────────────────────────────

def _build_critique_messages(
    prompt: str,
    response: str,
    context_messages: Optional[list[Message]] = None,
) -> list[Message]:
    """Build an isolated message list for a critique call.

    When context_messages is provided (the full conversation history including
    tool results), it is used as-is so the critic has complete context — e.g.
    it can see what read_file returned. A brief evaluation request is appended.

    Without context_messages, falls back to a minimal prompt+response pair.
    This is NOT added to the main conversation.
    """
    if context_messages:
        return list(context_messages) + [
            Message(role="user", content="Evaluate the assistant's last response above.")
        ]
    content = (
        f"Original user prompt:\n{prompt}\n\n"
        f"Your response:\n{response}\n\n"
        f"Evaluate the response above."
    )
    return [Message(role="user", content=content)]


def _build_refine_messages(
    prompt: str,
    response: str,
    critique: CritiqueResult,
    context_messages: Optional[list[Message]] = None,
) -> list[Message]:
    """Build an isolated message list for a refine call.

    When context_messages is provided, it is used so the refiner sees tool
    results (e.g. file contents read during the turn) and can write an improved
    response with full context. The critique is appended as a user turn.

    Without context_messages, falls back to a minimal prompt+response+critique.
    """
    refine_request = (
        f"Critique to address:\n{critique.critique}\n\n"
        f"Score received: {critique.score}/10. Required: >= {SCORE_THRESHOLD}/10.\n\n"
        f"Write the improved response:"
    )
    if context_messages:
        return list(context_messages) + [Message(role="user", content=refine_request)]
    content = (
        f"Original user prompt:\n{prompt}\n\n"
        f"Your previous response:\n{response}\n\n"
        f"{refine_request}"
    )
    return [Message(role="user", content=content)]


# ─── Critique parser ──────────────────────────────────────────────────────────

_VALID_TYPES = {"CODE_GENERATION", "CODE_EXPLANATION", "GENERAL"}


def _parse_critique(raw: str) -> CritiqueResult:
    """Parse the JSON critique response from the critic LLM.

    Expected format:
      {"score": 7, "type": "CODE_GENERATION", "critique": "Fix edge case." | null}

    Falls back gracefully when the JSON is missing or malformed:
      score     → 5 (below threshold, triggers refinement)
      type      → GENERAL
      critique  → the raw text itself (better than losing it entirely)

    Also handles the case where the LLM wraps JSON in markdown code fences.
    """
    text = raw.strip()
    # Strip markdown code fences if the LLM wraps the JSON despite instructions
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

    try:
        data = json.loads(text)
        score = max(1, min(10, int(data["score"])))
        response_type = str(data.get("type", "GENERAL")).upper()
        if response_type not in _VALID_TYPES:
            response_type = "GENERAL"
        critique_val = data.get("critique")
        critique = "None" if critique_val is None else str(critique_val).strip()
        return CritiqueResult(score=score, response_type=response_type, critique=critique, raw=raw)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return CritiqueResult(score=5, response_type="GENERAL", critique=raw.strip(), raw=raw)


# ─── Main entry point ─────────────────────────────────────────────────────────

def reflect(
    prompt: str,
    response: str,
    client: LLMClient,
    config: ReflectionConfig,
    context_messages: Optional[list[Message]] = None,
) -> ReflectionResult:
    """Run the self-refine critique/refine loop.

    Uses client.complete() (non-streaming) for all calls. Never modifies
    any Conversation object — callers must handle conversation updates.

    When context_messages is provided (the full conversation history), the
    critic and refiner receive complete context — including tool results from
    read_file calls — so they can evaluate and improve the response accurately.
    After each refinement round, context_messages is updated so subsequent
    critique calls see the latest revised response.

    Returns immediately (with original response) when config.depth == 0.
    """
    if config.depth == 0:
        return ReflectionResult(
            original_response=response,
            final_response=response,
            rounds=0,
            final_score=0,
        )

    get_tracer().emit(
        "reflection_start",
        initial_response_length=len(response),
        reflection_enabled=True,
    )

    current = response
    current_context = list(context_messages) if context_messages else None
    critiques: list[CritiqueResult] = []
    was_refined = False
    final_score = 0

    for _ in range(config.depth):
        # ── Critique ──────────────────────────────────────────────────────────
        _critique_msgs = _build_critique_messages(prompt, current, current_context)
        get_tracer().emit(
            "llm_request",
            message_count=len(_critique_msgs),
            messages=_ser_msgs(_critique_msgs),
            system=CRITIC_SYSTEM_PROMPT,
            tools=[],
            tool_names=[],
            model=getattr(client, "model_id", "unknown"),
            estimated_input_tokens=sum(len(str(m.content)) for m in _critique_msgs) // 4,
            reflection_role="critique",
        )
        _t0 = _time.monotonic()
        critique_resp = client.complete(_critique_msgs, system=CRITIC_SYSTEM_PROMPT)
        get_tracer().emit(
            "llm_response",
            response=critique_resp.content,
            stop_reason="end_turn",
            input_tokens=getattr(critique_resp, "input_tokens", 0),
            output_tokens=getattr(critique_resp, "output_tokens", 0),
            model=getattr(critique_resp, "model", getattr(client, "model_id", "unknown")),
            latency_ms=int((_time.monotonic() - _t0) * 1000),
            reflection_role="critique",
        )
        parsed = _parse_critique(critique_resp.content)
        critiques.append(parsed)
        final_score = parsed.score
        get_tracer().emit(
            "reflection_critique",
            score=parsed.score,
            critique=parsed.critique or "",
        )

        if parsed.score >= config.threshold:
            break   # good enough — exit before refining

        # ── Refine ────────────────────────────────────────────────────────────
        _refine_msgs = _build_refine_messages(prompt, current, parsed, current_context)
        get_tracer().emit(
            "llm_request",
            message_count=len(_refine_msgs),
            messages=_ser_msgs(_refine_msgs),
            system=REFINER_SYSTEM_PROMPT,
            tools=[],
            tool_names=[],
            model=getattr(client, "model_id", "unknown"),
            estimated_input_tokens=sum(len(str(m.content)) for m in _refine_msgs) // 4,
            reflection_role="refine",
        )
        _t0 = _time.monotonic()
        refine_resp = client.complete(_refine_msgs, system=REFINER_SYSTEM_PROMPT)
        get_tracer().emit(
            "llm_response",
            response=refine_resp.content,
            stop_reason="end_turn",
            input_tokens=getattr(refine_resp, "input_tokens", 0),
            output_tokens=getattr(refine_resp, "output_tokens", 0),
            model=getattr(refine_resp, "model", getattr(client, "model_id", "unknown")),
            latency_ms=int((_time.monotonic() - _t0) * 1000),
            reflection_role="refine",
        )
        refined = refine_resp.content.strip()

        if refined and refined != current:
            current = refined
            was_refined = True
            # Update context so the next critique round sees the refined response.
            # Replace the last assistant message with the new text.
            if current_context and current_context[-1].role == "assistant":
                current_context = current_context[:-1] + [
                    Message(role="assistant", content=current)
                ]

    result = ReflectionResult(
        original_response=response,
        final_response=current,
        rounds=len(critiques),
        final_score=final_score,
        critiques=critiques,
        was_refined=was_refined,
    )
    get_tracer().emit(
        "reflection_revision",
        was_revised=result.was_refined,
        new_response_length=len(result.final_response),
        final_response=result.final_response,
        original_response=result.original_response,
    )
    return result
