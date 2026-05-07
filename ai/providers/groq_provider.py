"""
Groq provider — wraps the Groq Python SDK behind the AIProvider interface.
"""
from __future__ import annotations

import re

from groq import Groq, RateLimitError, APIError

from ai.providers.base import (
    AIProvider,
    ProviderPayloadTooLargeError,
    ProviderRateLimitError,
)


class GroqProvider(AIProvider):
    """Single-key Groq adapter."""

    def __init__(self, api_key: str, model: str = "llama-3.3-70b-versatile"):
        self._api_key = api_key
        self._model = model
        # max_retries=0 → we handle retries ourselves via the pool manager
        self._client = Groq(api_key=api_key, max_retries=0)

    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self._model

    def generate(
        self,
        system_prompt: str,
        user_content: str,
        *,
        max_tokens: int = 1200,
        temperature: float = 0.1,
    ) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if content and ("limit of allowed tokens" in content or "Rate limit reached" in content):
                raise ProviderRateLimitError(
                    f"Groq rate-limited via inline message: {content[:100]}", 
                    retry_after=60.0
                )
            return content

        except RateLimitError as e:
            retry_after = _parse_retry_after(e)
            raise ProviderRateLimitError(
                f"Groq rate-limited: {e}", retry_after=retry_after
            ) from e
        except APIError as e:
            if _is_payload_too_large(e):
                raise ProviderPayloadTooLargeError(
                    f"Groq payload too large: {e}"
                ) from e
            # We treat generic API errors (like 503 Overloaded) as rate limits to trigger rotation
            raise ProviderRateLimitError(
                f"Groq API Error (rotated): {e}", retry_after=20.0
            ) from e


def _parse_retry_after(exc: RateLimitError) -> float | None:
    """Extract wait-seconds from Groq's error response."""
    # 1. Headers
    try:
        ra = (
            exc.response.headers.get("retry-after")
            or exc.response.headers.get("retry-after-ms")
        )
        if ra:
            val = float(ra)
            return val / 1000 if val > 300 else val  # ms→s if >5 min
    except Exception:
        pass

    # 2. Error message body
    match = re.search(r"retry.after[^\d]*(\d+(?:\.\d+)?)", str(exc), re.IGNORECASE)
    if match:
        return float(match.group(1))

    return None


def _is_payload_too_large(exc: APIError) -> bool:
    """Return True when Groq rejected the request with HTTP 413 / payload-too-large semantics."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status_code", None)

    try:
        if status_code is not None and int(status_code) == 413:
            return True
    except (TypeError, ValueError):
        pass

    message = str(exc).lower()
    return "payload too large" in message or "request too large" in message
