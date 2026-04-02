"""Tests for memory-related slash commands in minion/repl.py.

Covers /memory, /remember, /forget, /recall and their registration
in REPL_COMMANDS. All file I/O uses tmp_path. No API calls.
"""

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from minion.conversation import Conversation
from minion.memory.config import MemoryConfig
from minion.memory.record import MemoryRecord
from minion.memory.store import MemoryStore
from minion.repl import REPL_COMMANDS, ReplState, _get_last_response_text, _handle_slash_command


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_store(tmp_path: Path) -> MemoryStore:
    config = MemoryConfig(global_memory_dir=tmp_path / "global")
    return MemoryStore(
        config=config,
        project_cwd=tmp_path / "project",
        client=MagicMock(),
        embedder=None,
    )


def _dispatch(raw: str, memory_store=None, state=None) -> bool:
    client = MagicMock()
    conversation = Conversation()
    if state is None:
        state = ReplState()
    return _handle_slash_command(raw, client, conversation, state=state, memory_store=memory_store)


def _stored_record(store: MemoryStore, content: str = "test fact") -> MemoryRecord:
    r = MemoryRecord(
        id=str(uuid.uuid4()),
        content=content,
        type="semantic",
        scope="project",
        project_path=str(Path.cwd()),
        tags=[],
        created_at="2026-04-02T10:00:00",
    )
    store.store(r)
    return r


# ─── REPL_COMMANDS registry ───────────────────────────────────────────────────

class TestMemoryCommandsRegistered:
    def test_memory_in_repl_commands(self):
        assert "/memory" in REPL_COMMANDS

    def test_remember_in_repl_commands(self):
        assert "/remember" in REPL_COMMANDS

    def test_forget_in_repl_commands(self):
        assert "/forget" in REPL_COMMANDS

    def test_recall_in_repl_commands(self):
        assert "/recall" in REPL_COMMANDS


# ─── /memory ─────────────────────────────────────────────────────────────────

class TestMemoryCommand:
    def test_handled_as_slash_command(self, tmp_path):
        store = _make_store(tmp_path)
        assert _dispatch("/memory", memory_store=store) is True

    def test_memory_on_sets_flag(self, tmp_path):
        state = ReplState(memory_enabled=False)
        _dispatch("/memory on", memory_store=_make_store(tmp_path), state=state)
        assert state.memory_enabled is True

    def test_memory_off_clears_flag(self, tmp_path):
        state = ReplState(memory_enabled=True)
        _dispatch("/memory off", memory_store=_make_store(tmp_path), state=state)
        assert state.memory_enabled is False

    def test_memory_no_arg_returns_true(self, tmp_path):
        result = _dispatch("/memory", memory_store=_make_store(tmp_path))
        assert result is True

    def test_memory_none_state_still_returns_true(self):
        assert _dispatch("/memory", state=None) is True

    def test_memory_none_store_still_returns_true(self):
        state = ReplState()
        assert _dispatch("/memory", memory_store=None, state=state) is True


# ─── /remember ───────────────────────────────────────────────────────────────

class TestRememberCommand:
    def test_handled_as_slash_command(self, tmp_path):
        assert _dispatch("/remember User prefers pytest", memory_store=_make_store(tmp_path)) is True

    def test_stores_a_memory(self, tmp_path):
        store = _make_store(tmp_path)
        _dispatch("/remember User prefers pytest", memory_store=store)
        memories = store.list_all()
        assert any("prefers pytest" in m.content for m in memories)

    def test_remember_no_arg_returns_true(self, tmp_path):
        result = _dispatch("/remember", memory_store=_make_store(tmp_path))
        assert result is True

    def test_remember_no_arg_does_not_store(self, tmp_path):
        store = _make_store(tmp_path)
        _dispatch("/remember", memory_store=store)
        assert store.list_all() == []

    def test_remember_without_store_returns_true(self):
        assert _dispatch("/remember some fact", memory_store=None) is True


# ─── /forget ─────────────────────────────────────────────────────────────────

class TestForgetCommand:
    def test_handled_as_slash_command(self, tmp_path):
        store = _make_store(tmp_path)
        _stored_record(store)
        assert _dispatch("/forget test fact", memory_store=store) is True

    def test_deletes_matching_memory(self, tmp_path):
        store = _make_store(tmp_path)
        _stored_record(store, content="User loves PostgreSQL")
        _dispatch("/forget PostgreSQL", memory_store=store)
        assert store.list_all() == []

    def test_forget_no_arg_returns_true(self, tmp_path):
        assert _dispatch("/forget", memory_store=_make_store(tmp_path)) is True

    def test_forget_no_arg_does_not_delete_anything(self, tmp_path):
        store = _make_store(tmp_path)
        _stored_record(store, "keep this")
        _dispatch("/forget", memory_store=store)
        assert len(store.list_all()) == 1

    def test_forget_without_store_returns_true(self):
        assert _dispatch("/forget something", memory_store=None) is True


# ─── /recall ─────────────────────────────────────────────────────────────────

class TestRecallCommand:
    def test_handled_as_slash_command(self, tmp_path):
        assert _dispatch("/recall", memory_store=_make_store(tmp_path)) is True

    def test_recall_with_query_returns_true(self, tmp_path):
        assert _dispatch("/recall database", memory_store=_make_store(tmp_path)) is True

    def test_recall_without_store_returns_true(self):
        assert _dispatch("/recall", memory_store=None) is True


# ─── _get_last_response_text ──────────────────────────────────────────────────

class TestGetLastResponseText:
    def test_returns_none_for_empty_conversation(self):
        assert _get_last_response_text(Conversation()) is None

    def test_returns_text_for_string_content(self):
        conv = Conversation()
        conv.add_user("question")
        conv.add_assistant("answer", usage=None)
        assert _get_last_response_text(conv) == "answer"

    def test_returns_none_when_last_message_is_user(self):
        conv = Conversation()
        conv.add_user("question")
        assert _get_last_response_text(conv) is None
