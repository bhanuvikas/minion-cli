"""Unit tests for minion/tracing/tracer.py.

Tests cover: emit/JSONL correctness, no-op mode, thread safety, truncation
rules, session trimming, counter accumulation, and finalize().
"""

import json
import threading
import time
from pathlib import Path

import pytest

import minion.tracing.tracer as tracer_module
from minion.tracing.tracer import NullTracer, Tracer, get_tracer, init_tracer


@pytest.fixture(autouse=True)
def reset_tracer():
    """Reset module-level _tracer before and after each test."""
    tracer_module._tracer = None
    yield
    tracer_module._tracer = None


# ─── NullTracer ────────────────────────────────────────────────────────────────

class TestNullTracer:
    def test_emit_is_noop(self, tmp_path):
        null = NullTracer()
        null.emit("session_start", model="test", system_prompt="x", cwd="/")
        assert list(tmp_path.iterdir()) == []

    def test_finalize_is_noop(self):
        null = NullTracer()
        null.finalize()  # must not raise

    def test_session_id_is_none(self):
        assert NullTracer.session_id is None

    def test_get_tracer_before_init_returns_null_tracer(self):
        t = get_tracer()
        assert isinstance(t, NullTracer)


# ─── Tracer.emit() ─────────────────────────────────────────────────────────────

class TestTracerEmit:
    def test_emit_writes_valid_jsonl_line(self, tmp_path):
        t = Tracer(session_id="test-123", path=tmp_path / "test-123.jsonl")
        t.emit("user_turn", text="hello")
        lines = (tmp_path / "test-123.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event_type"] == "user_turn"
        assert event["session_id"] == "test-123"
        assert event["data"]["text"] == "hello"
        assert "timestamp" in event

    def test_emit_appends_multiple_events_in_order(self, tmp_path):
        t = Tracer(session_id="s1", path=tmp_path / "s1.jsonl")
        t.emit("user_turn", text="first")
        t.emit("user_turn", text="second")
        lines = (tmp_path / "s1.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["data"]["text"] == "first"
        assert json.loads(lines[1])["data"]["text"] == "second"

    def test_emit_is_thread_safe(self, tmp_path):
        t = Tracer(session_id="ts", path=tmp_path / "ts.jsonl")
        errors: list[Exception] = []

        def writer():
            try:
                for _ in range(50):
                    t.emit("user_turn", text="x")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors
        lines = (tmp_path / "ts.jsonl").read_text().strip().splitlines()
        assert len(lines) == 250  # 5 threads × 50 events
        # Every line must be valid JSON
        for line in lines:
            json.loads(line)


# ─── Truncation rules ─────────────────────────────────────────────────────────

class TestTruncation:
    def test_system_prompt_stored_in_full(self, tmp_path):
        t = Tracer(session_id="trunc1", path=tmp_path / "t1.jsonl")
        t.emit("session_start", model="m", system_prompt="x" * 1000, cwd="/")
        event = json.loads((tmp_path / "t1.jsonl").read_text())
        assert "system_prompt" in event["data"]
        assert len(event["data"]["system_prompt"]) == 1000

    def test_response_stored_in_full(self, tmp_path):
        t = Tracer(session_id="trunc2", path=tmp_path / "t2.jsonl")
        t.emit(
            "llm_response",
            response="y" * 2000,
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=5,
            model="m",
            latency_ms=100,
        )
        event = json.loads((tmp_path / "t2.jsonl").read_text())
        assert "response" in event["data"]
        assert len(event["data"]["response"]) == 2000

    def test_tool_output_stored_in_full(self, tmp_path):
        t = Tracer(session_id="trunc3", path=tmp_path / "t3.jsonl")
        t.emit("tool_result", tool_name="read_file", output="z" * 1000, success=True)
        event = json.loads((tmp_path / "t3.jsonl").read_text())
        assert "output" in event["data"]
        assert len(event["data"]["output"]) == 1000

    def test_critique_stored_in_full(self, tmp_path):
        t = Tracer(session_id="trunc4", path=tmp_path / "t4.jsonl")
        t.emit("reflection_critique", score=7, critique="c" * 1000)
        event = json.loads((tmp_path / "t4.jsonl").read_text())
        assert "critique" in event["data"]
        assert len(event["data"]["critique"]) == 1000

    def test_memory_retrieve_stores_all_memories(self, tmp_path):
        t = Tracer(session_id="trunc5", path=tmp_path / "t5.jsonl")
        mems = ["a" * 200] * 10
        t.emit("memory_retrieve", query="q", num_retrieved=10, memories=mems)
        event = json.loads((tmp_path / "t5.jsonl").read_text())
        stored = event["data"]["memories"]
        assert len(stored) == 10
        assert all(len(m) == 200 for m in stored)

    def test_context_inject_stores_all_memories(self, tmp_path):
        t = Tracer(session_id="trunc6", path=tmp_path / "t6.jsonl")
        mems = ["b" * 200] * 5
        t.emit("context_inject", memory_count=5, token_estimate=100, memories=mems)
        event = json.loads((tmp_path / "t6.jsonl").read_text())
        stored = event["data"]["memories"]
        assert len(stored) == 5
        assert all(len(m) == 200 for m in stored)


# ─── Counter accumulation ─────────────────────────────────────────────────────

class TestCounterAccumulation:
    def test_turn_count_increments_on_user_turn(self, tmp_path):
        t = Tracer(session_id="cnt", path=tmp_path / "cnt.jsonl")
        t.emit("user_turn", text="a")
        t.emit("user_turn", text="b")
        assert t._turn_count == 2

    def test_token_counters_accumulate_on_llm_response(self, tmp_path):
        t = Tracer(session_id="cnt2", path=tmp_path / "cnt2.jsonl")
        t.emit("llm_response", response="r", stop_reason="end_turn",
               input_tokens=100, output_tokens=50, model="m", latency_ms=10)
        t.emit("llm_response", response="r2", stop_reason="end_turn",
               input_tokens=200, output_tokens=75, model="m", latency_ms=20)
        assert t._input_tokens == 300
        assert t._output_tokens == 125

    def test_tool_call_count_increments(self, tmp_path):
        t = Tracer(session_id="cnt3", path=tmp_path / "cnt3.jsonl")
        t.emit("tool_call", tool_name="read_file", inputs={"path": "x"})
        t.emit("tool_call", tool_name="write_file", inputs={"path": "y", "content": "z"})
        assert t._tool_calls == 2


# ─── finalize() ───────────────────────────────────────────────────────────────

class TestFinalize:
    def test_finalize_emits_session_end(self, tmp_path):
        t = Tracer(session_id="fin", path=tmp_path / "fin.jsonl")
        t.emit("user_turn", text="hello")
        t.emit("tool_call", tool_name="read_file", inputs={})
        t.emit("llm_response", response="r", stop_reason="end_turn",
               input_tokens=50, output_tokens=20, model="m", latency_ms=5)
        t.finalize()

        lines = (tmp_path / "fin.jsonl").read_text().strip().splitlines()
        last_event = json.loads(lines[-1])
        assert last_event["event_type"] == "session_end"
        d = last_event["data"]
        assert d["total_turns"] == 1
        assert d["total_tool_calls"] == 1
        assert d["total_input_tokens"] == 50
        assert d["total_output_tokens"] == 20
        assert "duration_seconds" in d
        assert d["duration_seconds"] >= 0


# ─── init_tracer() ────────────────────────────────────────────────────────────

class TestInitTracer:
    def test_init_tracer_sets_module_singleton(self, tmp_path):
        init_tracer(session_id="abc", traces_dir=tmp_path)
        t = get_tracer()
        assert isinstance(t, Tracer)
        assert t.session_id == "abc"

    def test_init_tracer_creates_jsonl_file_on_first_emit(self, tmp_path):
        init_tracer(session_id="newfile", traces_dir=tmp_path)
        get_tracer().emit("user_turn", text="hi")
        assert (tmp_path / "newfile.jsonl").exists()

    def test_init_tracer_trims_old_sessions(self, tmp_path):
        # Create 55 fake session files with distinct mtimes
        for i in range(55):
            f = tmp_path / f"session-{i:04d}.jsonl"
            f.write_text("")
            time.sleep(0.001)

        init_tracer(session_id="new-session", traces_dir=tmp_path)

        remaining = list(tmp_path.glob("*.jsonl"))
        # The new session file + up to 49 old ones = at most 50
        assert len(remaining) <= 50

    def test_get_tracer_returns_tracer_after_init(self, tmp_path):
        assert isinstance(get_tracer(), NullTracer)
        init_tracer(session_id="x", traces_dir=tmp_path)
        assert isinstance(get_tracer(), Tracer)
