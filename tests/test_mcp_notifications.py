"""Unit tests for MCP notification and server→client request handling.

Covers every callback wired into ClientSession by _run_server():
  - logging_callback     → notifications/message (all 8 levels)
  - message_handler      → tools/resources/prompts list_changed, resources/updated, progress
  - sampling_callback    → sampling/createMessage
  - list_roots_callback  → roots/list
  - elicitation_callback → elicitation/create (form + URL, accept + cancel)

Also covers MCPManager.set_llm_client() propagation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import mcp.types as mcp_types
import pytest

from minion.mcp.config import MCPServerConfig
from minion.mcp.manager import (
    MCPManager,
    _MCPPrompt,
    _MCPPromptArg,
    _MCPTool,
    _ServerState,
    _elicitation_callback,
    _logging_callback,
    _message_handler,
    _roots_callback,
    _sampling_callback,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_state(name: str = "srv", confirm_all: bool = False,
                llm_client: Any = None) -> _ServerState:
    cfg = MCPServerConfig(name=name, command=["echo"], confirm_all=confirm_all)
    state = _ServerState(name=name, config=cfg)
    state.llm_client = llm_client
    return state


def _server_notif(inner) -> mcp_types.ServerNotification:
    return mcp_types.ServerNotification(root=inner)


def _tool_notif() -> mcp_types.ServerNotification:
    return _server_notif(mcp_types.ToolListChangedNotification(
        method="notifications/tools/list_changed"
    ))


def _resource_list_notif() -> mcp_types.ServerNotification:
    return _server_notif(mcp_types.ResourceListChangedNotification(
        method="notifications/resources/list_changed"
    ))


def _prompt_list_notif() -> mcp_types.ServerNotification:
    return _server_notif(mcp_types.PromptListChangedNotification(
        method="notifications/prompts/list_changed"
    ))


def _resource_updated_notif(uri: str = "file:///foo.py") -> mcp_types.ServerNotification:
    return _server_notif(mcp_types.ResourceUpdatedNotification(
        method="notifications/resources/updated",
        params=mcp_types.ResourceUpdatedNotificationParams(uri=uri),
    ))


def _progress_notif(progress: float, total: float | None = None,
                    message: str = "") -> mcp_types.ServerNotification:
    return _server_notif(mcp_types.ProgressNotification(
        method="notifications/progress",
        params=mcp_types.ProgressNotificationParams(
            progressToken="tok",
            progress=progress,
            total=total,
            message=message or None,
        ),
    ))


def _logging_params(level: str, msg: str,
                    logger: str | None = None) -> mcp_types.LoggingMessageNotificationParams:
    return mcp_types.LoggingMessageNotificationParams(
        level=level, data=msg, logger=logger
    )


# ── TestLoggingCallback ───────────────────────────────────────────────────────

class TestLoggingCallback:
    """notifications/message routed by level to console + Nefario."""

    @pytest.mark.asyncio
    async def test_debug_silent_to_console_but_traced(self):
        state = _make_state()
        params = _logging_params("debug", "verbose detail")

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _logging_callback(state, params)

        mock_console.print.assert_not_called()
        mock_tracer.return_value.emit.assert_called_once()
        _, kwargs = mock_tracer.return_value.emit.call_args
        assert kwargs["level"] == "debug"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["info", "notice"])
    async def test_info_notice_prints_muted(self, level):
        state = _make_state()
        params = _logging_params(level, "server started", logger="myserver")

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _logging_callback(state, params)

        printed = mock_console.print.call_args[0][0]
        assert "server started" in printed
        assert "myserver" in printed
        assert "muted" in printed

    @pytest.mark.asyncio
    async def test_warning_prints_yellow(self):
        state = _make_state()
        params = _logging_params("warning", "disk almost full")

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _logging_callback(state, params)

        printed = mock_console.print.call_args[0][0]
        assert "disk almost full" in printed
        assert "yellow" in printed
        assert "⚠" in printed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["error", "critical", "alert", "emergency"])
    async def test_error_levels_print_red(self, level):
        state = _make_state()
        params = _logging_params(level, "connection lost")

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _logging_callback(state, params)

        printed = mock_console.print.call_args[0][0]
        assert "connection lost" in printed
        assert "red" in printed
        assert "✗" in printed

    @pytest.mark.asyncio
    async def test_uses_logger_field_when_set(self):
        state = _make_state(name="srv")
        params = _logging_params("info", "hello", logger="custom_logger")

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _logging_callback(state, params)

        printed = mock_console.print.call_args[0][0]
        assert "custom_logger" in printed

    @pytest.mark.asyncio
    async def test_falls_back_to_server_name_when_no_logger(self):
        state = _make_state(name="fallback_srv")
        params = _logging_params("info", "hello", logger=None)

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _logging_callback(state, params)

        printed = mock_console.print.call_args[0][0]
        assert "fallback_srv" in printed

    @pytest.mark.asyncio
    async def test_emits_nefario_trace_with_correct_method(self):
        state = _make_state()
        params = _logging_params("error", "boom")

        tracer = MagicMock()
        with patch("minion.mcp.manager.console"), \
             patch("minion.mcp.manager.get_tracer", return_value=tracer):
            await _logging_callback(state, params)

        tracer.emit.assert_called_once()
        ev_args = tracer.emit.call_args
        assert ev_args[0][0] == "mcp_notification"
        assert ev_args[1]["method"] == "notifications/message"
        assert ev_args[1]["level"] == "error"


# ── TestMessageHandler ────────────────────────────────────────────────────────

class TestMessageHandler:
    """_message_handler handles all non-logging server notifications."""

    @pytest.mark.asyncio
    async def test_non_server_notification_is_ignored(self):
        state = _make_state()
        not_a_notif = MagicMock()  # not an instance of ServerNotification

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            await _message_handler(state, not_a_notif)

        mock_console.print.assert_not_called()

    # ── tools/list_changed ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_tools_list_changed_refetches_and_updates_state(self):
        state = _make_state()
        mock_session = AsyncMock()
        tool = MagicMock()
        tool.name = "new_tool"
        tool.description = "A fresh tool"
        tool.inputSchema = {"type": "object", "properties": {}}
        tool.annotations = None
        mock_session.list_tools.return_value = MagicMock(tools=[tool])
        state.session = mock_session

        tracer = MagicMock()
        with patch("minion.mcp.manager.console"), \
             patch("minion.mcp.manager.get_tracer", return_value=tracer):
            await _message_handler(state, _tool_notif())

        mock_session.list_tools.assert_called_once()
        assert len(state.tools) == 1
        assert state.tools[0].name == "new_tool"
        tracer.emit.assert_called_once()
        assert tracer.emit.call_args[1]["method"] == "notifications/tools/list_changed"
        assert tracer.emit.call_args[1]["tool_count"] == 1

    @pytest.mark.asyncio
    async def test_tools_list_changed_clears_old_tools_before_updating(self):
        state = _make_state()
        state.tools = [_MCPTool(name="old", description="", input_schema={})]
        mock_session = AsyncMock()
        mock_session.list_tools.return_value = MagicMock(tools=[])
        state.session = mock_session

        with patch("minion.mcp.manager.console"), \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, _tool_notif())

        assert state.tools == []

    @pytest.mark.asyncio
    async def test_tools_list_changed_marks_destructive_when_confirm_all(self):
        state = _make_state(confirm_all=True)
        mock_session = AsyncMock()
        tool = MagicMock()
        tool.name = "risky"
        tool.description = ""
        tool.inputSchema = {}
        tool.annotations = None
        mock_session.list_tools.return_value = MagicMock(tools=[tool])
        state.session = mock_session

        with patch("minion.mcp.manager.console"), \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, _tool_notif())

        assert state.tools[0].destructive is True

    @pytest.mark.asyncio
    async def test_tools_list_changed_prints_muted_notice(self):
        state = _make_state(name="myserver")
        mock_session = AsyncMock()
        mock_session.list_tools.return_value = MagicMock(tools=[])
        state.session = mock_session

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, _tool_notif())

        mock_console.print.assert_called_once()
        printed = mock_console.print.call_args[0][0]
        assert "myserver" in printed
        assert "updated" in printed.lower() or "tool" in printed.lower()

    @pytest.mark.asyncio
    async def test_tools_list_changed_no_session_does_nothing(self):
        state = _make_state()
        state.session = None

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, _tool_notif())

        mock_console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_tools_list_changed_handles_fetch_error_gracefully(self):
        state = _make_state()
        mock_session = AsyncMock()
        mock_session.list_tools.side_effect = RuntimeError("server gone")
        state.session = mock_session

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, _tool_notif())  # must not raise

        printed = mock_console.print.call_args[0][0]
        assert "server gone" in printed

    # ── resources/list_changed ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_resource_list_changed_refetches_silently(self):
        state = _make_state()
        state.has_resources = False
        mock_session = AsyncMock()
        mock_session.list_resources.return_value = MagicMock()
        state.session = mock_session

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, _resource_list_notif())

        mock_session.list_resources.assert_called_once()
        assert state.has_resources is True
        mock_console.print.assert_not_called()

    # ── prompts/list_changed ──────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_prompt_list_changed_refetches_and_updates_state(self):
        state = _make_state()
        state.prompts = [_MCPPrompt(name="old_prompt", description="")]
        mock_session = AsyncMock()
        prompt = MagicMock()
        prompt.name = "new_prompt"
        prompt.description = "fresh"
        arg = MagicMock()
        arg.name = "q"
        arg.description = "query"
        arg.required = True
        prompt.arguments = [arg]
        mock_session.list_prompts.return_value = MagicMock(prompts=[prompt])
        state.session = mock_session

        with patch("minion.mcp.manager.console"), \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, _prompt_list_notif())

        assert len(state.prompts) == 1
        assert state.prompts[0].name == "new_prompt"
        assert state.prompts[0].arguments[0].name == "q"
        assert state.prompts[0].arguments[0].required is True

    # ── resources/updated ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_resource_updated_prints_uri_and_traces(self):
        state = _make_state(name="filesrv")
        uri = "file:///project/main.py"

        tracer = MagicMock()
        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer", return_value=tracer):
            await _message_handler(state, _resource_updated_notif(uri))

        mock_console.print.assert_called_once()
        printed = mock_console.print.call_args[0][0]
        assert "main.py" in printed
        assert "filesrv" in printed

        tracer.emit.assert_called_once()
        assert tracer.emit.call_args[1]["method"] == "notifications/resources/updated"
        assert "main.py" in tracer.emit.call_args[1]["uri"]

    # ── progress ──────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_progress_with_total_prints_n_of_total(self):
        state = _make_state(name="indexer")
        notif = _progress_notif(progress=3, total=10, message="Scanning files")

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, notif)

        printed = mock_console.print.call_args[0][0]
        assert "3/10" in printed
        assert "Scanning files" in printed
        assert "indexer" in printed

    @pytest.mark.asyncio
    async def test_progress_without_total_prints_just_count(self):
        state = _make_state()
        notif = _progress_notif(progress=7, total=None, message="")

        with patch("minion.mcp.manager.console") as mock_console, \
             patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _message_handler(state, notif)

        printed = mock_console.print.call_args[0][0]
        assert "7" in printed
        assert "7/" not in printed  # no "N/total" pattern when total is absent


# ── TestSamplingCallback ──────────────────────────────────────────────────────

class TestSamplingCallback:
    """sampling/createMessage — server delegates an LLM call to minion."""

    def _make_params(self, text: str = "Say hello", system: str = "",
                     max_tokens: int = 100) -> mcp_types.CreateMessageRequestParams:
        msgs = [mcp_types.SamplingMessage(
            role="user",
            content=mcp_types.TextContent(type="text", text=text),
        )]
        return mcp_types.CreateMessageRequestParams(
            messages=msgs,
            maxTokens=max_tokens,
            systemPrompt=system or None,
        )

    def _make_llm_client(self, response_text: str = "Hello!",
                         model: str = "claude-test") -> MagicMock:
        from minion.llm.base import LLMResponse
        client = MagicMock()
        client.async_complete = AsyncMock(return_value=LLMResponse(
            content=response_text, input_tokens=10, output_tokens=5, model=model,
        ))
        return client

    @pytest.mark.asyncio
    async def test_returns_error_when_no_llm_client(self):
        state = _make_state(llm_client=None)
        params = self._make_params()
        ctx = MagicMock()

        result = await _sampling_callback(state, ctx, params)

        assert isinstance(result, mcp_types.ErrorData)
        assert result.code == -32603
        assert "No LLM client" in result.message

    @pytest.mark.asyncio
    async def test_calls_llm_and_returns_result(self):
        client = self._make_llm_client("The answer is 42")
        state = _make_state(llm_client=client)
        params = self._make_params("What is the answer?")
        ctx = MagicMock()

        tracer = MagicMock()
        with patch("minion.mcp.manager.get_tracer", return_value=tracer):
            result = await _sampling_callback(state, ctx, params)

        assert isinstance(result, mcp_types.CreateMessageResult)
        assert result.role == "assistant"
        assert result.content.text == "The answer is 42"
        assert result.stopReason == "endTurn"

    @pytest.mark.asyncio
    async def test_passes_system_prompt_to_llm(self):
        client = self._make_llm_client()
        state = _make_state(llm_client=client)
        params = self._make_params("hi", system="You are a pirate.")
        ctx = MagicMock()

        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _sampling_callback(state, ctx, params)

        _, call_kwargs = client.async_complete.call_args
        assert call_kwargs.get("system") == "You are a pirate."

    @pytest.mark.asyncio
    async def test_converts_multiple_messages(self):
        client = self._make_llm_client()
        state = _make_state(llm_client=client)
        msgs = [
            mcp_types.SamplingMessage(role="user",
                content=mcp_types.TextContent(type="text", text="first")),
            mcp_types.SamplingMessage(role="assistant",
                content=mcp_types.TextContent(type="text", text="second")),
            mcp_types.SamplingMessage(role="user",
                content=mcp_types.TextContent(type="text", text="third")),
        ]
        params = mcp_types.CreateMessageRequestParams(messages=msgs, maxTokens=100)
        ctx = MagicMock()

        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            await _sampling_callback(state, ctx, params)

        passed_messages, _ = client.async_complete.call_args[0], client.async_complete.call_args[1]
        sent = client.async_complete.call_args[0][0]
        assert len(sent) == 3
        assert sent[0].role == "user" and sent[0].content == "first"
        assert sent[1].role == "assistant" and sent[1].content == "second"

    @pytest.mark.asyncio
    async def test_returns_error_on_llm_exception(self):
        client = MagicMock()
        client.async_complete = AsyncMock(side_effect=RuntimeError("API timeout"))
        state = _make_state(llm_client=client)
        params = self._make_params()
        ctx = MagicMock()

        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            result = await _sampling_callback(state, ctx, params)

        assert isinstance(result, mcp_types.ErrorData)
        assert "API timeout" in result.message

    @pytest.mark.asyncio
    async def test_emits_nefario_trace(self):
        client = self._make_llm_client()
        state = _make_state(llm_client=client)
        params = self._make_params()
        ctx = MagicMock()

        tracer = MagicMock()
        with patch("minion.mcp.manager.get_tracer", return_value=tracer):
            await _sampling_callback(state, ctx, params)

        tracer.emit.assert_called_once()
        assert tracer.emit.call_args[1]["method"] == "sampling/createMessage"

    @pytest.mark.asyncio
    async def test_result_model_matches_llm_response(self):
        client = self._make_llm_client(response_text="pong", model="claude-haiku")
        state = _make_state(llm_client=client)
        params = self._make_params()
        ctx = MagicMock()

        with patch("minion.mcp.manager.get_tracer") as mock_tracer:
            mock_tracer.return_value = MagicMock()
            result = await _sampling_callback(state, ctx, params)

        assert result.model == "claude-haiku"
        assert result.content.text == "pong"


# ── TestRootsCallback ─────────────────────────────────────────────────────────

class TestRootsCallback:
    """roots/list — returns cwd as the exposed filesystem root."""

    @pytest.mark.asyncio
    async def test_returns_cwd_as_file_uri(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ctx = MagicMock()

        result = await _roots_callback(ctx)

        assert isinstance(result, mcp_types.ListRootsResult)
        assert len(result.roots) == 1
        assert str(result.roots[0].uri) == tmp_path.as_uri()

    @pytest.mark.asyncio
    async def test_root_name_is_directory_name(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ctx = MagicMock()

        result = await _roots_callback(ctx)

        assert result.roots[0].name == tmp_path.name

    @pytest.mark.asyncio
    async def test_uri_is_file_scheme(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ctx = MagicMock()

        result = await _roots_callback(ctx)

        assert str(result.roots[0].uri).startswith("file://")


# ── TestElicitationCallback ───────────────────────────────────────────────────

class TestElicitationCallback:
    """elicitation/create — interactive form/URL prompts surfaced via questionary."""

    def _make_form_params(self, schema: dict,
                          message: str = "Please fill in") -> mcp_types.ElicitRequestFormParams:
        return mcp_types.ElicitRequestFormParams(
            message=message,
            requestedSchema=schema,
        )

    # ── URL elicitation ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_url_elicitation_opens_browser_and_accepts(self):
        state = _make_state()
        params = mcp_types.ElicitRequestURLParams(
            message="Authorize in your browser",
            url="https://example.com/auth",
            elicitationId="elicit-1",
        )
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("webbrowser.open") as mock_browser, \
             patch("asyncio.to_thread", new=AsyncMock(return_value=True)):
            result = await _elicitation_callback(state, ctx, params)

        mock_browser.assert_called_once_with("https://example.com/auth")
        assert isinstance(result, mcp_types.ElicitResult)
        assert result.action == "accept"

    @pytest.mark.asyncio
    async def test_url_elicitation_cancel_when_user_declines(self):
        state = _make_state()
        params = mcp_types.ElicitRequestURLParams(
            message="Authorize",
            url="https://example.com/auth",
            elicitationId="elicit-2",
        )
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("webbrowser.open"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value=False)):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "cancel"

    # ── Form elicitation — string field ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_form_string_field_returns_text_answer(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {"branch": {"type": "string", "description": "Branch name"}},
            "required": ["branch"],
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value="main")):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "accept"
        assert result.content["branch"] == "main"

    # ── Form elicitation — boolean field ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_form_boolean_field_returns_bool(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {"force": {"type": "boolean", "description": "Force push?"}},
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value=True)):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "accept"
        assert result.content["force"] is True

    # ── Form elicitation — enum field ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_form_enum_field_returns_selection(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {
                "env": {"type": "string", "enum": ["dev", "staging", "prod"],
                        "description": "Environment"}
            },
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value="staging")):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "accept"
        assert result.content["env"] == "staging"

    # ── Form elicitation — integer field ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_form_integer_field_parsed_from_string(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "How many?"}},
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value="42")):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "accept"
        assert result.content["count"] == 42

    # ── Form elicitation — number (float) field ───────────────────────────────

    @pytest.mark.asyncio
    async def test_form_number_field_parsed_as_float(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {"threshold": {"type": "number", "description": "Threshold"}},
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value="0.75")):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "accept"
        assert result.content["threshold"] == pytest.approx(0.75)

    # ── Form elicitation — multiple fields ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_form_multiple_fields_collected_in_order(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name"},
                "dry_run": {"type": "boolean", "description": "Dry run?"},
            },
            "required": ["name"],
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        answers = iter(["alice", True])
        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(side_effect=lambda fn: next(answers))):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "accept"
        assert result.content["name"] == "alice"
        assert result.content["dry_run"] is True

    # ── Cancellation paths ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_form_cancel_when_user_returns_none(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {"branch": {"type": "string", "description": "Branch"}},
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(return_value=None)):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "cancel"

    @pytest.mark.asyncio
    async def test_form_cancel_on_keyboard_interrupt(self):
        state = _make_state()
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string", "description": "x"}},
        }
        params = self._make_form_params(schema)
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"), \
             patch("asyncio.to_thread", new=AsyncMock(side_effect=KeyboardInterrupt)):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "cancel"

    @pytest.mark.asyncio
    async def test_no_schema_returns_accept_with_empty_content(self):
        state = _make_state()
        # ElicitRequestFormParams with empty schema
        params = mcp_types.ElicitRequestFormParams(message="ok?", requestedSchema={})
        ctx = MagicMock()

        with patch("minion.mcp.manager.console"):
            result = await _elicitation_callback(state, ctx, params)

        assert result.action == "accept"
        assert result.content == {}

    @pytest.mark.asyncio
    async def test_prints_server_name_and_message(self):
        state = _make_state(name="auth_server")
        params = mcp_types.ElicitRequestFormParams(
            message="Which region?", requestedSchema={}
        )
        ctx = MagicMock()

        with patch("minion.mcp.manager.console") as mock_console:
            await _elicitation_callback(state, ctx, params)

        printed = mock_console.print.call_args[0][0]
        assert "auth_server" in printed
        assert "Which region?" in printed


# ── TestMCPManagerSetLlmClient ────────────────────────────────────────────────

class TestMCPManagerSetLlmClient:
    """set_llm_client() propagates the LLM client to all connected server states."""

    def test_set_llm_client_populates_all_states(self):
        manager = MCPManager()
        cfg = MCPServerConfig(name="s", command=["echo"])
        state_a = _ServerState(name="a", config=cfg)
        state_b = _ServerState(name="b", config=cfg)
        manager._states = {"a": state_a, "b": state_b}

        fake_client = MagicMock()
        manager.set_llm_client(fake_client)

        assert state_a.llm_client is fake_client
        assert state_b.llm_client is fake_client

    def test_set_llm_client_on_empty_manager_is_noop(self):
        manager = MCPManager()
        manager.set_llm_client(MagicMock())  # must not raise

    def test_set_llm_client_overwrites_previous_client(self):
        manager = MCPManager()
        cfg = MCPServerConfig(name="s", command=["echo"])
        state = _ServerState(name="s", config=cfg)
        state.llm_client = MagicMock()
        manager._states = {"s": state}

        new_client = MagicMock()
        manager.set_llm_client(new_client)

        assert state.llm_client is new_client
