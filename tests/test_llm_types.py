"""Tests for the core LLM type layer (Message, LLMResponse, LLMClient).

These types are the stable foundation that every phase builds on.
No API calls — purely structural tests.
"""

from minion.llm.base import LLMClient, LLMResponse, Message


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

            @property
            def model_id(self):
                return "test-model"

            @property
            def provider_name(self):
                return "test"

        client = MinimalClient()
        assert client.last_usage is None
