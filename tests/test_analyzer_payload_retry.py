from __future__ import annotations

import sys
from types import SimpleNamespace

from ai.providers.base import ProviderPayloadTooLargeError


class PayloadRetryProvider:
    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    @property
    def model_name(self) -> str:
        return "fake-groq"

    def generate(self, system_prompt, user_content, *, max_tokens=1200, temperature=0.1) -> str:
        self.calls.append((len(system_prompt.encode("utf-8")), len(user_content.encode("utf-8"))))
        if len(self.calls) == 1:
            raise ProviderPayloadTooLargeError("payload too large")
        return '{"compliance_score": 91}'


def test_analyze_conversation_retries_with_compact_payload(monkeypatch):
    import ai.analyzer as analyzer_mod

    fake_prefilter = SimpleNamespace(run_prefilter=lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "ai.prefilter", fake_prefilter)

    provider = PayloadRetryProvider()
    pooled_key = analyzer_mod.PooledKey(
        key="g1",
        provider=provider,
        provider_type="groq",
    )

    class FakePool:
        def __init__(self):
            self._groq_by_key = {"g1": pooled_key}

        def ensure_loaded(self):
            return None

        def _pick_nim_key(self, agent_name: str):
            return None

        def mark_success(self, pk):
            return None

        def mark_rate_limited(self, pk, retry_after=None):
            return None

        def mark_quota_exhausted(self, pk):
            return None

    prompt_sizes: list[int] = []

    def fake_get_system_prompt(*, batch=False, funnel_tier=None, guidelines=None, include_learned_rules=True):
        size = 22_000 if include_learned_rules else 10_000
        prompt_sizes.append(size)
        return "P" * size

    monkeypatch.setattr(analyzer_mod, "_pool", FakePool())
    monkeypatch.setattr(analyzer_mod, "_db_reserve_groq_key", lambda lease_seconds=15: (1, "g1"))
    monkeypatch.setattr(analyzer_mod, "_db_release_groq_key", lambda key_id: None)
    monkeypatch.setattr(analyzer_mod, "_db_cooldown_groq_key", lambda key_id, seconds: None)
    monkeypatch.setattr(analyzer_mod, "get_system_prompt", fake_get_system_prompt)
    monkeypatch.setattr("ai.analyzer.time.sleep", lambda *_: None)

    messages = [
        {"sender": "agent", "message": f"message {idx} " + ("x" * 120)}
        for idx in range(120)
    ]

    result = analyzer_mod.analyze_conversation(
        messages=messages,
        agent_name="Kaci",
        contact_name="Dora Cooper",
        assigned_labels=["Not interested"],
        funnel_tier="MF",
        guidelines="Ask about motivation and closing timeline.",
    )

    assert result["compliance_score"] == 91
    assert len(provider.calls) == 2
    assert provider.calls[1][0] < provider.calls[0][0]
    assert sum(provider.calls[1]) < sum(provider.calls[0])
    assert prompt_sizes == [22_000, 10_000]
