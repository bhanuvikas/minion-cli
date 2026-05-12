"""Headless smoke tests for MinionApp (Textual).

Uses App.run_test() to mount the widget tree without a real TTY.
Verifies that compose() produces the expected widget IDs and that the
public API surface (update_session, set_thinking, show/hide permission,
etc.) is callable without error.
"""
from __future__ import annotations

import pytest

from minion.tui.app import (
    MinionApp,
    CompletionList,
    ConversationArea,
    InputRow,
    InputSection,
    PermissionContent,
    StatusLine,
)


# ── Widget tree ───────────────────────────────────────────────────────────────

class TestCompose:
    @pytest.mark.asyncio
    async def test_all_expected_widget_ids_exist(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#conv-area",          ConversationArea)
            assert app.query_one("#input-section",      InputSection)
            assert app.query_one("#permission-content", PermissionContent)
            assert app.query_one("#input-row",          InputRow)
            assert app.query_one("#completion-list",    CompletionList)
            assert app.query_one("#status-line",        StatusLine)

    @pytest.mark.asyncio
    async def test_component_state_machines_attached(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.conversation is not None
            assert app.permission   is not None
            assert app.status       is not None
            assert app.slots        is not None


# ── Initial visibility ────────────────────────────────────────────────────────

class TestInitialVisibility:
    @pytest.mark.asyncio
    async def test_slots_widget_absent_on_mount(self):
        """Slots widget is created on demand; ConversationArea has no slots child at startup."""
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#conv-area", ConversationArea)._slots_widget is None

    @pytest.mark.asyncio
    async def test_permission_content_hidden_on_mount(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert not app.query_one("#permission-content").display

    @pytest.mark.asyncio
    async def test_completion_list_hidden_on_mount(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert not app.query_one("#completion-list").display

    @pytest.mark.asyncio
    async def test_input_row_visible_on_mount(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#input-row").display

    @pytest.mark.asyncio
    async def test_status_line_visible_on_mount(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#status-line").display


# ── Public API calls ──────────────────────────────────────────────────────────

class TestPublicApi:
    @pytest.mark.asyncio
    async def test_update_session_does_not_raise(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.update_session(
                model="claude-opus-4",
                provider="anthropic",
                project="my-project",
                cwd="/tmp",
                memory=True,
                agents=3,
                version="1.2.3",
            )

    @pytest.mark.asyncio
    async def test_set_thinking_toggle_does_not_raise(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.set_thinking(True)
            app.set_thinking(False)

    @pytest.mark.asyncio
    async def test_invalidate_does_not_raise(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.invalidate()

    @pytest.mark.asyncio
    async def test_scroll_to_bottom_does_not_raise(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.scroll_to_bottom()

    @pytest.mark.asyncio
    async def test_flush_writes_async_does_not_raise(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.flush_writes_async()


# ── Permission show/hide ──────────────────────────────────────────────────────

class TestPermissionShowHide:
    @pytest.mark.asyncio
    async def test_show_permission_hides_input_row(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_permission()
            assert not app.query_one("#input-row").display

    @pytest.mark.asyncio
    async def test_show_permission_reveals_permission_content(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_permission()
            assert app.query_one("#permission-content").display

    @pytest.mark.asyncio
    async def test_show_permission_adds_active_class(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_permission()
            assert "permission-active" in app.query_one("#input-section").classes

    @pytest.mark.asyncio
    async def test_hide_permission_restores_input_row(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_permission()
            app.hide_permission()
            assert app.query_one("#input-row").display

    @pytest.mark.asyncio
    async def test_hide_permission_conceals_permission_content(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_permission()
            app.hide_permission()
            assert not app.query_one("#permission-content").display

    @pytest.mark.asyncio
    async def test_hide_permission_removes_active_class(self):
        app = MinionApp(model_name="claude-test")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_permission()
            app.hide_permission()
            assert "permission-active" not in app.query_one("#input-section").classes


# ── Legacy constructor params ─────────────────────────────────────────────────

class TestLegacyConstructorParams:
    @pytest.mark.asyncio
    async def test_completer_param_ignored_gracefully(self):
        """completer= was the old API; ignored in Textual version."""
        from unittest.mock import MagicMock
        app = MinionApp(model_name="claude-test", completer=MagicMock())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one("#conv-area", ConversationArea)
