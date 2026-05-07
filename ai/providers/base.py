"""
Abstract base for AI providers.

Every provider (Groq, NIM, …) implements this interface so the
KeyPoolManager can treat all keys uniformly regardless of backend.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderRateLimitError(Exception):
    """Raised by a provider when the API returns a rate-limit (HTTP 429 or equivalent)."""

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderQuotaExhaustedError(Exception):
    """Raised when a key's daily/monthly quota is fully exhausted (limit: 0).
    Unlike a rate-limit, retrying will not help — skip this agent entirely."""


class ProviderPayloadTooLargeError(Exception):
    """Raised when a provider rejects the request because the payload is too large."""


class AIProvider(ABC):
    """Thin wrapper around a single API key for one LLM provider."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short label — 'groq' or 'nim'."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model identifier used for this provider."""

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_content: str,
        *,
        max_tokens: int = 1200,
        temperature: float = 0.1,
    ) -> str:
        """
        Send a chat completion and return the raw JSON string.

        Must raise ProviderRateLimitError on rate-limit responses so the
        pool manager can rotate to another key.
        """
