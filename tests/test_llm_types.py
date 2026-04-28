"""Tests for the core LLM type layer (Message, LLMResponse, LLMClient, ContentBlocks).

These types are the stable foundation that every phase builds on.
No API calls — purely structural tests.
"""

from minion.llm.base import (
    ContentTextBlock, ContentToolResultBlock, ContentToolUseBlock,
    LLMClient, LLMResponse, Message,
)


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
