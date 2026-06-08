"""Session persistence: save and load conversations to ~/.minion/sessions/.

Single responsibility: thin I/O layer only. No business logic — just JSON
serialization of a Conversation to disk and back.

Session file format (version 1):
  {
    "version": 1,
    "model": "claude-3-5-sonnet-20241022",
    "total_tokens": 8901,
    "saved_at": "2026-03-31T10:22:00",
    "messages": [
      {"role": "user",      "content": "..."},
      {"role": "assistant", "content": "..."}
    ]
  }
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Union

from ..llm.conversation import Conversation
from ..llm.base import (
    ContentTextBlock, ContentToolUseBlock, ContentToolResultBlock, Message,
)

SESSIONS_DIR = Path.home() / ".minion" / "sessions"


def _sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def _serialize_content(content: Union[str, list]) -> Union[str, list]:
    """Convert message content to a JSON-serializable form.

    Plain-text content passes through unchanged. Content-block lists are
    converted to typed dicts so json.dumps() can encode them.
    """
    if isinstance(content, str):
        return content
    blocks = []
    for block in content:
        if isinstance(block, ContentTextBlock):
            blocks.append({"type": "text", "text": block.text})
        elif isinstance(block, ContentToolUseBlock):
            blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
        elif isinstance(block, ContentToolResultBlock):
            blocks.append({"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content})
        else:
            blocks.append(str(block))
    return blocks


def _deserialize_content(content: Union[str, list]) -> Union[str, list]:
    """Reconstruct typed ContentBlocks from the serialized dict form."""
    if isinstance(content, str):
        return content
    blocks = []
    for item in content:
        t = item.get("type", "")
        if t == "text":
            blocks.append(ContentTextBlock(text=item["text"]))
        elif t == "tool_use":
            blocks.append(ContentToolUseBlock(id=item["id"], name=item["name"], input=item.get("input", {})))
        elif t == "tool_result":
            blocks.append(ContentToolResultBlock(tool_use_id=item["tool_use_id"], content=item["content"]))
        else:
            blocks.append(item)  # unknown type — keep as dict; tolerated by adapter
    return blocks


def save(conversation: Conversation, name: str) -> Path:
    """Serialize conversation to ~/.minion/sessions/<name>.json.

    Returns the path written to.
    """
    path = _sessions_dir() / f"{name}.json"
    data = {
        "version": 1,
        "model": conversation._model,
        "total_tokens": conversation.total_tokens,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "messages": [
            {"role": m.role, "content": _serialize_content(m.content)}
            for m in conversation.messages
        ],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def load(name: str) -> Conversation:
    """Deserialize a session from disk. Raises FileNotFoundError if not found."""
    path = _sessions_dir() / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"No session named '{name}' found at {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    conversation = Conversation(model=data.get("model", ""))
    conversation.total_tokens = data.get("total_tokens", 0)
    conversation.messages = [
        Message(role=m["role"], content=_deserialize_content(m["content"]))
        for m in data.get("messages", [])
    ]
    return conversation


def list_sessions() -> list[str]:
    """Return session names (without .json extension) sorted alphabetically."""
    try:
        return sorted(p.stem for p in _sessions_dir().glob("*.json"))
    except FileNotFoundError:
        return []


@dataclass
class SessionMeta:
    """Lightweight session descriptor — read from JSON without full message deserialisation."""
    name: str
    model: str
    total_tokens: int
    saved_at: str        # ISO 8601 timestamp
    message_count: int
    first_user_msg: str  # first 400 chars of first user message (plain text)
    last_user_msg: str   # first 400 chars of last user message ("" if only one)


def _extract_text(content: object) -> str:
    """Pull plain text out of a message content field (str or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def list_sessions_with_metadata() -> list[SessionMeta]:
    """Return sessions sorted newest-first, with metadata extracted from each JSON file."""
    metas: list[SessionMeta] = []
    try:
        paths = list(_sessions_dir().glob("*.json"))
    except FileNotFoundError:
        return []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            user_texts = [
                _extract_text(m["content"])
                for m in msgs if m.get("role") == "user"
            ]
            first = user_texts[0][:400] if user_texts else ""
            last  = user_texts[-1][:400] if len(user_texts) > 1 else ""
            metas.append(SessionMeta(
                name=path.stem,
                model=data.get("model", ""),
                total_tokens=data.get("total_tokens", 0),
                saved_at=data.get("saved_at", ""),
                message_count=len(msgs),
                first_user_msg=first,
                last_user_msg=last,
            ))
        except Exception:
            metas.append(SessionMeta(path.stem, "", 0, "", 0, "", ""))
    return sorted(metas, key=lambda s: s.saved_at, reverse=True)
