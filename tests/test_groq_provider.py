"""
Unit tests for ai/providers/groq_provider.py
----------------------------------------------
All Groq API calls are mocked — no real network traffic, no real keys needed.

Run with:
    pytest tests/test_groq_provider.py -v
    pytest tests/test_groq_provider.py -v --tb=short   # shorter tracebacks
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from ai.providers.base import ProviderPayloadTooLargeError, ProviderRateLimitError
from ai.providers.groq_provider import GroqProvider, _parse_retry_after


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_response(content: str) -> MagicMock:
    """Build a fake Groq chat-completion response object."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_rate_limit_error(
    retry_after_header: str | None = None,
    message: str = "Rate limit reached",
) -> MagicMock:
    """Build a fake RateLimitError with optional retry-after header."""
    from groq import RateLimitError

    headers = {}
    if retry_after_header is not None:
        headers["retry-after"] = retry_after_header

    response_mock = MagicMock()
    response_mock.headers = headers

    # RateLimitError needs (message, response, body)
    err = RateLimitError(message, response=response_mock, body={})
    err.response = response_mock
    return err


def _make_api_error(
    message: str = "Service Unavailable",
    *,
    status_code: int | None = None,
) -> MagicMock:
    """Build a fake Groq APIError."""
    from groq import APIError

    request_mock = MagicMock()
    err = APIError(message, request_mock, body={})
    response_mock = MagicMock()
    response_mock.status_code = status_code
    err.response = response_mock
    return err


@pytest.fixture
def provider():
    """Return a GroqProvider with a patched Groq client so no real calls are made."""
    with patch("ai.providers.groq_provider.Groq") as MockGroq:
        p = GroqProvider(api_key="test-key-123", model="llama-3.3-70b-versatile")
        p._mock_client = MockGroq.return_value          # convenience ref
        p._mock_completions = p._mock_client.chat.completions
        yield p


# ─────────────────────────────────────────────────────────────────────────────
# 1. Construction & properties
# ─────────────────────────────────────────────────────────────────────────────

class TestGroqProviderConstruction:
    def test_provider_name_is_groq(self, provider):
        assert provider.provider_name == "groq"

    def test_model_name_is_set_correctly(self, provider):
        assert provider.model_name == "llama-3.3-70b-versatile"

    def test_default_model_used_when_not_specified(self):
        with patch("ai.providers.groq_provider.Groq"):
            p = GroqProvider(api_key="key")
        assert p.model_name == "llama-3.3-70b-versatile"

    def test_custom_model_stored(self):
        with patch("ai.providers.groq_provider.Groq"):
            p = GroqProvider(api_key="key", model="mixtral-8x7b-32768")
        assert p.model_name == "mixtral-8x7b-32768"

    def test_groq_client_created_with_no_retries(self):
        """max_retries=0 means we handle retries ourselves — verify it's set."""
        with patch("ai.providers.groq_provider.Groq") as MockGroq:
            GroqProvider(api_key="my-api-key")
            MockGroq.assert_called_once_with(api_key="my-api-key", max_retries=0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Successful generation
# ─────────────────────────────────────────────────────────────────────────────

class TestGroqProviderGenerate:
    def test_returns_content_string_on_success(self, provider):
        """Happy path — API returns valid JSON string."""
        fake_json = '{"score": 9, "feedback": "Great response"}'
        provider._mock_completions.create.return_value = _make_response(fake_json)

        result = provider.generate("You are an auditor.", "Evaluate this conversation.")

        assert result == fake_json

    def test_passes_correct_messages_to_api(self, provider):
        """Verify system + user messages are passed in the right format."""
        provider._mock_completions.create.return_value = _make_response('{"ok": true}')

        provider.generate("SYS_PROMPT", "USER_CONTENT")

        call_kwargs = provider._mock_completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0] == {"role": "system", "content": "SYS_PROMPT"}
        assert messages[1] == {"role": "user",   "content": "USER_CONTENT"}

    def test_passes_json_object_response_format(self, provider):
        """response_format must always be json_object to enforce JSON output."""
        provider._mock_completions.create.return_value = _make_response('{"ok": true}')

        provider.generate("sys", "usr")

        call_kwargs = provider._mock_completions.create.call_args.kwargs
        assert call_kwargs["response_format"] == {"type": "json_object"}

    def test_default_max_tokens_and_temperature(self, provider):
        provider._mock_completions.create.return_value = _make_response('{"ok": true}')

        provider.generate("sys", "usr")

        call_kwargs = provider._mock_completions.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 1200
        assert call_kwargs["temperature"] == 0.1

    def test_custom_max_tokens_and_temperature(self, provider):
        provider._mock_completions.create.return_value = _make_response('{"ok": true}')

        provider.generate("sys", "usr", max_tokens=500, temperature=0.7)

        call_kwargs = provider._mock_completions.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 500
        assert call_kwargs["temperature"] == 0.7

    def test_model_name_passed_to_api(self, provider):
        provider._mock_completions.create.return_value = _make_response('{"ok": true}')

        provider.generate("sys", "usr")

        call_kwargs = provider._mock_completions.create.call_args.kwargs
        assert call_kwargs["model"] == "llama-3.3-70b-versatile"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Inline rate-limit messages (edge case specific to Groq)
# ─────────────────────────────────────────────────────────────────────────────

class TestInlineRateLimitDetection:
    """Groq sometimes returns a rate-limit message *inside* a 200 response body."""

    @pytest.mark.parametrize("inline_msg", [
        "limit of allowed tokens per minute",
        "Rate limit reached for model xyz",
        "You have hit the limit of allowed tokens in the response",
    ])
    def test_raises_provider_rate_limit_for_inline_messages(self, provider, inline_msg):
        provider._mock_completions.create.return_value = _make_response(inline_msg)

        with pytest.raises(ProviderRateLimitError):
            provider.generate("sys", "usr")

    def test_inline_rate_limit_has_60_second_retry_after(self, provider):
        provider._mock_completions.create.return_value = _make_response(
            "Rate limit reached: please wait"
        )

        with pytest.raises(ProviderRateLimitError) as exc_info:
            provider.generate("sys", "usr")

        assert exc_info.value.retry_after == 60.0

    def test_normal_json_content_does_not_trigger_inline_detection(self, provider):
        """Ensure valid JSON content is never mistaken for a rate-limit message."""
        valid_json = '{"score": 8, "feedback": "Good job"}'
        provider._mock_completions.create.return_value = _make_response(valid_json)

        result = provider.generate("sys", "usr")
        assert result == valid_json

    def test_none_content_does_not_raise(self, provider):
        """If content is None the inline check is skipped and None is returned."""
        provider._mock_completions.create.return_value = _make_response(None)

        result = provider.generate("sys", "usr")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. RateLimitError (HTTP 429) handling
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimitErrorHandling:
    def test_rate_limit_error_raises_provider_rate_limit_error(self, provider):
        from groq import RateLimitError
        provider._mock_completions.create.side_effect = _make_rate_limit_error()

        with pytest.raises(ProviderRateLimitError):
            provider.generate("sys", "usr")

    def test_rate_limit_error_wraps_original_exception(self, provider):
        from groq import RateLimitError
        provider._mock_completions.create.side_effect = _make_rate_limit_error()

        with pytest.raises(ProviderRateLimitError) as exc_info:
            provider.generate("sys", "usr")

        assert exc_info.value.__cause__ is not None

    def test_retry_after_extracted_from_header(self, provider):
        provider._mock_completions.create.side_effect = _make_rate_limit_error(
            retry_after_header="30"
        )

        with pytest.raises(ProviderRateLimitError) as exc_info:
            provider.generate("sys", "usr")

        assert exc_info.value.retry_after == 30.0

    def test_retry_after_none_when_no_header(self, provider):
        provider._mock_completions.create.side_effect = _make_rate_limit_error(
            retry_after_header=None
        )

        with pytest.raises(ProviderRateLimitError) as exc_info:
            provider.generate("sys", "usr")

        # No header, no message match → should be None
        assert exc_info.value.retry_after is None


# ─────────────────────────────────────────────────────────────────────────────
# 5. Generic APIError (503, overloaded, etc.)
# ─────────────────────────────────────────────────────────────────────────────

class TestAPIErrorHandling:
    def test_payload_too_large_raises_provider_payload_too_large_error(self, provider):
        provider._mock_completions.create.side_effect = _make_api_error(
            "413 Payload Too Large",
            status_code=413,
        )

        with pytest.raises(ProviderPayloadTooLargeError):
            provider.generate("sys", "usr")

    def test_api_error_raises_provider_rate_limit_error(self, provider):
        from groq import APIError
        provider._mock_completions.create.side_effect = _make_api_error("503 Overloaded")

        with pytest.raises(ProviderRateLimitError):
            provider.generate("sys", "usr")

    def test_api_error_retry_after_is_20_seconds(self, provider):
        """Generic API errors use a fixed 20-second cooldown to trigger rotation."""
        from groq import APIError
        provider._mock_completions.create.side_effect = _make_api_error()

        with pytest.raises(ProviderRateLimitError) as exc_info:
            provider.generate("sys", "usr")

        assert exc_info.value.retry_after == 20.0

    def test_api_error_message_contains_rotated_label(self, provider):
        """Error message should signal pool manager to rotate the key."""
        from groq import APIError
        provider._mock_completions.create.side_effect = _make_api_error("upstream error")

        with pytest.raises(ProviderRateLimitError) as exc_info:
            provider.generate("sys", "usr")

        assert "rotated" in str(exc_info.value).lower()

    def test_api_error_wraps_original_exception(self, provider):
        from groq import APIError
        provider._mock_completions.create.side_effect = _make_api_error()

        with pytest.raises(ProviderRateLimitError) as exc_info:
            provider.generate("sys", "usr")

        assert exc_info.value.__cause__ is not None


# ─────────────────────────────────────────────────────────────────────────────
# 6. _parse_retry_after helper function
# ─────────────────────────────────────────────────────────────────────────────

class TestParseRetryAfter:
    def _make_exc(self, headers: dict, message: str = "rate limited") -> object:
        """Build a minimal exception-like object for _parse_retry_after."""
        from groq import RateLimitError
        response_mock = MagicMock()
        response_mock.headers = headers
        err = RateLimitError(message, response=response_mock, body={})
        err.response = response_mock
        return err

    def test_reads_retry_after_header_in_seconds(self):
        exc = self._make_exc({"retry-after": "45"})
        result = _parse_retry_after(exc)
        assert result == 45.0

    def test_reads_retry_after_ms_header_and_converts(self):
        """Values > 300 are treated as milliseconds and divided by 1000."""
        exc = self._make_exc({"retry-after-ms": "60000"})
        result = _parse_retry_after(exc)
        assert result == 60.0

    def test_small_value_in_ms_header_not_divided(self):
        """Values <= 300 in retry-after-ms are already seconds."""
        exc = self._make_exc({"retry-after-ms": "30"})
        result = _parse_retry_after(exc)
        assert result == 30.0

    def test_prefers_retry_after_over_retry_after_ms(self):
        exc = self._make_exc({"retry-after": "10", "retry-after-ms": "60000"})
        result = _parse_retry_after(exc)
        assert result == 10.0

    def test_falls_back_to_message_body_when_no_header(self):
        exc = self._make_exc({}, message="Please retry after 25 seconds")
        result = _parse_retry_after(exc)
        assert result == 25.0

    def test_message_body_float_value(self):
        exc = self._make_exc({}, message="retry_after=12.5")
        result = _parse_retry_after(exc)
        assert result == 12.5

    def test_returns_none_when_no_header_and_no_message_match(self):
        exc = self._make_exc({}, message="Something went wrong, no timing info")
        result = _parse_retry_after(exc)
        assert result is None

    def test_handles_broken_headers_gracefully(self):
        """If headers access raises, falls through to message parsing."""
        exc = self._make_exc({})
        exc.response.headers = None   # simulate broken header object
        # Should not crash — just returns None or falls back
        result = _parse_retry_after(exc)
        assert result is None or isinstance(result, float)

    @pytest.mark.parametrize("phrase,expected", [
        ("retry after 5 seconds",   5.0),
        ("retry_after 120",         120.0),
        ("Retry-After: 60",         60.0),
        ("retry after 0.5",         0.5),
    ])
    def test_various_message_patterns(self, phrase, expected):
        exc = self._make_exc({}, message=phrase)
        result = _parse_retry_after(exc)
        assert result == expected


# ─────────────────────────────────────────────────────────────────────────────
# 7. Integration smoke test (full call chain, no real API)
# ─────────────────────────────────────────────────────────────────────────────

class TestGroqProviderIntegration:
    def test_full_audit_call_returns_json_string(self):
        """Simulate exactly what analyzer.py does — pass a full prompt, get JSON back."""
        fake_output = '{"overall_score": 8, "issues": [], "compliant": true}'

        with patch("ai.providers.groq_provider.Groq") as MockGroq:
            MockGroq.return_value.chat.completions.create.return_value = (
                _make_response(fake_output)
            )
            p = GroqProvider(api_key="fake-key")
            result = p.generate(
                system_prompt="You are a texting audit AI. Return JSON only.",
                user_content="[Agent]: Hi!\n[Contact]: Hello!",
                max_tokens=800,
                temperature=0.1,
            )

        assert result == fake_output

    def test_three_consecutive_rate_limits_all_raise(self):
        """Pool manager will get three ProviderRateLimitErrors in a row."""
        from groq import RateLimitError

        with patch("ai.providers.groq_provider.Groq") as MockGroq:
            MockGroq.return_value.chat.completions.create.side_effect = (
                _make_rate_limit_error(retry_after_header="10")
            )
            p = GroqProvider(api_key="fake-key")

            for _ in range(3):
                with pytest.raises(ProviderRateLimitError) as exc_info:
                    p.generate("sys", "usr")
                assert exc_info.value.retry_after == 10.0

    def test_provider_does_not_retry_internally(self):
        """GroqProvider must NOT retry on failure — the pool manager owns that logic."""
        from groq import RateLimitError

        with patch("ai.providers.groq_provider.Groq") as MockGroq:
            MockGroq.return_value.chat.completions.create.side_effect = (
                _make_rate_limit_error()
            )
            p = GroqProvider(api_key="fake-key")

            with pytest.raises(ProviderRateLimitError):
                p.generate("sys", "usr")

            # Must have been called exactly once — no internal retry loop
            assert MockGroq.return_value.chat.completions.create.call_count == 1
