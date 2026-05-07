"""
NVIDIA NIM provider — OpenAI-compatible API hosted by NVIDIA.
Uses the openai SDK pointed at NVIDIA's base URL.
"""
from __future__ import annotations

import re

from openai import OpenAI, RateLimitError

from ai.providers.base import AIProvider, ProviderRateLimitError


class NimProvider(AIProvider):
    """Single-key NVIDIA NIM adapter (OpenAI-compatible)."""

    BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(self, api_key: str, model: str = "meta/llama-3.3-70b-instruct"):
        self._api_key = api_key
        self._model   = model
        self._client  = OpenAI(
            base_url=self.BASE_URL,
            api_key=api_key,
            max_retries=0,
        )

    @property
    def provider_name(self) -> str:
        return "nim"

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
                    {"role": "user",   "content": user_content + "\n\nIMPORTANT: Your entire response must be valid JSON only. No markdown, no explanation, no text outside the JSON object."},
                ],
            )
            return _strip_markdown(response.choices[0].message.content)

        except RateLimitError as e:
            retry_after = _parse_retry_after(e)
            raise ProviderRateLimitError(
                f"NIM rate-limited: {e}", retry_after=retry_after
            ) from e


def _strip_markdown(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers if model adds them."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]  # drop opening fence line
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    return text.strip()


def _parse_retry_after(exc: RateLimitError) -> float | None:
    try:
        ra = (
            exc.response.headers.get("retry-after")
            or exc.response.headers.get("retry-after-ms")
        )
        if ra:
            val = float(ra)
            return val / 1000 if val > 300 else val
    except Exception:
        pass
    match = re.search(r"retry.after[^\d]*(\d+(?:\.\d+)?)", str(exc), re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None
