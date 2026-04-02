"""MemoryExtractor — LLM-based memory extraction and consolidation.

Two focused operations, both using client.complete() (never stream):

  extract()     — given a prompt+response turn, ask the LLM what's worth
                  remembering; returns a list of MemoryRecord objects

  consolidate() — given two high-similarity memories, ask the LLM whether
                  to keep both, supersede one, or merge them

Both calls use isolated list[Message] — they never touch the main Conversation.
The lightweight nature of the prompts makes these cheap calls even with a
full-size model.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..llm.base import LLMClient, Message
from .config import MemoryConfig
from .record import MemoryRecord


# ─── Prompts ──────────────────────────────────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = """\
You are extracting memories from a conversation exchange for a coding assistant's long-term memory store.

Extract only information worth remembering across future sessions:
- User preferences (code style, tools, language choices, testing frameworks, conventions)
- Technical decisions made in this project (framework chosen, database selected, architecture)
- Bug causes discovered and their root fixes
- Project constraints and requirements the user stated
- Facts the user stated as true about their environment or workflow

Do NOT extract:
- Transient task details ("look at line 42 of auth.py right now")
- Generic knowledge any experienced developer would already know
- Questions the user asked (not facts)
- Information derivable by reading the current codebase without any context

Classify each memory:
  type     "semantic"    = stable fact or preference, likely to remain true over time
           "episodic"    = specific event with temporal context (e.g. "we fixed X bug today")
  scope    "global"      = applies across all projects (e.g. "user prefers pytest over unittest")
           "project"     = specific to the current codebase (e.g. "this repo uses PostgreSQL 15")
  category "identity"    = who the user is: name, role, background, experience level
           "preference"  = how the user likes to work: tools, code style, frameworks, habits
           "project"     = technical facts about a project or cross-project patterns: stack,
                           architecture decisions, constraints, conventions
           "event"       = catch-all for anything else: timestamped occurrences, bugs fixed,
                           discoveries, observations that don't fit the above

Output JSON array only — no preamble, no code fences:
[{"type":"semantic|episodic","scope":"global|project","category":"identity|preference|project|event","content":"...","tags":["tag1","tag2"]}]

If nothing is worth remembering, output: []"""


CONSOLIDATION_SYSTEM_PROMPT = """\
Two memories from a coding assistant's memory store may conflict or overlap. Decide what to do.

Output JSON only — no preamble, no code fences:
{"action":"supersede_a|supersede_b|keep_both|merge","merged_content":null}

For "merge", set merged_content to the combined text. For all other actions, set merged_content to null.

Rules:
- SUPERSEDE_A: Memory B is more accurate or recent; Memory A should be replaced by B
- SUPERSEDE_B: Memory A is more accurate or recent; Memory B should be replaced by A
- KEEP_BOTH: they are genuinely distinct facts worth keeping separately
- MERGE: both have useful, non-redundant information; combine them into one"""


# ─── ConsolidationResult ─────────────────────────────────────────────────────

@dataclass
class ConsolidationResult:
    """Output from one consolidation LLM call."""
    action: str               # "keep_both"|"supersede_a"|"supersede_b"|"merge"
    merged_content: Optional[str] = None  # set only when action == "merge"


_VALID_ACTIONS = {"keep_both", "supersede_a", "supersede_b", "merge"}


# ─── MemoryExtractor ──────────────────────────────────────────────────────────

class MemoryExtractor:
    """Wraps the two LLM calls needed for memory management.

    Stateless: holds only the config. Pass the LLMClient at call time so
    the extractor is easily testable and doesn't hold a reference to the
    client at construction.
    """

    def __init__(self, config: MemoryConfig) -> None:
        self._config = config

    # ─── Extraction ───────────────────────────────────────────────────────────

    def extract(
        self,
        prompt: str,
        response: str,
        client: LLMClient,
        project_path: Optional[str] = None,
        existing_memories: Optional[list[MemoryRecord]] = None,
    ) -> list[MemoryRecord]:
        """Extract memorable facts from a single conversation turn.

        Returns an empty list when:
          - The LLM finds nothing worth remembering
          - The LLM response cannot be parsed as valid JSON

        project_path is stored as metadata on project-scoped memories.
        existing_memories, when provided, are listed in the prompt so the LLM
        can skip facts that are already known.
        """
        existing_block = ""
        if existing_memories:
            lines = ["Already remembered (do not re-extract these):"]
            for m in existing_memories:
                lines.append(f"- {m.content}")
            existing_block = "\n\n" + "\n".join(lines)

        user_content = (
            f"Conversation exchange to analyse:\n\n"
            f"User: {prompt}\n\n"
            f"Assistant: {response}"
            f"{existing_block}"
        )
        messages = [Message(role="user", content=user_content)]
        resp = client.complete(messages, system=EXTRACTION_SYSTEM_PROMPT)
        return self._parse_extracted(resp.content, project_path)

    def _parse_extracted(
        self,
        raw: str,
        project_path: Optional[str],
    ) -> list[MemoryRecord]:
        """Parse the JSON extraction response into MemoryRecord objects."""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

        try:
            items = json.loads(text)
            if not isinstance(items, list):
                return []
        except (json.JSONDecodeError, ValueError):
            return []

        records: list[MemoryRecord] = []
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            type_ = str(item.get("type", "semantic")).lower()
            if type_ not in ("semantic", "episodic"):
                type_ = "semantic"
            scope = str(item.get("scope", "project")).lower()
            if scope not in ("global", "project"):
                scope = "project"
            category = str(item.get("category", "project")).lower()
            if category not in ("identity", "preference", "project", "event"):
                category = "event"
            tags = [str(t).strip() for t in item.get("tags", []) if str(t).strip()]

            records.append(MemoryRecord(
                id=str(uuid.uuid4()),
                content=content,
                type=type_,
                scope=scope,
                project_path=project_path if scope == "project" else None,
                tags=tags,
                created_at=now,
                superseded_by=None,
                category=category,
            ))

        return records

    # ─── Consolidation ────────────────────────────────────────────────────────

    def consolidate(
        self,
        a: MemoryRecord,
        b: MemoryRecord,
        client: LLMClient,
    ) -> ConsolidationResult:
        """Ask the LLM whether memories a and b conflict and how to resolve.

        Falls back to KEEP_BOTH when the response cannot be parsed, to avoid
        accidentally deleting memories on LLM errors.
        """
        user_content = (
            f"Memory A (created {a.created_at}):\n{a.content}\n\n"
            f"Memory B (created {b.created_at}):\n{b.content}"
        )
        messages = [Message(role="user", content=user_content)]
        resp = client.complete(messages, system=CONSOLIDATION_SYSTEM_PROMPT)
        return self._parse_consolidation(resp.content)

    def _parse_consolidation(self, raw: str) -> ConsolidationResult:
        """Parse the JSON consolidation response. Falls back to keep_both."""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

        try:
            data = json.loads(text)
            action = str(data.get("action", "keep_both")).lower()
            if action not in _VALID_ACTIONS:
                action = "keep_both"
            merged = data.get("merged_content")
            merged_content = str(merged).strip() if merged else None
            return ConsolidationResult(action=action, merged_content=merged_content)
        except (json.JSONDecodeError, ValueError, TypeError):
            return ConsolidationResult(action="keep_both")
