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
from datetime import datetime
from pathlib import Path

from .conversation import Conversation
from .llm.base import Message

SESSIONS_DIR = Path.home() / ".minion" / "sessions"


def _sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


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
            {"role": m.role, "content": m.content}
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
        Message(role=m["role"], content=m["content"])
        for m in data.get("messages", [])
    ]
    return conversation


def list_sessions() -> list[str]:
    """Return session names (without .json extension) sorted alphabetically."""
    try:
        return sorted(p.stem for p in _sessions_dir().glob("*.json"))
    except FileNotFoundError:
        return []
