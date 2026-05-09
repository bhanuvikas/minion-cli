"""Tests for rate-limit helpers in minion/llm/anthropic.py.

Verifies that:
  - _is_input_token_rate_limit correctly classifies errors
  - _RATE_LIMIT_STATUS produces a well-formed countdown string
  - _MAX_RETRY and _RETRY_WAIT_SECONDS are sensible constants
  - retry logic re-raises on final attempt (sync and async paths)
  - retry logic raises InputTokenRateLimitError immediately for input-token errors

No live API calls — all Anthropic SDK objects are mocked.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import anthropic
import pytest

from minion.llm.anthropic import (
    _MAX_RETRY,
    _RATE_LIMIT_STATUS,
    _RETRY_WAIT_SECONDS,
    _is_input_token_rate_limit,
)
from minion.llm.base import InputTokenRateLimitError


# ─── _is_input_token_rate_limit ───────────────────────────────────────────────

class TestIsInputTokenRateLimit:
    def _make_error(self, message: str) -> anthropic.RateLimitError:
        """Construct a minimal RateLimitError with a given message string."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        return anthropic.RateLimitError(message=message, response=mock_response, body={})

    def test_input_tokens_phrase_detected(self):
        err = self._make_error("Rate limit exceeded: input tokens per minute")
        assert _is_input_token_rate_limit(err) is True

    def test_input_tokens_case_insensitive(self):
        err = self._make_error("Rate limit: INPUT TOKENS per minute exceeded")
        assert _is_input_token_rate_limit(err) is True

    def test_requests_per_minute_not_input_token(self):
        err = self._make_error("Rate limit exceeded: requests per minute")
        assert _is_input_token_rate_limit(err) is False

    def test_output_tokens_not_input_token(self):
        err = self._make_error("Rate limit: output tokens per minute exceeded")
        assert _is_input_token_rate_limit(err) is False

    def test_generic_rate_limit_not_input_token(self):
        err = self._make_error("Too many requests")
        assert _is_input_token_rate_limit(err) is False

    def test_empty_message_not_input_token(self):
        err = self._make_error("")
        assert _is_input_token_rate_limit(err) is False


# ─── _RATE_LIMIT_STATUS format string ────────────────────────────────────────

class TestRateLimitStatus:
    def test_format_produces_countdown_string(self):
        msg = _RATE_LIMIT_STATUS.format(30)
        assert "30" in msg
        assert "retrying" in msg.lower()

    def test_format_with_zero(self):
        msg = _RATE_LIMIT_STATUS.format(0)
        assert "0" in msg

    def test_format_is_rich_markup(self):
        msg = _RATE_LIMIT_STATUS.format(10)
        assert "[" in msg and "]" in msg


# ─── Constants sanity ────────────────────────────────────────────────────────

class TestRateLimitConstants:
    def test_max_retry_is_positive(self):
        assert _MAX_RETRY > 0

    def test_retry_wait_is_positive(self):
        assert _RETRY_WAIT_SECONDS > 0

    def test_retry_wait_at_least_ten_seconds(self):
        assert _RETRY_WAIT_SECONDS >= 10


# ─── Retry behaviour — sync complete() ───────────────────────────────────────

class TestSyncCompleteRetry:
    def _make_client(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            from minion.llm.anthropic import AnthropicClient
            return AnthropicClient(model="claude-sonnet-4-5")

    def _make_rate_limit_error(self, message: str = "requests per minute"):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        return anthropic.RateLimitError(message=message, response=mock_response, body={})

    def test_input_token_rate_limit_raises_immediately(self):
        client = self._make_client()
        err = self._make_rate_limit_error("input tokens per minute")
        with (
            patch.object(client._client.messages, "create", side_effect=err),
            patch("minion.llm.anthropic._rate_limit_wait") as mock_wait,
        ):
            with pytest.raises(InputTokenRateLimitError):
                client.complete([MagicMock()], system="")
            mock_wait.assert_not_called()

    def test_generic_rate_limit_retries_then_raises(self):
        client = self._make_client()
        err = self._make_rate_limit_error("requests per minute")
        with (
            patch.object(client._client.messages, "create", side_effect=err),
            patch("minion.llm.anthropic._rate_limit_wait") as mock_wait,
        ):
            with pytest.raises(anthropic.RateLimitError):
                client.complete([MagicMock()], system="")
            assert mock_wait.call_count == _MAX_RETRY - 1


# ─── Retry behaviour — async async_complete() ────────────────────────────────

class TestAsyncCompleteRetry:
    def _make_client(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            from minion.llm.anthropic import AnthropicClient
            return AnthropicClient(model="claude-sonnet-4-5")

    def _make_rate_limit_error(self, message: str = "requests per minute"):
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        return anthropic.RateLimitError(message=message, response=mock_response, body={})

    @pytest.mark.asyncio
    async def test_input_token_rate_limit_raises_immediately(self):
        client = self._make_client()
        err = self._make_rate_limit_error("input tokens per minute")
        with (
            patch.object(client._async_client.messages, "create", side_effect=err),
            patch("minion.llm.anthropic._rate_limit_wait_async", new_callable=AsyncMock) as mock_wait,
        ):
            with pytest.raises(InputTokenRateLimitError):
                await client.async_complete([MagicMock()], system="")
            mock_wait.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_rate_limit_retries_then_raises(self):
        client = self._make_client()
        err = self._make_rate_limit_error("requests per minute")
        with (
            patch.object(client._async_client.messages, "create", side_effect=err),
            patch("minion.llm.anthropic._rate_limit_wait_async", new_callable=AsyncMock) as mock_wait,
        ):
            with pytest.raises(anthropic.RateLimitError):
                await client.async_complete([MagicMock()], system="")
            assert mock_wait.call_count == _MAX_RETRY - 1
