"""Tests for the core LLM type layer (Message, LLMResponse, LLMClient, ContentBlocks).

These types are the stable foundation that every phase builds on.
No API calls — purely structural tests.
"""

from minion.llm.base import (
    ContentTextBlock, ContentToolResultBlock, ContentToolUseBlock,
    LLMClient, LLMResponse, Message, ToolDefinition,
)


# ─── ToolDefinition ───────────────────────────────────────────────────────────

class TestToolDefinition:
    def test_required_fields(self):
        td = ToolDefinition(name="read_file", description="Read a file")
        assert td.name == "read_file"
        assert td.description == "Read a file"

    def test_parameters_defaults_to_empty_object_schema(self):
        td = ToolDefinition(name="x", description="y")
        assert td.parameters == {"type": "object", "properties": {}}

    def test_explicit_parameters(self):
        params = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
        td = ToolDefinition(name="read_file", description="Read a file", parameters=params)
        assert td.parameters == params

    def test_equality(self):
        a = ToolDefinition("t", "d", {"type": "object", "properties": {}})
        b = ToolDefinition("t", "d", {"type": "object", "properties": {}})
        assert a == b

    def test_inequality_on_name(self):
        a = ToolDefinition("a", "d")
        b = ToolDefinition("b", "d")
        assert a != b

    def test_name_attribute_access(self):
        td = ToolDefinition(name="run_shell", description="Run a shell command")
        assert td.name == "run_shell"

    def test_not_subscriptable(self):
        td = ToolDefinition(name="x", description="y")
        try:
            _ = td["name"]
            assert False, "Should have raised TypeError"
        except TypeError:
            pass


# ─── AnthropicClient._to_provider_format ─────────────────────────────────────

class TestAnthropicProviderFormat:
    def _fmt(self, tools):
        from minion.llm.anthropic import AnthropicClient
        return AnthropicClient._to_provider_format(tools)

    def test_empty_list_returns_empty(self):
        assert self._fmt([]) == []

    def test_single_tool_has_cache_control(self):
        td = ToolDefinition("read_file", "Read a file", {"type": "object", "properties": {}})
        result = self._fmt([td])
        assert len(result) == 1
        assert result[0]["name"] == "read_file"
        assert result[0]["description"] == "Read a file"
        assert result[0]["input_schema"] == {"type": "object", "properties": {}}
        assert result[0]["cache_control"] == {"type": "ephemeral"}

    def test_multiple_tools_only_last_has_cache_control(self):
        tools = [
            ToolDefinition("read_file", "Read", {"type": "object", "properties": {}}),
            ToolDefinition("run_shell", "Shell", {"type": "object", "properties": {}}),
            ToolDefinition("write_file", "Write", {"type": "object", "properties": {}}),
        ]
        result = self._fmt(tools)
        assert len(result) == 3
        assert "cache_control" not in result[0]
        assert "cache_control" not in result[1]
        assert result[2]["cache_control"] == {"type": "ephemeral"}

    def test_parameters_mapped_to_input_schema(self):
        params = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}
        td = ToolDefinition("run_shell", "Run", params)
        result = self._fmt([td])
        assert result[0]["input_schema"] == params

    def test_original_tools_not_mutated(self):
        td = ToolDefinition("x", "y")
        original_dict = {"name": "x", "description": "y", "input_schema": td.parameters}
        result = self._fmt([td])
        assert "cache_control" in result[0]
        assert td.parameters == {"type": "object", "properties": {}}


# ─── TOOL_DEFINITIONS consistency ─────────────────────────────────────────────

class TestToolDefinitionsConsistency:
    def test_all_entries_are_tool_definitions(self):
        from minion.tools.definitions import TOOL_DEFINITIONS
        for t in TOOL_DEFINITIONS:
            assert isinstance(t, ToolDefinition), f"{t!r} is not a ToolDefinition"

    def test_names_are_unique(self):
        from minion.tools.definitions import TOOL_DEFINITIONS
        names = [t.name for t in TOOL_DEFINITIONS]
        assert len(names) == len(set(names))

    def test_spawn_agent_present(self):
        from minion.tools.definitions import TOOL_DEFINITIONS
        assert any(t.name == "spawn_agent" for t in TOOL_DEFINITIONS)

    def test_send_remote_task_present(self):
        from minion.tools.definitions import TOOL_DEFINITIONS
        assert any(t.name == "send_remote_task" for t in TOOL_DEFINITIONS)

    def test_all_have_parameters_with_type(self):
        from minion.tools.definitions import TOOL_DEFINITIONS
        for t in TOOL_DEFINITIONS:
            assert "type" in t.parameters, f"{t.name} missing 'type' in parameters"


# ─── ContentBlock types ───────────────────────────────────────────────────────

class TestContentBlocks:
    def test_content_text_block_fields(self):
        b = ContentTextBlock(text="hello")
        assert b.text == "hello"

    def test_content_tool_use_block_fields(self):
        b = ContentToolUseBlock(id="toolu_01", name="read_file", input={"path": "x.py"})
        assert b.id == "toolu_01"
        assert b.name == "read_file"
        assert b.input == {"path": "x.py"}

    def test_content_tool_use_block_default_input(self):
        b = ContentToolUseBlock(id="toolu_01", name="list_directory")
        assert b.input == {}

    def test_content_tool_result_block_fields(self):
        b = ContentToolResultBlock(tool_use_id="toolu_01", content="file contents")
        assert b.tool_use_id == "toolu_01"
        assert b.content == "file contents"

    def test_content_blocks_are_distinct_types(self):
        assert ContentTextBlock(text="x") != ContentToolUseBlock(id="x", name="x")


# ─── Message ──────────────────────────────────────────────────────────────────

class TestMessage:
    def test_fields(self):
        m = Message(role="user", content="hello")
        assert m.role == "user"
        assert m.content == "hello"

    def test_roles(self):
        for role in ("user", "assistant", "system"):
            m = Message(role=role, content="x")
            assert m.role == role

    def test_equality(self):
        assert Message("user", "hi") == Message("user", "hi")
        assert Message("user", "hi") != Message("assistant", "hi")

    def test_content_can_be_list_of_blocks(self):
        blocks = [
            ContentTextBlock(text="I'll read that."),
            ContentToolUseBlock(id="toolu_01", name="read_file", input={"path": "x.py"}),
        ]
        m = Message(role="assistant", content=blocks)
        assert isinstance(m.content, list)
        assert len(m.content) == 2

    def test_tool_result_message(self):
        m = Message(role="user", content=[
            ContentToolResultBlock(tool_use_id="toolu_01", content="file data")
        ])
        assert m.role == "user"
        assert isinstance(m.content[0], ContentToolResultBlock)


class TestLLMResponse:
    def test_fields(self):
        r = LLMResponse(
            content="Hello!",
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-5",
        )
        assert r.content == "Hello!"
        assert r.input_tokens == 10
        assert r.output_tokens == 5
        assert r.model == "claude-sonnet-4-5"

    def test_zero_tokens(self):
        # Usage-only response (content="") used internally for streaming metadata
        r = LLMResponse(content="", input_tokens=0, output_tokens=0, model="x")
        assert r.input_tokens == 0
        assert r.output_tokens == 0


class TestLLMClientInterface:
    def test_cannot_instantiate_abstract(self):
        """LLMClient is abstract — instantiating it directly must raise TypeError."""
        try:
            LLMClient()  # type: ignore
            assert False, "Should have raised TypeError"
        except TypeError:
            pass

    def test_concrete_subclass_must_implement_all_methods(self):
        """A subclass missing any abstract method also cannot be instantiated."""

        class IncompleteClient(LLMClient):
            # Missing: complete, stream, model_id, provider_name
            pass

        try:
            IncompleteClient()
            assert False, "Should have raised TypeError"
        except TypeError:
            pass

    def test_last_usage_default_is_none(self):
        """The default last_usage on the base class returns None."""

        class MinimalClient(LLMClient):
            def complete(self, messages, system=""):
                return LLMResponse("", 0, 0, "m")

            def stream(self, messages, system=""):
                yield ""

            async def async_complete(self, messages, system=""):
                raise NotImplementedError

            async def async_stream(self, messages, system="", tools=None):
                raise NotImplementedError
                yield

            @property
            def model_id(self):
                return "test-model"

            @property
            def provider_name(self):
                return "test"

        client = MinimalClient()
        assert client.last_usage is None
