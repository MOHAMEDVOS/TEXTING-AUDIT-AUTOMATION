"""Tests for KeyPoolManager — shared Groq pool + dedicated NIM keys."""
import json
import time

import pytest

from ai.analyzer import PooledKey, KeyPoolManager
from tests.conftest import FakeProvider


# ── Task 2: PooledKey new fields ──────────────────────────────────────────────

def test_pooled_key_defaults_new_fields():
    p = FakeProvider("k1")
    pk = PooledKey(key="k1", provider=p, provider_type="groq")
    assert pk.last_used_at == 0.0
    assert pk.quota_exhausted is False


def test_is_ready_false_when_quota_exhausted():
    p = FakeProvider("k1")
    pk = PooledKey(key="k1", provider=p, provider_type="groq", quota_exhausted=True)
    assert pk.is_ready is False


def test_is_ready_true_when_not_cooling_and_not_exhausted():
    p = FakeProvider("k1")
    pk = PooledKey(key="k1", provider=p, provider_type="groq")
    assert pk.is_ready is True


def test_is_ready_false_when_cooling():
    p = FakeProvider("k1")
    pk = PooledKey(
        key="k1",
        provider=p,
        provider_type="groq",
        cool_until=time.monotonic() + 100,
    )
    assert pk.is_ready is False


# ── Task 3: Groq pool loading ─────────────────────────────────────────────────

def test_load_groq_pool_from_config_file(tmp_config_dir, monkeypatch):
    (tmp_config_dir / "groq_keys.json").write_text(
        json.dumps(["gsk_aaaaaa", "gsk_bbbbbb", "gsk_cccccc"])
    )
    (tmp_config_dir / "agent_keys.json").write_text("{}")

    def fake_groq_ctor(api_key, model):
        return FakeProvider(api_key, model)

    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(groq_mod, "GroqProvider", fake_groq_ctor)

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    assert len(mgr._groq_pool) == 3
    assert [pk.key for pk in mgr._groq_pool] == ["gsk_aaaaaa", "gsk_bbbbbb", "gsk_cccccc"]
    assert all(pk.provider_type == "groq" for pk in mgr._groq_pool)


def test_load_groq_pool_missing_file_raises(tmp_config_dir):
    (tmp_config_dir / "groq_keys.json").unlink()
    mgr = KeyPoolManager()
    with pytest.raises(ValueError, match="groq_keys.json"):
        mgr.ensure_loaded()


def test_load_groq_pool_empty_list_ok(tmp_config_dir):
    (tmp_config_dir / "groq_keys.json").write_text("[]")
    (tmp_config_dir / "agent_keys.json").write_text("{}")
    mgr = KeyPoolManager()
    mgr.ensure_loaded()
    assert mgr._groq_pool == []


# ── Task 4: LRU pool selection ────────────────────────────────────────────────

def test_pick_groq_key_returns_lru(tmp_config_dir, monkeypatch, frozen_time):
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )

    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["k1", "k2", "k3"]))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    # k1 picked first (all last_used_at=0, tie broken by list order)
    chosen = mgr._pick_groq_key()
    assert chosen.key == "k1"

    # Advance time; pick should now return k2 (k1 just used)
    frozen_time.advance(1.0)
    chosen = mgr._pick_groq_key()
    assert chosen.key == "k2"

    frozen_time.advance(1.0)
    chosen = mgr._pick_groq_key()
    assert chosen.key == "k3"

    # Now k1 is LRU again
    frozen_time.advance(1.0)
    chosen = mgr._pick_groq_key()
    assert chosen.key == "k1"


def test_pick_groq_key_skips_cooling(tmp_config_dir, monkeypatch, frozen_time):
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["k1", "k2"]))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    # Mark k1 as cooling
    mgr._groq_pool[0].cool_until = frozen_time.now() + 100

    chosen = mgr._pick_groq_key()
    assert chosen.key == "k2"


def test_pick_groq_key_all_cooling_returns_soonest(tmp_config_dir, monkeypatch, frozen_time):
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["k1", "k2"]))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    mgr._groq_pool[0].cool_until = frozen_time.now() + 200
    mgr._groq_pool[1].cool_until = frozen_time.now() + 50

    chosen = mgr._pick_groq_key()
    assert chosen.key == "k2"  # recovers sooner
    assert chosen.wait_seconds > 0


def test_pick_groq_key_skips_exhausted(tmp_config_dir, monkeypatch, frozen_time):
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["k1", "k2"]))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    mgr._groq_pool[0].quota_exhausted = True

    chosen = mgr._pick_groq_key()
    assert chosen.key == "k2"


def test_pick_groq_key_all_exhausted_raises(tmp_config_dir, monkeypatch):
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["k1", "k2"]))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    for pk in mgr._groq_pool:
        pk.quota_exhausted = True

    with pytest.raises(RuntimeError, match="All Groq keys exhausted"):
        mgr._pick_groq_key()


def test_pick_nim_key_returns_dedicated(tmp_config_dir, monkeypatch):
    # Patch NimProvider at the source BEFORE the module is loaded,
    # so that the openai import inside nim_provider.py is never triggered.
    import sys
    from unittest.mock import MagicMock

    # Create a fake nim_provider module
    fake_nim_mod = MagicMock()
    fake_nim_mod.NimProvider = lambda api_key: FakeProvider(api_key, "nim-model")
    monkeypatch.setitem(sys.modules, "ai.providers.nim_provider", fake_nim_mod)

    (tmp_config_dir / "groq_keys.json").write_text("[]")
    (tmp_config_dir / "agent_keys.json").write_text(json.dumps({
        "resva1054": {"provider": "nim", "key": "nvapi-xxx"},
    }))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    chosen = mgr._pick_nim_key("Resva1054")
    assert chosen is not None
    assert chosen.key == "nvapi-xxx"

    assert mgr._pick_nim_key("unknown") is None


# ── Task 5: quota exhaustion + status ─────────────────────────────────────────

def test_mark_quota_exhausted_sets_flag(tmp_config_dir, monkeypatch):
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["k1"]))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    pk = mgr._groq_pool[0]
    mgr.mark_quota_exhausted(pk)

    assert pk.quota_exhausted is True
    assert pk.is_ready is False


def test_get_status_reports_groq_and_nim(tmp_config_dir, monkeypatch, frozen_time):
    import sys
    from unittest.mock import MagicMock
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    # Patch nim_provider module to avoid openai import
    fake_nim_mod = MagicMock()
    fake_nim_mod.NimProvider = lambda api_key: FakeProvider(api_key, "nim-model")
    monkeypatch.setitem(sys.modules, "ai.providers.nim_provider", fake_nim_mod)

    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1", "g2", "g3"]))
    (tmp_config_dir / "agent_keys.json").write_text(json.dumps({
        "a1": {"provider": "nim", "key": "n1"},
        "a2": {"provider": "nim", "key": "n2"},
    }))

    mgr = KeyPoolManager()
    mgr.ensure_loaded()

    # One cooling Groq, one exhausted Groq, one ready Groq
    mgr._groq_pool[0].cool_until = frozen_time.now() + 100
    mgr._groq_pool[1].quota_exhausted = True

    status = mgr.get_status()
    assert status["total_keys"] == 5
    assert status["available_keys"] == 3  # 1 ready groq + 2 ready nim
    assert status["cooling_keys"] == 1
    assert status["exhausted_keys"] == 1
    assert status["providers"]["groq"]["total"] == 3
    assert status["providers"]["groq"]["available"] == 1
    assert status["providers"]["groq"]["cooling"] == 1
    assert status["providers"]["groq"]["exhausted"] == 1
    assert status["providers"]["nim"]["total"] == 2
    assert status["providers"]["nim"]["available"] == 2


# ── Tasks 6-7: analyze_conversation + analyze_batch routing ──────────────────

def test_analyze_conversation_uses_groq_pool_for_unknown_agent(tmp_config_dir, monkeypatch):
    import ai.providers.groq_provider as groq_mod
    import ai.analyzer as analyzer_mod

    def tracking_ctor(api_key, model):
        p = FakeProvider(api_key, model)
        p.response = '{"compliance_score": 90, "recommendations": []}'
        return p

    monkeypatch.setattr(groq_mod, "GroqProvider", tracking_ctor)
    monkeypatch.setattr("ai.analyzer.time.sleep", lambda *_: None)
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1"]))
    (tmp_config_dir / "agent_keys.json").write_text("{}")

    analyzer_mod._pool = analyzer_mod.KeyPoolManager()

    result = analyzer_mod.analyze_conversation(
        messages=[{"sender": "agent", "message": "hi"}],
        agent_name="Resva1028",
        contact_name="JohnDoe",
    )
    assert result.get("compliance_score") == 90


def test_analyze_conversation_rotates_on_rate_limit(tmp_config_dir, monkeypatch):
    from ai.providers.base import ProviderRateLimitError
    import ai.providers.groq_provider as groq_mod
    import ai.analyzer as analyzer_mod

    providers_made: list[FakeProvider] = []

    def tracking_ctor(api_key, model):
        p = FakeProvider(api_key, model)
        providers_made.append(p)
        return p

    monkeypatch.setattr(groq_mod, "GroqProvider", tracking_ctor)
    monkeypatch.setattr("ai.analyzer.time.sleep", lambda *_: None)
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1", "g2"]))
    (tmp_config_dir / "agent_keys.json").write_text("{}")

    analyzer_mod._pool = analyzer_mod.KeyPoolManager()
    analyzer_mod._pool.ensure_loaded()
    # First provider rate-limits; second succeeds
    providers_made[0].response = ProviderRateLimitError("429", retry_after=1)
    providers_made[1].response = '{"compliance_score": 88}'

    result = analyzer_mod.analyze_conversation(
        messages=[{"sender": "agent", "message": "hi"}],
        agent_name="Resva1028",
        contact_name="Jane",
    )
    assert result.get("compliance_score") == 88
    assert len(providers_made[0].calls) >= 1
    assert len(providers_made[1].calls) >= 1


def test_analyze_conversation_uses_nim_for_nim_agent(tmp_config_dir, monkeypatch):
    import sys
    from unittest.mock import MagicMock
    import ai.providers.groq_provider as groq_mod
    import ai.analyzer as analyzer_mod

    groq_made: list[FakeProvider] = []
    nim_made: list[FakeProvider] = []

    def groq_ctor(api_key, model):
        p = FakeProvider(api_key, model)
        groq_made.append(p)
        return p

    def nim_ctor(api_key):
        p = FakeProvider(api_key, "nim-model")
        p.response = '{"compliance_score": 77}'
        nim_made.append(p)
        return p

    monkeypatch.setattr(groq_mod, "GroqProvider", groq_ctor)
    # Patch nim_provider module to avoid openai import
    fake_nim_mod = MagicMock()
    fake_nim_mod.NimProvider = nim_ctor
    monkeypatch.setitem(sys.modules, "ai.providers.nim_provider", fake_nim_mod)
    monkeypatch.setattr("ai.analyzer.time.sleep", lambda *_: None)

    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1"]))
    (tmp_config_dir / "agent_keys.json").write_text(json.dumps({
        "resva1054": {"provider": "nim", "key": "nvapi-xxx"}
    }))

    analyzer_mod._pool = analyzer_mod.KeyPoolManager()

    result = analyzer_mod.analyze_conversation(
        messages=[{"sender": "agent", "message": "hi"}],
        agent_name="Resva1054",
        contact_name="Bob",
    )
    assert result.get("compliance_score") == 77
    # Groq pool key should NOT have been called
    assert all(len(p.calls) == 0 for p in groq_made)
    assert len(nim_made[0].calls) == 1


def test_analyze_batch_uses_groq_pool(tmp_config_dir, monkeypatch):
    import ai.providers.groq_provider as groq_mod
    import ai.analyzer as analyzer_mod

    def ctor(api_key, model):
        p = FakeProvider(api_key, model)
        p.response = '{"results": [{"compliance_score": 80}, {"compliance_score": 70}]}'
        return p

    monkeypatch.setattr(groq_mod, "GroqProvider", ctor)
    monkeypatch.setattr("ai.analyzer.time.sleep", lambda *_: None)

    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1"]))
    (tmp_config_dir / "agent_keys.json").write_text("{}")

    analyzer_mod._pool = analyzer_mod.KeyPoolManager()

    batch = [
        {"parsed_messages": [{"sender": "agent", "message": "hi"}], "contact_name": "A"},
        {"parsed_messages": [{"sender": "agent", "message": "hi"}], "contact_name": "B"},
    ]
    results = analyzer_mod.analyze_batch(batch, "Resva1028")
    assert len(results) == 2
    assert results[0]["compliance_score"] == 80
    assert results[1]["compliance_score"] == 70
    assert results[0]["contact_name"] == "A"
    assert results[1]["contact_name"] == "B"
