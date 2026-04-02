"""Tests for minion/reflection.py — ReflectionConfig, reflect(), helpers.

No real API calls. All LLM calls mocked via MagicMock + LLMResponse.
"""

import pytest
from unittest.mock import MagicMock, call

from minion.llm.base import LLMResponse, Message
from minion.reflection import (
    SCORE_THRESHOLD,
    CritiqueResult,
    ReflectionConfig,
    ReflectionResult,
    _build_critique_messages,
    _build_refine_messages,
    _parse_critique,
    reflect,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _critique_response(score: int, type_: str = "CODE_GENERATION", critique: str = "Fix it.") -> LLMResponse:
    import json
    return LLMResponse(
        content=json.dumps({"score": score, "type": type_, "critique": critique}),
        input_tokens=10,
        output_tokens=15,
        model="test-model",
    )


def _refine_response(content: str = "def improved(): pass") -> LLMResponse:
    return LLMResponse(
        content=content,
        input_tokens=20,
        output_tokens=30,
        model="test-model",
    )


def _mock_client_passing(score: int = 8) -> MagicMock:
    """Client whose critique passes on the first call (score >= threshold)."""
    client = MagicMock()
    client.complete.return_value = _critique_response(score)
    return client


def _mock_client_failing_then_passing(
    fail_score: int = 5,
    pass_score: int = 8,
    refined_text: str = "def improved(): pass",
) -> MagicMock:
    """Client that fails critique once, then passes after refinement."""
    client = MagicMock()
    client.complete.side_effect = [
        _critique_response(fail_score),    # first critique — fails
        _refine_response(refined_text),    # refine call
        _critique_response(pass_score),    # second critique — passes
    ]
    return client


# ─── _parse_critique ──────────────────────────────────────────────────────────

class TestParseCritique:
    def test_parses_valid_json_response(self):
        import json
        raw = json.dumps({"score": 8, "type": "CODE_GENERATION", "critique": "Looks good."})
        result = _parse_critique(raw)
        assert result.score == 8
        assert result.response_type == "CODE_GENERATION"
        assert result.critique == "Looks good."

    def test_falls_back_gracefully_on_missing_format(self):
        raw = "I think this is pretty good overall."
        result = _parse_critique(raw)
        assert result.score == 5       # safe default below threshold
        assert result.response_type == "GENERAL"
        assert result.raw == raw

    def test_score_clamped_to_1_to_10(self):
        import json
        raw = json.dumps({"score": 15, "type": "GENERAL", "critique": "Over the top."})
        result = _parse_critique(raw)
        assert result.score == 10

        raw_low = json.dumps({"score": -3, "type": "GENERAL", "critique": "Way too low."})
        result_low = _parse_critique(raw_low)
        assert result_low.score == 1

    def test_parses_code_generation_type(self):
        import json
        raw = json.dumps({"score": 6, "type": "CODE_GENERATION", "critique": "Missing edge case."})
        result = _parse_critique(raw)
        assert result.response_type == "CODE_GENERATION"

    def test_parses_code_explanation_type(self):
        import json
        raw = json.dumps({"score": 7, "type": "CODE_EXPLANATION", "critique": "Good."})
        result = _parse_critique(raw)
        assert result.response_type == "CODE_EXPLANATION"

    def test_parses_general_type(self):
        import json
        raw = json.dumps({"score": 9, "type": "GENERAL", "critique": None})
        result = _parse_critique(raw)
        assert result.response_type == "GENERAL"

    def test_null_critique_normalized_to_none_string(self):
        import json
        raw = json.dumps({"score": 9, "type": "GENERAL", "critique": None})
        result = _parse_critique(raw)
        assert result.critique == "None"

    def test_unknown_type_defaults_to_general(self):
        import json
        raw = json.dumps({"score": 7, "type": "SOMETHING_WEIRD", "critique": "OK."})
        result = _parse_critique(raw)
        assert result.response_type == "GENERAL"

    def test_type_value_normalized_to_uppercase(self):
        import json
        raw = json.dumps({"score": 8, "type": "code_generation", "critique": "Fine."})
        result = _parse_critique(raw)
        assert result.score == 8
        assert result.response_type == "CODE_GENERATION"

    def test_strips_markdown_code_fences(self):
        raw = "```json\n{\"score\": 7, \"type\": \"GENERAL\", \"critique\": \"Fix it.\"}\n```"
        result = _parse_critique(raw)
        assert result.score == 7
        assert result.response_type == "GENERAL"


# ─── _build_critique_messages ────────────────────────────────────────────────

class TestBuildCritiqueMessages:
    def test_returns_single_user_message(self):
        msgs = _build_critique_messages("explain quicksort", "Quicksort is...")
        assert len(msgs) == 1
        assert msgs[0].role == "user"

    def test_message_contains_prompt_and_response(self):
        msgs = _build_critique_messages("my prompt", "my response")
        content = msgs[0].content
        assert "my prompt" in content
        assert "my response" in content

    def test_each_call_returns_fresh_list(self):
        msgs1 = _build_critique_messages("p", "r")
        msgs2 = _build_critique_messages("p", "r")
        assert msgs1 is not msgs2


# ─── _build_refine_messages ──────────────────────────────────────────────────

class TestBuildRefineMessages:
    def test_returns_single_user_message(self):
        critique = CritiqueResult(score=5, response_type="CODE_GENERATION",
                                  critique="Fix edge case.", raw="")
        msgs = _build_refine_messages("my prompt", "my response", critique)
        assert len(msgs) == 1
        assert msgs[0].role == "user"

    def test_message_includes_critique_text(self):
        critique = CritiqueResult(score=5, response_type="CODE_GENERATION",
                                  critique="Handle None input.", raw="")
        msgs = _build_refine_messages("p", "r", critique)
        assert "Handle None input." in msgs[0].content

    def test_message_includes_original_response(self):
        critique = CritiqueResult(score=5, response_type="CODE_GENERATION",
                                  critique="Fix.", raw="")
        msgs = _build_refine_messages("prompt", "original response", critique)
        assert "original response" in msgs[0].content

    def test_message_includes_score(self):
        critique = CritiqueResult(score=4, response_type="CODE_GENERATION",
                                  critique="Bad.", raw="")
        msgs = _build_refine_messages("p", "r", critique)
        assert "4/10" in msgs[0].content


# ─── reflect() ────────────────────────────────────────────────────────────────

class TestReflectFunction:
    def test_returns_original_when_depth_zero(self):
        client = MagicMock()
        config = ReflectionConfig(depth=0)
        result = reflect("prompt", "response", client, config)
        assert result.final_response == "response"
        assert result.rounds == 0
        assert result.was_refined is False

    def test_no_llm_calls_when_depth_zero(self):
        client = MagicMock()
        reflect("prompt", "response", client, ReflectionConfig(depth=0))
        client.complete.assert_not_called()

    def test_stream_never_called_only_complete(self):
        """Reflection must use complete(), never stream()."""
        client = _mock_client_passing()
        reflect("prompt", "response", client, ReflectionConfig(depth=1))
        client.stream.assert_not_called()
        client.complete.assert_called()

    def test_returns_original_when_score_above_threshold(self):
        client = _mock_client_passing(score=SCORE_THRESHOLD)
        result = reflect("prompt", "def foo(): pass", client, ReflectionConfig(depth=1))
        assert result.final_response == "def foo(): pass"
        assert result.was_refined is False

    def test_refine_called_when_score_below_threshold(self):
        # depth=1: one iteration = critique(fail) + refine = 2 calls
        client = _mock_client_failing_then_passing(fail_score=SCORE_THRESHOLD - 1)
        result = reflect("prompt", "def foo(): pass", client, ReflectionConfig(depth=1))
        assert client.complete.call_count == 2
        assert result.was_refined is True

    def test_refined_response_is_final_response(self):
        client = _mock_client_failing_then_passing(refined_text="def improved(): pass")
        result = reflect("prompt", "def foo(): pass", client, ReflectionConfig(depth=1))
        assert result.final_response == "def improved(): pass"

    def test_loop_stops_at_max_depth(self):
        """With depth=1 and always-failing score, only one iteration runs."""
        client = MagicMock()
        # depth=1: one iteration = critique(fail) + refine = 2 calls, no more
        client.complete.side_effect = [
            _critique_response(3),    # critique — fail
            _refine_response("v2"),   # refine
        ]
        result = reflect("prompt", "v1", client, ReflectionConfig(depth=1))
        assert client.complete.call_count == 2
        assert result.rounds == 1

    def test_was_refined_false_when_no_refinement_needed(self):
        client = _mock_client_passing(score=9)
        result = reflect("prompt", "response", client, ReflectionConfig(depth=2))
        assert result.was_refined is False

    def test_was_refined_true_after_refinement(self):
        client = _mock_client_failing_then_passing()
        result = reflect("prompt", "original", client, ReflectionConfig(depth=1))
        assert result.was_refined is True

    def test_rounds_count_correct_when_one_round(self):
        client = _mock_client_failing_then_passing()
        result = reflect("prompt", "original", client, ReflectionConfig(depth=1))
        assert result.rounds == 1

    def test_rounds_count_zero_when_passes_immediately(self):
        client = _mock_client_passing()
        result = reflect("prompt", "response", client, ReflectionConfig(depth=2))
        assert result.rounds == 1   # one critique call, no refine

    def test_critiques_list_populated(self):
        client = _mock_client_passing(score=8)
        result = reflect("prompt", "response", client, ReflectionConfig(depth=1))
        assert len(result.critiques) == 1
        assert result.critiques[0].score == 8

    def test_final_score_reflects_last_critique(self):
        # Need depth=2 so the second critique (pass_score=9) is actually reached
        client = _mock_client_failing_then_passing(fail_score=4, pass_score=9)
        result = reflect("prompt", "response", client, ReflectionConfig(depth=2))
        assert result.final_score == 9

    def test_original_response_preserved_in_result(self):
        client = _mock_client_failing_then_passing(refined_text="new version")
        result = reflect("prompt", "original", client, ReflectionConfig(depth=1))
        assert result.original_response == "original"
        assert result.final_response == "new version"
