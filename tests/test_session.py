"""Tests for minion/session.py — save, load, list_sessions.

Uses tmp_path fixture so no files are written to ~/.minion/ during testing.
"""

import json
import pytest
from unittest.mock import patch
from pathlib import Path

from minion.conversation import Conversation
from minion.llm.base import Message
from minion.session import save, load, list_sessions


def _make_conversation() -> Conversation:
    c = Conversation(model="claude-3-5-sonnet")
    c.messages = [
        Message(role="user",      content="What is a closure?"),
        Message(role="assistant", content="A closure is..."),
    ]
    c.total_tokens = 300
    return c


@pytest.fixture(autouse=True)
def mock_sessions_dir(tmp_path):
    """Redirect all session I/O to a temp directory."""
    with patch("minion.session.SESSIONS_DIR", tmp_path):
        yield tmp_path


# ─── save ─────────────────────────────────────────────────────────────────────

class TestSave:
    def test_creates_json_file(self, tmp_path):
        save(_make_conversation(), "test-session")
        assert (tmp_path / "test-session.json").exists()

    def test_saved_json_is_valid(self, tmp_path):
        save(_make_conversation(), "test-session")
        data = json.loads((tmp_path / "test-session.json").read_text())
        assert data["version"] == 1
        assert data["model"] == "claude-3-5-sonnet"
        assert data["total_tokens"] == 300
        assert len(data["messages"]) == 2

    def test_messages_serialized_correctly(self, tmp_path):
        save(_make_conversation(), "test-session")
        data = json.loads((tmp_path / "test-session.json").read_text())
        assert data["messages"][0] == {"role": "user", "content": "What is a closure?"}
        assert data["messages"][1] == {"role": "assistant", "content": "A closure is..."}

    def test_returns_path(self, tmp_path):
        path = save(_make_conversation(), "my-session")
        assert path == tmp_path / "my-session.json"

    def test_saved_at_field_present(self, tmp_path):
        save(_make_conversation(), "test-session")
        data = json.loads((tmp_path / "test-session.json").read_text())
        assert "saved_at" in data


# ─── load ─────────────────────────────────────────────────────────────────────

class TestLoad:
    def test_round_trip(self, tmp_path):
        original = _make_conversation()
        save(original, "round-trip")
        restored = load("round-trip")
        assert restored.messages == original.messages
        assert restored.total_tokens == original.total_tokens
        assert restored._model == original._model

    def test_missing_session_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="no-such-session"):
            load("no-such-session")

    def test_empty_conversation_round_trips(self, tmp_path):
        c = Conversation(model="gpt-4o")
        save(c, "empty")
        restored = load("empty")
        assert restored.messages == []
        assert restored.total_tokens == 0


# ─── list_sessions ────────────────────────────────────────────────────────────

class TestListSessions:
    def test_empty_when_no_sessions(self):
        assert list_sessions() == []

    def test_returns_names_without_extension(self, tmp_path):
        save(_make_conversation(), "alpha")
        save(_make_conversation(), "beta")
        assert list_sessions() == ["alpha", "beta"]

    def test_returns_sorted_alphabetically(self, tmp_path):
        save(_make_conversation(), "zebra")
        save(_make_conversation(), "alpha")
        save(_make_conversation(), "mango")
        assert list_sessions() == ["alpha", "mango", "zebra"]
