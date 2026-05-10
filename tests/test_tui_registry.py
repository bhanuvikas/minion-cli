"""Tests for tui/agent_registry.py, tui/inspector.py, tui/messages.py, tui/theme.py.

All pure unit tests — no Textual runtime, no real TTY.

SubagentRegistry is the source of truth for the inspector panel: threads
call register()/update() from parallel agent execution; the inspector reads
get_all() to render per-agent transcripts.  Every update posts an
InspectorUpdated message for thread-safe UI refresh.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from rich.text import Text


# ── SubagentRegistry ──────────────────────────────────────────────────────────

class TestSubagentRegistry:
    def setup_method(self):
        from minion.tui.agent_registry import SubagentRegistry
        self.reg = SubagentRegistry()
        self.posted: list = []
        self.reg.set_post_message(self.posted.append)

    # ── register ──────────────────────────────────────────────────────────────

    def test_empty_on_creation(self):
        assert len(self.reg) == 0
        assert self.reg.get_all() == []

    def test_register_adds_state(self):
        self.reg.register("id1", "coder", "write hello.py", "coder")
        assert len(self.reg) == 1

    def test_registered_state_fields(self):
        self.reg.register("id1", "coder", "write hello.py", "coder")
        state = self.reg.get_all()[0]
        assert state.id     == "id1"
        assert state.label  == "coder"
        assert state.task   == "write hello.py"
        assert state.role   == "coder"
        assert state.status == "pending"

    def test_duplicate_id_register_is_no_op(self):
        self.reg.register("id1", "coder",  "task-a", "coder")
        self.reg.register("id1", "tester", "task-b", "tester")   # ignored
        assert len(self.reg) == 1
        assert self.reg.get_all()[0].label == "coder"

    # ── update ────────────────────────────────────────────────────────────────

    def test_update_running_changes_status(self):
        self.reg.register("id1", "coder", "task", "coder")
        self.reg.update("id1", "running")
        assert self.reg.get("id1").status == "running"

    def test_update_complete_sets_status_latency_preview(self):
        self.reg.register("id1", "coder", "task", "coder")
        self.reg.update("id1", "complete", latency_ms=1234, preview="wrote 3 files")
        s = self.reg.get("id1")
        assert s.status     == "complete"
        assert s.latency_ms == 1234
        assert s.preview    == "wrote 3 files"

    def test_update_error_sets_status_and_message(self):
        self.reg.register("id1", "coder", "task", "coder")
        self.reg.update("id1", "error", error="timeout")
        s = self.reg.get("id1")
        assert s.status == "error"
        assert s.error  == "timeout"

    def test_update_turn_end_stores_messages(self):
        self.reg.register("id1", "coder", "task", "coder")
        msgs = [{"role": "user", "type": "text", "text": "do X"}]
        self.reg.update("id1", "turn_end", messages=msgs)
        assert self.reg.get("id1").messages == msgs

    def test_update_unknown_id_is_no_op(self):
        self.reg.update("nonexistent", "running")
        assert len(self.posted) == 0

    # ── post_message / invalidate alias ───────────────────────────────────────

    def test_update_posts_inspector_updated_message(self):
        from minion.tui.messages import InspectorUpdated
        self.reg.register("id1", "coder", "task", "coder")
        self.reg.update("id1", "running")
        assert len(self.posted) == 1
        assert isinstance(self.posted[0], InspectorUpdated)

    def test_multiple_updates_post_multiple_messages(self):
        self.reg.register("id1", "coder", "task", "coder")
        self.reg.update("id1", "running")
        self.reg.update("id1", "complete", latency_ms=0)
        assert len(self.posted) == 2

    def test_set_invalidate_is_alias_for_set_post_message(self):
        from minion.tui.messages import InspectorUpdated
        from minion.tui.agent_registry import SubagentRegistry
        reg2 = SubagentRegistry()
        posted2: list = []
        reg2.set_invalidate(posted2.append)        # old API name
        reg2.register("id1", "coder", "task", "coder")
        reg2.update("id1", "running")
        assert len(posted2) == 1
        assert isinstance(posted2[0], InspectorUpdated)

    def test_no_post_message_fn_does_not_raise(self):
        from minion.tui.agent_registry import SubagentRegistry
        reg = SubagentRegistry()                   # no post_message set
        reg.register("id1", "coder", "task", "coder")
        reg.update("id1", "running")               # must not raise

    # ── clear / get_all / get / len ───────────────────────────────────────────

    def test_clear_removes_all_states(self):
        self.reg.register("a", "a-agent", "task", "a")
        self.reg.register("b", "b-agent", "task", "b")
        self.reg.clear()
        assert len(self.reg) == 0
        assert self.reg.get_all() == []

    def test_get_all_preserves_insertion_order(self):
        self.reg.register("z", "z-agent", "task-z", "z")
        self.reg.register("a", "a-agent", "task-a", "a")
        labels = [s.label for s in self.reg.get_all()]
        assert labels == ["z-agent", "a-agent"]

    def test_get_all_returns_independent_copies(self):
        self.reg.register("id1", "coder", "task", "coder")
        states = self.reg.get_all()
        states[0].status = "mutated"
        assert self.reg.get("id1").status == "pending"  # original unchanged

    def test_get_returns_none_for_unknown_id(self):
        assert self.reg.get("nonexistent") is None

    def test_get_returns_independent_copy(self):
        self.reg.register("id1", "coder", "task", "coder")
        s = self.reg.get("id1")
        s.status = "mutated"
        assert self.reg.get("id1").status == "pending"

    def test_len_tracks_registered_count(self):
        assert len(self.reg) == 0
        self.reg.register("a", "a", "t", "a")
        assert len(self.reg) == 1
        self.reg.register("b", "b", "t", "b")
        assert len(self.reg) == 2

    # ── thread safety smoke test ──────────────────────────────────────────────

    def test_concurrent_registers_do_not_corrupt_state(self):
        import threading

        def _register(n: int) -> None:
            self.reg.register(f"id{n}", f"agent{n}", f"task{n}", "role")

        threads = [threading.Thread(target=_register, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(self.reg) == 20


# ── InspectorPanel ────────────────────────────────────────────────────────────

class TestInspectorPanel:
    def _make(self, *, with_agents: bool = True):
        from minion.tui.agent_registry import SubagentRegistry
        from minion.tui.inspector import InspectorPanel
        reg = SubagentRegistry()
        if with_agents:
            reg.register("id1", "coder", "write hello.py", "coder")
        panel = InspectorPanel(registry=reg)
        return panel, reg

    def _app_mock(self, *, width: int = 120, height: int = 40) -> MagicMock:
        app = MagicMock()
        app.size.width  = width
        app.size.height = height
        return app

    # ── visibility / toggle ───────────────────────────────────────────────────

    def test_not_visible_initially(self):
        panel, _ = self._make()
        assert not panel.is_visible

    def test_toggle_opens_when_agents_exist(self):
        panel, _ = self._make()
        panel.toggle()
        assert panel.is_visible

    def test_toggle_closes_when_already_open(self):
        panel, _ = self._make()
        panel.open()
        panel.toggle()
        assert not panel.is_visible

    def test_open_noop_when_no_agents(self):
        panel, _ = self._make(with_agents=False)
        panel.open()
        assert not panel.is_visible

    def test_close_sets_invisible(self):
        panel, _ = self._make()
        panel.open()
        panel.close()
        assert not panel.is_visible

    # ── navigation ────────────────────────────────────────────────────────────

    def test_move_agent_advances_selection(self):
        panel, reg = self._make()
        reg.register("id2", "tester", "run tests", "tester")
        panel.open()
        panel.move_agent(1)
        assert panel._sel_idx == 1

    def test_move_agent_wraps_around_backward(self):
        panel, reg = self._make()
        reg.register("id2", "tester", "run tests", "tester")
        panel.open()
        panel.move_agent(-1)   # 0 → len-1
        assert panel._sel_idx == 1

    def test_scroll_increases_offset(self):
        panel, _ = self._make()
        panel.scroll(5)
        assert panel._scroll == 5

    def test_scroll_clamps_at_zero(self):
        panel, _ = self._make()
        panel.scroll(-100)
        assert panel._scroll == 0

    def test_toggle_expanded_flips_flag(self):
        panel, _ = self._make()
        assert not panel._expanded
        panel.toggle_expanded()
        assert panel._expanded
        panel.toggle_expanded()
        assert not panel._expanded

    # ── hint text ─────────────────────────────────────────────────────────────

    def test_hint_includes_agent_label(self):
        panel, _ = self._make()
        panel.open()
        assert "coder" in panel.hint()

    def test_hint_includes_close_shortcut(self):
        panel, _ = self._make()
        panel.open()
        assert "ctrl+o" in panel.hint()

    def test_hint_includes_switch_instruction_with_multiple_agents(self):
        panel, reg = self._make()
        reg.register("id2", "tester", "run tests", "tester")
        panel.open()
        hint = panel.hint()
        assert "←→" in hint or "switch" in hint

    def test_hint_omits_switch_with_single_agent(self):
        panel, _ = self._make()
        panel.open()
        hint = panel.hint()
        assert "←→" not in hint

    # ── get_rich_text ─────────────────────────────────────────────────────────

    def test_get_rich_text_returns_text_object(self):
        panel, reg = self._make()
        reg.update("id1", "running")
        panel.open()
        panel.set_app(self._app_mock())
        assert isinstance(panel.get_rich_text(), Text)

    def test_get_rich_text_contains_inspector_title(self):
        panel, _ = self._make()
        panel.open()
        panel.set_app(self._app_mock())
        assert "Inspector" in str(panel.get_rich_text())

    def test_get_rich_text_contains_agent_label(self):
        panel, _ = self._make()
        panel.open()
        panel.set_app(self._app_mock())
        assert "coder" in str(panel.get_rich_text())

    def test_get_rich_text_empty_when_no_agents(self):
        panel, _ = self._make(with_agents=False)
        panel._visible = True   # force visible to reach render path
        assert str(panel.get_rich_text()) == ""

    def test_get_rich_text_shows_running_for_running_agent(self):
        panel, reg = self._make()
        reg.update("id1", "running")
        panel.open()
        panel.set_app(self._app_mock())
        text = str(panel.get_rich_text())
        assert "running" in text

    def test_get_rich_text_has_box_borders(self):
        panel, _ = self._make()
        panel.open()
        panel.set_app(self._app_mock())
        text = str(panel.get_rich_text())
        assert "┌" in text
        assert "└" in text


# ── Messages ──────────────────────────────────────────────────────────────────

class TestTuiMessages:
    def test_slots_updated_is_message_subclass(self):
        from textual.message import Message
        from minion.tui.messages import SlotsUpdated
        assert issubclass(SlotsUpdated, Message)

    def test_inspector_updated_is_message_subclass(self):
        from textual.message import Message
        from minion.tui.messages import InspectorUpdated
        assert issubclass(InspectorUpdated, Message)

    def test_slots_updated_is_instantiable(self):
        from minion.tui.messages import SlotsUpdated
        assert SlotsUpdated() is not None

    def test_inspector_updated_is_instantiable(self):
        from minion.tui.messages import InspectorUpdated
        assert InspectorUpdated() is not None

    def test_messages_are_distinct_types(self):
        from minion.tui.messages import SlotsUpdated, InspectorUpdated
        assert SlotsUpdated is not InspectorUpdated


# ── Theme ─────────────────────────────────────────────────────────────────────

class TestTuiTheme:
    def test_gold_hex(self):
        from minion.tui.theme import GOLD
        assert GOLD == "#FFD700"

    def test_blue_hex(self):
        from minion.tui.theme import BLUE
        assert BLUE == "#1E90FF"

    def test_green_hex(self):
        from minion.tui.theme import GREEN
        assert GREEN == "#4CAF50"

    def test_silver_hex(self):
        from minion.tui.theme import SILVER
        assert SILVER == "#C0C0C0"

    def test_dim_hex(self):
        from minion.tui.theme import DIM
        assert DIM == "#666666"

    def test_tcss_is_non_empty_string(self):
        from minion.tui.theme import MINION_TCSS
        assert isinstance(MINION_TCSS, str)
        assert len(MINION_TCSS) > 100

    def test_tcss_contains_key_widget_selectors(self):
        from minion.tui.theme import MINION_TCSS
        for selector in ("ConversationArea", "StreamingZone", "SlotsZone",
                         "InspectorZone", "InputSection", "PermissionContent",
                         "InputArea", "CompletionList", "StatusLine"):
            assert selector in MINION_TCSS, f"Missing selector: {selector}"

    def test_tcss_embeds_palette_colors(self):
        from minion.tui.theme import MINION_TCSS, GOLD, DIM, SILVER
        assert GOLD   in MINION_TCSS
        assert DIM    in MINION_TCSS
        assert SILVER in MINION_TCSS

    def test_status_line_is_docked_bottom(self):
        from minion.tui.theme import MINION_TCSS
        # StatusLine block must have "dock: bottom"
        idx = MINION_TCSS.index("StatusLine")
        snippet = MINION_TCSS[idx:idx + 200]
        assert "dock: bottom" in snippet
