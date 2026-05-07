 # Shared Groq Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `ai/analyzer.py` so all non-NIM agents share a pool of 14 Groq API keys loaded from `config/groq_keys.json`, with LRU key selection and guaranteed-no-skip semantics on rate limits. NIM agents keep dedicated keys in `config/agent_keys.json`.

**Architecture:** `KeyPoolManager` maintains two stores: a list-based Groq shared pool (`_groq_pool`) and a dict of dedicated NIM keys (`_nim_keys`). Agent routing: if the agent name is in `_nim_keys`, use that dedicated key; otherwise pull from the shared Groq pool using LRU selection. On rate-limit, mark the key cooling and rotate to the next Groq key — retry up to 10 full pool cycles before raising. On daily-quota exhaustion, permanently remove the key from rotation.

**Tech Stack:** Python 3.11+, `threading.Lock`, `pytest`, existing `AIProvider` abstraction (Groq + NIM providers unchanged).

**Spec:** `docs/superpowers/specs/2026-04-14-shared-groq-pool-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ai/analyzer.py` | Modify | Refactor `KeyPoolManager`, new LRU pool selection, new `analyze_conversation()`/`analyze_batch()` retry loops |
| `tests/test_key_pool_manager.py` | Create | Unit tests for `KeyPoolManager` with mocked providers |
| `tests/__init__.py` | Create if missing | Make `tests/` a package |
| `tests/conftest.py` | Create | Shared pytest fixtures (mock provider, temp config files) |
| `config/agent_keys.json` | Modify | Trim to 5 NIM-only entries |
| `config/gemini_keys.json` | Delete | Leftover from previous Gemini removal |
| `ai/providers/__init__.py` | Modify | Update docstring |
| `dashboard/app.py` | Modify | Update `/api/ai/status` docstring example to show `exhausted` counter |
| `CLAUDE.md` | Modify | Add section documenting the shared-pool model |

---

## Task 1: Scaffold test infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_key_pool_manager.py` (empty scaffold in this task)

- [ ] **Step 1: Create tests package marker**

Create `tests/__init__.py` with empty content.

```python
```

- [ ] **Step 2: Create shared fixtures**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures for KeyPoolManager tests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ai.providers.base import AIProvider


class FakeProvider(AIProvider):
    """Test double for AIProvider — records calls, returns configurable output."""

    def __init__(self, api_key: str, model: str = "fake-model"):
        self._api_key = api_key
        self._model = model
        self.calls: list[tuple] = []
        self.response: str | Exception = '{"ok": true}'

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return self._model

    def generate(self, system_prompt, user_content, *, max_tokens=1200, temperature=0.1) -> str:
        self.calls.append((system_prompt, user_content, max_tokens, temperature))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def fake_provider_factory():
    """Factory that returns a new FakeProvider per call."""
    created: list[FakeProvider] = []

    def _make(api_key: str = "fake-key", model: str = "fake-model") -> FakeProvider:
        p = FakeProvider(api_key, model)
        created.append(p)
        return p

    _make.created = created
    return _make


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch) -> Path:
    """Create tmp config dir with groq_keys.json and agent_keys.json; patch PROJECT_ROOT."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "groq_keys.json").write_text("[]")
    (config_dir / "agent_keys.json").write_text("{}")

    # Patch the module-level path constants in ai.analyzer
    import ai.analyzer as analyzer_mod
    monkeypatch.setattr(analyzer_mod, "_AGENT_KEYS_FILE", config_dir / "agent_keys.json")
    monkeypatch.setattr(analyzer_mod, "_GROQ_KEYS_FILE", config_dir / "groq_keys.json")

    return config_dir


@pytest.fixture
def frozen_time(monkeypatch):
    """Patch time.monotonic() to return a controllable value."""
    current = [1000.0]

    def fake_monotonic():
        return current[0]

    def advance(seconds: float):
        current[0] += seconds

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    return type("Clock", (), {"advance": staticmethod(advance), "now": lambda: current[0]})()
```

- [ ] **Step 3: Create empty test module**

Create `tests/test_key_pool_manager.py`:

```python
"""Tests for KeyPoolManager — shared Groq pool + dedicated NIM keys."""
```

- [ ] **Step 4: Verify pytest discovers the package**

Run: `python -m pytest tests/ --collect-only`
Expected: Output shows `tests/test_key_pool_manager.py` discovered, 0 tests collected.

- [ ] **Step 5: Commit**

```bash
git add tests/__init__.py tests/conftest.py tests/test_key_pool_manager.py
git commit -m "test: scaffold key-pool test infrastructure"
```

---

## Task 2: Update `PooledKey` dataclass with `last_used_at` and `quota_exhausted`

**Files:**
- Modify: `ai/analyzer.py` (PooledKey dataclass, lines 35-51)
- Modify: `tests/test_key_pool_manager.py`

- [ ] **Step 1: Write failing test for `PooledKey` new fields**

Append to `tests/test_key_pool_manager.py`:

```python
import time

from ai.analyzer import PooledKey
from tests.conftest import FakeProvider


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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: `test_pooled_key_defaults_new_fields` FAILS with `AttributeError: 'PooledKey' object has no attribute 'last_used_at'`.

- [ ] **Step 3: Update `PooledKey` in `ai/analyzer.py`**

Replace the `PooledKey` dataclass (lines 35-51) with:

```python
@dataclass
class PooledKey:
    """One API key + its provider instance + health bookkeeping."""
    key: str
    provider: AIProvider
    provider_type: str          # "groq" or "nim"
    cool_until: float = 0.0     # monotonic timestamp when usable again
    last_used_at: float = 0.0   # monotonic timestamp of most recent use (for LRU)
    quota_exhausted: bool = False  # permanently removed from rotation (daily quota hit)
    success_count: int = 0
    fail_count: int = 0

    @property
    def is_ready(self) -> bool:
        return (not self.quota_exhausted) and time.monotonic() >= self.cool_until

    @property
    def wait_seconds(self) -> float:
        return max(0.0, self.cool_until - time.monotonic())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: All 4 `PooledKey` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ai/analyzer.py tests/test_key_pool_manager.py
git commit -m "feat(analyzer): add last_used_at and quota_exhausted to PooledKey"
```

---

## Task 3: Add `_GROQ_KEYS_FILE` path constant and Groq pool loader

**Files:**
- Modify: `ai/analyzer.py` (add constant near line 28, add loader method to `KeyPoolManager`)
- Modify: `tests/test_key_pool_manager.py`

- [ ] **Step 1: Write failing test for Groq pool loading**

Append to `tests/test_key_pool_manager.py`:

```python
import json

from ai.analyzer import KeyPoolManager


def test_load_groq_pool_from_config_file(tmp_config_dir, monkeypatch):
    (tmp_config_dir / "groq_keys.json").write_text(
        json.dumps(["gsk_aaaaaa", "gsk_bbbbbb", "gsk_cccccc"])
    )
    (tmp_config_dir / "agent_keys.json").write_text("{}")

    # Patch provider import so we don't make real API clients
    from tests.conftest import FakeProvider

    def fake_groq_ctor(api_key, model):
        return FakeProvider(api_key, model)

    import ai.analyzer as analyzer_mod
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
    import pytest
    with pytest.raises(ValueError, match="groq_keys.json"):
        mgr.ensure_loaded()


def test_load_groq_pool_empty_list_ok(tmp_config_dir):
    (tmp_config_dir / "groq_keys.json").write_text("[]")
    (tmp_config_dir / "agent_keys.json").write_text("{}")
    mgr = KeyPoolManager()
    mgr.ensure_loaded()
    assert mgr._groq_pool == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_key_pool_manager.py::test_load_groq_pool_from_config_file -v`
Expected: FAILS — `_groq_pool` attribute does not exist.

- [ ] **Step 3: Add `_GROQ_KEYS_FILE` constant**

In `ai/analyzer.py`, find the line:
```python
_AGENT_KEYS_FILE  = PROJECT_ROOT / "config" / "agent_keys.json"
```

Replace it with:
```python
_AGENT_KEYS_FILE = PROJECT_ROOT / "config" / "agent_keys.json"
_GROQ_KEYS_FILE  = PROJECT_ROOT / "config" / "groq_keys.json"
```

- [ ] **Step 4: Refactor `KeyPoolManager.__init__` and add Groq pool loader**

In `ai/analyzer.py`, replace the `KeyPoolManager.__init__` (lines 64-68) with:

```python
    def __init__(self):
        self._groq_pool: list[PooledKey] = []           # shared pool, LRU-selected
        self._nim_keys: dict[str, PooledKey] = {}       # agent_name.lower() → dedicated NIM key
        self._lock = threading.Lock()
        self._loaded = False
```

- [ ] **Step 5: Replace `_load()` method**

Replace the entire `_load()` method (lines 72-126) with:

```python
    def _load(self) -> None:
        if self._loaded:
            return

        self._load_groq_pool()
        self._load_nim_keys()

        logger.info(
            f"[Pool] Loaded {len(self._groq_pool)} Groq keys "
            f"(shared pool) + {len(self._nim_keys)} NIM dedicated keys"
        )
        self._loaded = True

    def _load_groq_pool(self) -> None:
        if not _GROQ_KEYS_FILE.exists():
            raise ValueError(
                "config/groq_keys.json not found. "
                "Add the list of Groq API keys to that file."
            )

        try:
            key_list = json.loads(_GROQ_KEYS_FILE.read_text())
        except Exception as e:
            raise ValueError(f"Could not read groq_keys.json: {e}") from e

        if not isinstance(key_list, list):
            raise ValueError("groq_keys.json must contain a JSON array of key strings")

        from ai.providers.groq_provider import GroqProvider

        for api_key in key_list:
            if not api_key or not isinstance(api_key, str):
                logger.warning(f"[Pool] Skipping invalid Groq key entry: {api_key!r}")
                continue
            try:
                prov = GroqProvider(api_key=api_key, model=GROQ_MODEL)
                pk = PooledKey(key=api_key, provider=prov, provider_type="groq")
                self._groq_pool.append(pk)
                logger.debug(f"[Pool] Groq pool +1 […{api_key[-6:]}]")
            except Exception as e:
                logger.warning(f"[Pool] Failed to init Groq key […{api_key[-6:]}]: {e}")

    def _load_nim_keys(self) -> None:
        if not _AGENT_KEYS_FILE.exists():
            logger.info("[Pool] agent_keys.json not found — no NIM keys loaded")
            return

        try:
            agent_map = json.loads(_AGENT_KEYS_FILE.read_text())
        except Exception as e:
            raise ValueError(f"Could not read agent_keys.json: {e}") from e

        for agent_name, entry in agent_map.items():
            if not entry:
                continue
            provider_type = entry.get("provider", "").lower()
            api_key = entry.get("key", "")

            if provider_type != "nim":
                logger.warning(
                    f"[Pool] Ignoring non-NIM entry for '{agent_name}' in agent_keys.json "
                    f"(provider={provider_type!r}) — Groq agents use the shared pool"
                )
                continue
            if not api_key:
                logger.warning(f"[Pool] Agent '{agent_name}' — empty NIM key, skipping")
                continue

            try:
                from ai.providers.nim_provider import NimProvider
                prov = NimProvider(api_key=api_key)
                pk = PooledKey(key=api_key, provider=prov, provider_type="nim")
                self._nim_keys[agent_name.lower()] = pk
                logger.debug(f"[Pool] NIM key for '{agent_name}' […{api_key[-6:]}]")
            except Exception as e:
                logger.warning(f"[Pool] Failed to init NIM key for '{agent_name}': {e}")
```

- [ ] **Step 6: Run tests to verify pass**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: All tests in this file PASS so far (7 tests).

- [ ] **Step 7: Commit**

```bash
git add ai/analyzer.py tests/test_key_pool_manager.py
git commit -m "feat(analyzer): load Groq shared pool from config/groq_keys.json"
```

---

## Task 4: Implement `_pick_groq_key()` with LRU selection

**Files:**
- Modify: `ai/analyzer.py` (replace `assign_key_for_agent` and `pick_best_key`)
- Modify: `tests/test_key_pool_manager.py`

- [ ] **Step 1: Write failing tests for LRU pool selection**

Append to `tests/test_key_pool_manager.py`:

```python
def test_pick_groq_key_returns_lru(tmp_config_dir, monkeypatch, frozen_time):
    from tests.conftest import FakeProvider
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
    from tests.conftest import FakeProvider
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
    from tests.conftest import FakeProvider
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
    from tests.conftest import FakeProvider
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
    from tests.conftest import FakeProvider
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

    import pytest
    with pytest.raises(RuntimeError, match="All Groq keys exhausted"):
        mgr._pick_groq_key()


def test_pick_nim_key_returns_dedicated(tmp_config_dir, monkeypatch):
    from tests.conftest import FakeProvider
    import ai.providers.nim_provider as nim_mod
    monkeypatch.setattr(
        nim_mod, "NimProvider",
        lambda api_key: FakeProvider(api_key, "nim-model"),
    )

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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: New tests FAIL — `_pick_groq_key` / `_pick_nim_key` methods do not exist.

- [ ] **Step 3: Replace `assign_key_for_agent` and `pick_best_key`**

In `ai/analyzer.py`, remove the existing `assign_key_for_agent` and `pick_best_key` methods (lines 134-151 in the original file) and replace with:

```python
    # ── Key selection ─────────────────────────────────────────────────────

    def _pick_nim_key(self, agent_name: str) -> PooledKey | None:
        """Return the dedicated NIM key for this agent, or None if no NIM entry."""
        with self._lock:
            self._load()
            return self._nim_keys.get(agent_name.lower())

    def _pick_groq_key(self) -> PooledKey:
        """
        Pick the best available Groq key from the shared pool.

        Selection rules:
          1. Skip permanently exhausted keys.
          2. If any remaining keys are ready (not cooling) → pick LRU.
          3. If all remaining keys are cooling → return the one recovering soonest.
          4. If no usable keys remain → raise RuntimeError.
        """
        with self._lock:
            self._load()
            usable = [k for k in self._groq_pool if not k.quota_exhausted]
            if not usable:
                raise RuntimeError("All Groq keys exhausted — cannot score conversation")

            ready = [k for k in usable if k.is_ready]
            if ready:
                chosen = min(ready, key=lambda k: k.last_used_at)
                chosen.last_used_at = time.monotonic()
                return chosen
            return min(usable, key=lambda k: k.cool_until)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: All tests pass so far.

- [ ] **Step 5: Commit**

```bash
git add ai/analyzer.py tests/test_key_pool_manager.py
git commit -m "feat(analyzer): add LRU Groq key selection and NIM dedicated lookup"
```

---

## Task 5: Add `mark_quota_exhausted` and update `get_status`

**Files:**
- Modify: `ai/analyzer.py` (KeyPoolManager)
- Modify: `tests/test_key_pool_manager.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_key_pool_manager.py`:

```python
def test_mark_quota_exhausted_sets_flag(tmp_config_dir, monkeypatch):
    from tests.conftest import FakeProvider
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
    from tests.conftest import FakeProvider
    import ai.providers.groq_provider as groq_mod
    import ai.providers.nim_provider as nim_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    monkeypatch.setattr(
        nim_mod, "NimProvider",
        lambda api_key: FakeProvider(api_key, "nim-model"),
    )

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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_key_pool_manager.py::test_mark_quota_exhausted_sets_flag tests/test_key_pool_manager.py::test_get_status_reports_groq_and_nim -v`
Expected: FAIL — `mark_quota_exhausted` missing, status shape wrong.

- [ ] **Step 3: Add `mark_quota_exhausted` and rewrite `get_status`**

In `ai/analyzer.py`, the `mark_success` method currently ends around line 162. After it, add:

```python
    def mark_quota_exhausted(self, pk: PooledKey) -> None:
        """Permanently remove a key from rotation (daily quota hit)."""
        with self._lock:
            pk.quota_exhausted = True
            pk.fail_count += 1
            logger.warning(
                f"[Pool] Key […{pk.key[-6:]}] ({pk.provider_type}) "
                f"marked quota-exhausted — removed from rotation"
            )
```

Then replace the entire `get_status` method (lines 166-191) with:

```python
    def get_status(self) -> dict:
        with self._lock:
            self._load()

            all_keys = list(self._groq_pool) + list(self._nim_keys.values())

            providers: dict[str, dict] = {}
            for pk in all_keys:
                pt = pk.provider_type
                if pt not in providers:
                    providers[pt] = {
                        "total": 0,
                        "available": 0,
                        "cooling": 0,
                        "exhausted": 0,
                        "model": pk.provider.model_name,
                        "success": 0,
                        "failures": 0,
                    }
                providers[pt]["total"] += 1
                if pk.quota_exhausted:
                    providers[pt]["exhausted"] += 1
                elif pk.is_ready:
                    providers[pt]["available"] += 1
                else:
                    providers[pt]["cooling"] += 1
                providers[pt]["success"] += pk.success_count
                providers[pt]["failures"] += pk.fail_count

            return {
                "total_keys": len(all_keys),
                "available_keys": sum(1 for pk in all_keys if pk.is_ready),
                "cooling_keys": sum(
                    1 for pk in all_keys
                    if not pk.quota_exhausted and not pk.is_ready
                ),
                "exhausted_keys": sum(1 for pk in all_keys if pk.quota_exhausted),
                "providers": providers,
            }
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add ai/analyzer.py tests/test_key_pool_manager.py
git commit -m "feat(analyzer): add quota exhaustion tracking and enriched status"
```

---

## Task 6: Rewrite `analyze_conversation()` to use shared pool

**Files:**
- Modify: `ai/analyzer.py` (analyze_conversation function)
- Modify: `tests/test_key_pool_manager.py`

- [ ] **Step 1: Write failing tests for analyze_conversation pool behavior**

Append to `tests/test_key_pool_manager.py`:

```python
def test_analyze_conversation_uses_groq_pool_for_unknown_agent(tmp_config_dir, monkeypatch):
    from tests.conftest import FakeProvider
    import ai.providers.groq_provider as groq_mod
    monkeypatch.setattr(
        groq_mod, "GroqProvider",
        lambda api_key, model: FakeProvider(api_key, model),
    )
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1"]))
    (tmp_config_dir / "agent_keys.json").write_text("{}")

    # Reset singleton
    import ai.analyzer as analyzer_mod
    analyzer_mod._pool = analyzer_mod.KeyPoolManager()

    # Make FakeProvider return valid JSON
    original_ctor = groq_mod.GroqProvider

    def tracking_ctor(api_key, model):
        p = FakeProvider(api_key, model)
        p.response = '{"compliance_score": 90, "recommendations": []}'
        return p

    monkeypatch.setattr(groq_mod, "GroqProvider", tracking_ctor)
    analyzer_mod._pool = analyzer_mod.KeyPoolManager()

    result = analyzer_mod.analyze_conversation(
        messages=[{"sender": "agent", "text": "hi"}],
        agent_name="Resva1028",  # not in agent_keys.json — should use Groq pool
        contact_name="JohnDoe",
    )
    assert result["error"] is None or "error" not in result or result.get("compliance_score") == 90


def test_analyze_conversation_rotates_on_rate_limit(tmp_config_dir, monkeypatch):
    from tests.conftest import FakeProvider
    from ai.providers.base import ProviderRateLimitError
    import ai.providers.groq_provider as groq_mod
    import ai.analyzer as analyzer_mod

    providers_made: list[FakeProvider] = []

    def tracking_ctor(api_key, model):
        p = FakeProvider(api_key, model)
        providers_made.append(p)
        return p

    monkeypatch.setattr(groq_mod, "GroqProvider", tracking_ctor)
    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1", "g2"]))
    (tmp_config_dir / "agent_keys.json").write_text("{}")

    # Patch time.sleep so tests don't stall
    monkeypatch.setattr("ai.analyzer.time.sleep", lambda *_: None)

    analyzer_mod._pool = analyzer_mod.KeyPoolManager()
    # First provider rate-limits; second succeeds
    analyzer_mod._pool.ensure_loaded()
    providers_made[0].response = ProviderRateLimitError("429", retry_after=1)
    providers_made[1].response = '{"compliance_score": 88}'

    result = analyzer_mod.analyze_conversation(
        messages=[{"sender": "agent", "text": "hi"}],
        agent_name="Resva1028",
        contact_name="Jane",
    )
    assert result.get("compliance_score") == 88
    # Both providers should have been called
    assert len(providers_made[0].calls) >= 1
    assert len(providers_made[1].calls) >= 1


def test_analyze_conversation_uses_nim_for_nim_agent(tmp_config_dir, monkeypatch):
    from tests.conftest import FakeProvider
    import ai.providers.groq_provider as groq_mod
    import ai.providers.nim_provider as nim_mod
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
    monkeypatch.setattr(nim_mod, "NimProvider", nim_ctor)
    monkeypatch.setattr("ai.analyzer.time.sleep", lambda *_: None)

    (tmp_config_dir / "groq_keys.json").write_text(json.dumps(["g1"]))
    (tmp_config_dir / "agent_keys.json").write_text(json.dumps({
        "resva1054": {"provider": "nim", "key": "nvapi-xxx"}
    }))

    analyzer_mod._pool = analyzer_mod.KeyPoolManager()

    result = analyzer_mod.analyze_conversation(
        messages=[{"sender": "agent", "text": "hi"}],
        agent_name="Resva1054",
        contact_name="Bob",
    )
    assert result.get("compliance_score") == 77
    # Groq pool key should NOT have been called
    assert all(len(p.calls) == 0 for p in groq_made)
    assert len(nim_made[0].calls) == 1
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: The 3 new tests FAIL.

- [ ] **Step 3: Add `_MAX_POOL_CYCLES` constant**

In `ai/analyzer.py`, near the top-level `_DEFAULT_COOLDOWN`, add:

```python
_DEFAULT_COOLDOWN = 60  # seconds
_MAX_POOL_CYCLES  = 10  # max full rotations through the Groq pool before giving up
```

- [ ] **Step 4: Rewrite `analyze_conversation`**

Replace the entire `analyze_conversation` function (lines 212-315) with:

```python
def analyze_conversation(
    messages: list[dict],
    agent_name: str,
    contact_name: str = "Contact",
    assigned_labels: list[str] | None = None,
    *,
    model: str | None = None,
) -> dict:
    """
    Analyze a single parsed conversation.

    NIM agents use their dedicated NIM key with cooldown retry.
    All other agents use the shared Groq pool with LRU rotation.
    On rate-limit, rotates to the next Groq key — never skips a conversation.

    Returns dict with audit scores or {scores=None, error=...} on failure.
    """
    if not messages:
        return _empty_result("No messages to analyze", contact_name)

    transcript = format_for_analysis(messages, agent_name, contact_name)
    label_line = (
        f"\nLabel(s) assigned by agent: {', '.join(assigned_labels)}\n"
        if assigned_labels
        else "\nLabel(s) assigned by agent: (none recorded)\n"
    )
    user_content = (
        f"Analyze this conversation and return your JSON audit."
        f"{label_line}"
        f"\n{transcript}"
    )

    # ── NIM agent path: dedicated key, cooldown retry ─────────────────
    nim_key = _pool._pick_nim_key(agent_name)
    if nim_key is not None:
        return _run_with_nim_key(nim_key, user_content, contact_name)

    # ── Groq agent path: shared pool, rotate on rate-limit ────────────
    return _run_with_groq_pool(user_content, contact_name)


def _run_with_nim_key(pk: "PooledKey", user_content: str, contact_name: str) -> dict:
    retries = _max_retries()
    raw = ""
    for attempt in range(retries):
        if not pk.is_ready:
            wait = pk.wait_seconds
            logger.info(
                f"[Analyzer] NIM key cooling for {contact_name} — waiting {wait:.1f}s"
            )
            time.sleep(wait + 0.5)
        try:
            raw = pk.provider.generate(
                system_prompt=SYSTEM_PROMPT,
                user_content=user_content,
                max_tokens=1200,
                temperature=0.1,
            )
            return _finalize_result(raw, pk, contact_name)
        except ProviderQuotaExhaustedError as e:
            _pool.mark_quota_exhausted(pk)
            return _empty_result(str(e), contact_name)
        except ProviderRateLimitError as e:
            _pool.mark_rate_limited(pk, e.retry_after)
            logger.warning(
                f"[Analyzer] NIM key rate-limited for {contact_name} — retrying"
            )
            continue
        except json.JSONDecodeError as e:
            logger.error(
                f"[Analyzer] JSON parse error for {contact_name} (nim): {e}\nRaw: {raw[:300]}"
            )
            return _empty_result(f"JSON parse error: {e}", contact_name)
        except Exception as e:
            logger.error(f"[Analyzer] NIM failed for {contact_name}: {e}")
            return _empty_result(str(e), contact_name)
    return _empty_result(
        f"NIM key still rate-limited after {retries} retries", contact_name
    )


def _run_with_groq_pool(user_content: str, contact_name: str) -> dict:
    raw = ""
    for cycle in range(_MAX_POOL_CYCLES):
        try:
            pk = _pool._pick_groq_key()
        except RuntimeError as e:
            logger.error(f"[Analyzer] {contact_name}: {e}")
            return _empty_result(str(e), contact_name)

        if not pk.is_ready:
            wait = pk.wait_seconds
            logger.info(
                f"[Analyzer] All Groq keys cooling for {contact_name} — "
                f"waiting {wait:.1f}s for […{pk.key[-6:]}]"
            )
            time.sleep(wait + 0.5)

        try:
            raw = pk.provider.generate(
                system_prompt=SYSTEM_PROMPT,
                user_content=user_content,
                max_tokens=1200,
                temperature=0.1,
            )
            return _finalize_result(raw, pk, contact_name)
        except ProviderQuotaExhaustedError:
            _pool.mark_quota_exhausted(pk)
            continue
        except ProviderRateLimitError as e:
            _pool.mark_rate_limited(pk, e.retry_after)
            logger.warning(
                f"[Analyzer] Groq key […{pk.key[-6:]}] rate-limited for {contact_name} "
                f"— rotating to next key"
            )
            continue
        except json.JSONDecodeError as e:
            logger.error(
                f"[Analyzer] JSON parse error for {contact_name} (groq): {e}\nRaw: {raw[:300]}"
            )
            return _empty_result(f"JSON parse error: {e}", contact_name)
        except Exception as e:
            logger.error(f"[Analyzer] Groq failed for {contact_name}: {e}")
            return _empty_result(str(e), contact_name)

    msg = f"Could not score {contact_name} after {_MAX_POOL_CYCLES} Groq pool cycles"
    logger.error(f"[Analyzer] {msg}")
    return _empty_result(msg, contact_name)


def _finalize_result(raw: str, pk: "PooledKey", contact_name: str) -> dict:
    result = json.loads(raw)
    recs = result.get("recommendations", [])
    if isinstance(recs, str):
        result["recommendations"] = [recs] if recs else []
    result["model_used"] = pk.provider.model_name
    result["contact_name"] = contact_name
    _pool.mark_success(pk)
    logger.debug(
        f"[Analyzer] {contact_name} scored via "
        f"{pk.provider_type}/{pk.provider.model_name}"
    )
    return result
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add ai/analyzer.py tests/test_key_pool_manager.py
git commit -m "feat(analyzer): route analyze_conversation through shared Groq pool"
```

---

## Task 7: Rewrite `analyze_batch()` with same pool logic

**Files:**
- Modify: `ai/analyzer.py` (analyze_batch function)
- Modify: `tests/test_key_pool_manager.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_key_pool_manager.py`:

```python
def test_analyze_batch_uses_groq_pool(tmp_config_dir, monkeypatch):
    from tests.conftest import FakeProvider
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
        {"parsed_messages": [{"sender": "agent", "text": "hi"}], "contact_name": "A"},
        {"parsed_messages": [{"sender": "agent", "text": "hi"}], "contact_name": "B"},
    ]
    results = analyzer_mod.analyze_batch(batch, "Resva1028")
    assert len(results) == 2
    assert results[0]["compliance_score"] == 80
    assert results[1]["compliance_score"] == 70
    assert results[0]["contact_name"] == "A"
    assert results[1]["contact_name"] == "B"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python -m pytest tests/test_key_pool_manager.py::test_analyze_batch_uses_groq_pool -v`
Expected: FAIL (old code uses `assign_key_for_agent`).

- [ ] **Step 3: Rewrite `analyze_batch`**

Replace the entire `analyze_batch` function (original lines 320-468) with:

```python
def analyze_batch(
    batch: list[dict],
    agent_name: str,
    *,
    model: str | None = None,
) -> list[dict]:
    """
    Analyze multiple conversations in a single API call.

    Each item in `batch` must have:
      - "parsed_messages": list[dict]
      - "contact_name": str
      - "assigned_labels": list[str] | None

    Returns a list of result dicts in the same order as `batch`.
    """
    if not batch:
        return []

    sections: list[str] = []
    contact_names: list[str] = []
    for i, convo in enumerate(batch, 1):
        parsed = convo.get("parsed_messages") or []
        contact = convo.get("contact_name") or "Contact"
        labels = convo.get("assigned_labels") or []
        contact_names.append(contact)

        if not parsed:
            sections.append(f"────── CONVERSATION {i}: {contact} ──────\n(No messages)\n")
            continue

        transcript = format_for_analysis(parsed, agent_name, contact)
        label_line = (
            f"Label(s) assigned by agent: {', '.join(labels)}"
            if labels
            else "Label(s) assigned by agent: (none recorded)"
        )
        sections.append(
            f"────── CONVERSATION {i}: {contact} ──────\n"
            f"{label_line}\n\n{transcript}\n"
        )

    user_content = (
        f"Analyze each conversation below and return a JSON object with a "
        f"\"results\" key containing an array of {len(batch)} audit objects, "
        f"one per conversation, in the same order.\n\n"
        + "\n".join(sections)
    )

    nim_key = _pool._pick_nim_key(agent_name)
    if nim_key is not None:
        return _run_batch_with_nim_key(nim_key, user_content, contact_names, len(batch))
    return _run_batch_with_groq_pool(user_content, contact_names, len(batch))


def _run_batch_with_nim_key(
    pk: "PooledKey",
    user_content: str,
    contact_names: list[str],
    batch_size: int,
) -> list[dict]:
    retries = _max_retries()
    raw = ""
    for attempt in range(retries):
        if not pk.is_ready:
            time.sleep(pk.wait_seconds + 0.5)
        try:
            raw = pk.provider.generate(
                system_prompt=BATCH_SYSTEM_PROMPT,
                user_content=user_content,
                max_tokens=1200 * batch_size,
                temperature=0.1,
            )
            return _finalize_batch_results(raw, pk, contact_names, batch_size)
        except ProviderQuotaExhaustedError as e:
            _pool.mark_quota_exhausted(pk)
            return [_empty_result(str(e), c) for c in contact_names]
        except ProviderRateLimitError as e:
            _pool.mark_rate_limited(pk, e.retry_after)
            continue
        except json.JSONDecodeError as e:
            logger.error(f"[Analyzer] Batch JSON parse (nim): {e}\nRaw: {raw[:500]}")
            return [_empty_result(f"Batch JSON parse error: {e}", c) for c in contact_names]
        except Exception as e:
            logger.error(f"[Analyzer] Batch NIM failed: {e}")
            return [_empty_result(str(e), c) for c in contact_names]
    return [
        _empty_result(f"NIM batch still rate-limited after {retries} retries", c)
        for c in contact_names
    ]


def _run_batch_with_groq_pool(
    user_content: str,
    contact_names: list[str],
    batch_size: int,
) -> list[dict]:
    raw = ""
    for cycle in range(_MAX_POOL_CYCLES):
        try:
            pk = _pool._pick_groq_key()
        except RuntimeError as e:
            return [_empty_result(str(e), c) for c in contact_names]

        if not pk.is_ready:
            time.sleep(pk.wait_seconds + 0.5)

        try:
            raw = pk.provider.generate(
                system_prompt=BATCH_SYSTEM_PROMPT,
                user_content=user_content,
                max_tokens=1200 * batch_size,
                temperature=0.1,
            )
            return _finalize_batch_results(raw, pk, contact_names, batch_size)
        except ProviderQuotaExhaustedError:
            _pool.mark_quota_exhausted(pk)
            continue
        except ProviderRateLimitError as e:
            _pool.mark_rate_limited(pk, e.retry_after)
            continue
        except json.JSONDecodeError as e:
            logger.error(f"[Analyzer] Batch JSON parse (groq): {e}\nRaw: {raw[:500]}")
            return [_empty_result(f"Batch JSON parse error: {e}", c) for c in contact_names]
        except Exception as e:
            logger.error(f"[Analyzer] Batch Groq failed: {e}")
            return [_empty_result(str(e), c) for c in contact_names]

    msg = f"Batch could not be scored after {_MAX_POOL_CYCLES} Groq pool cycles"
    return [_empty_result(msg, c) for c in contact_names]


def _finalize_batch_results(
    raw: str,
    pk: "PooledKey",
    contact_names: list[str],
    batch_size: int,
) -> list[dict]:
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        results_list = parsed
    elif isinstance(parsed, dict) and "results" in parsed:
        results_list = parsed["results"]
    else:
        results_list = [parsed]

    out: list[dict] = []
    for idx, r in enumerate(results_list):
        recs = r.get("recommendations", [])
        if isinstance(recs, str):
            r["recommendations"] = [recs] if recs else []
        r["model_used"] = pk.provider.model_name
        r["contact_name"] = (
            contact_names[idx] if idx < len(contact_names) else "Contact"
        )
        out.append(r)

    while len(out) < batch_size:
        i = len(out)
        out.append(
            _empty_result(
                "Model did not return result for this conversation",
                contact_names[i] if i < len(contact_names) else "Contact",
            )
        )

    _pool.mark_success(pk)
    return out
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add ai/analyzer.py tests/test_key_pool_manager.py
git commit -m "feat(analyzer): route analyze_batch through shared Groq pool"
```

---

## Task 8: Update analyzer.py module docstring

**Files:**
- Modify: `ai/analyzer.py` (top-of-file docstring)

- [ ] **Step 1: Replace docstring**

In `ai/analyzer.py`, replace the top docstring (lines 1-12) with:

```python
"""
Multi-provider AI analyzer — shared Groq pool + dedicated NIM keys.

Groq-eligible agents (everything NOT in agent_keys.json with provider="nim")
use a shared pool of keys loaded from config/groq_keys.json.
LRU selection spreads load evenly; rate-limited keys rotate automatically.

NIM agents keep dedicated keys in config/agent_keys.json.

Public API:
    analyze_conversation(...)  → dict
    analyze_batch(...)         → list[dict]
    get_pool_status()          → dict   (for /api/ai/status)
"""
```

- [ ] **Step 2: Run full test suite to confirm nothing broke**

Run: `python -m pytest tests/test_key_pool_manager.py -v`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add ai/analyzer.py
git commit -m "docs(analyzer): update module docstring for shared Groq pool"
```

---

## Task 9: Migrate `config/agent_keys.json` to NIM-only

**Files:**
- Modify: `config/agent_keys.json`
- Delete: `config/gemini_keys.json`

- [ ] **Step 1: Overwrite `config/agent_keys.json` with NIM-only content**

Write `config/agent_keys.json`:

```json
{
  "resva1054": {
    "provider": "nim",
    "key": "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"
  },
  "resva1055": {
    "provider": "nim",
    "key": "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"
  },
  "resva1056": {
    "provider": "nim",
    "key": "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"
  },
  "resva1057": {
    "provider": "nim",
    "key": "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"
  },
  "resva1058": {
    "provider": "nim",
    "key": "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"
  }
}
```

- [ ] **Step 2: Delete `config/gemini_keys.json`**

Run: `rm "config/gemini_keys.json"`
Expected: File is gone.

- [ ] **Step 3: Verify via import that the pool loads correctly**

Run: `python -c "from ai.analyzer import _pool; _pool.ensure_loaded(); import json; print(json.dumps(_pool.get_status(), indent=2))"`
Expected: JSON output showing `"groq": {"total": 14, ...}` and `"nim": {"total": 5, ...}`, `total_keys: 19`.

- [ ] **Step 4: Commit**

```bash
git add config/agent_keys.json
git rm config/gemini_keys.json
git commit -m "config: migrate agent_keys.json to NIM-only; remove gemini_keys.json"
```

---

## Task 10: Update dashboard docstring for new status shape

**Files:**
- Modify: `dashboard/app.py` (the `/api/ai/status` docstring around line 650)

- [ ] **Step 1: Read current docstring to locate exact position**

Run: `grep -n "exhausted\|cooling_keys\|available_keys" dashboard/app.py`

- [ ] **Step 2: Replace the status endpoint response example**

In `dashboard/app.py`, find the docstring block containing:
```
            "total_keys": 14,
            "available_keys": 13,
            "cooling_keys": 1,
            "providers": {
              "groq": {"total": 14, "available": 13, "model": "...", "success": 42, "failures": 1}
            }
```

Replace it with:
```
            "total_keys": 19,
            "available_keys": 17,
            "cooling_keys": 2,
            "exhausted_keys": 0,
            "providers": {
              "groq": {"total": 14, "available": 13, "cooling": 1, "exhausted": 0, "model": "...", "success": 42, "failures": 1},
              "nim":  {"total": 5,  "available": 4,  "cooling": 1, "exhausted": 0, "model": "...", "success": 38, "failures": 1}
            }
```

- [ ] **Step 3: Commit**

```bash
git add dashboard/app.py
git commit -m "docs(dashboard): update /api/ai/status response example for shared pool"
```

---

## Task 11: Update `ai/providers/__init__.py` docstring

**Files:**
- Modify: `ai/providers/__init__.py`

- [ ] **Step 1: Replace file contents**

Overwrite `ai/providers/__init__.py` with:

```python
# AI Provider abstractions — shared Groq pool + per-agent NIM keys
```

- [ ] **Step 2: Commit**

```bash
git add ai/providers/__init__.py
git commit -m "docs(providers): update package docstring for shared-pool model"
```

---

## Task 12: Document the shared-pool model in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append section at the end of `CLAUDE.md`**

Append to `CLAUDE.md`:

```markdown

---

## AI Key Pool Model (April 2026)

**Two stores in `ai.analyzer.KeyPoolManager`:**

1. **Groq shared pool** — `config/groq_keys.json` (flat list of key strings).
   Every agent NOT listed in `agent_keys.json` as a NIM entry uses this pool.
   Selection is LRU; rate-limited keys rotate automatically; quota-exhausted
   keys are permanently removed from rotation.

2. **NIM dedicated keys** — `config/agent_keys.json` (only `provider: "nim"` entries).
   Each NIM agent has its own key. Non-NIM entries here are ignored (logged warning).

**Guarantee:** No conversation is skipped due to rate limits. The Groq pool
cycles up to 10 times (≈140 key attempts) before giving up. A skip only
happens when the model returns malformed JSON — that is a data issue, not
a key issue.

**Do NOT:**
- Put Groq keys in `agent_keys.json` — they'll be logged as warnings and ignored
- Use `networkidle` waits anywhere (existing rule — SPA login)
- Add Gemini support — removed April 2026
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document shared Groq pool model in CLAUDE.md"
```

---

## Task 13: End-to-end smoke tests

**Files:** (no code changes — validation only)

- [ ] **Step 1: Run the full pytest suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass (should be ~15 tests total).

- [ ] **Step 2: Verify pool loads from real config**

Run:
```bash
python -c "from ai.analyzer import _pool; _pool.ensure_loaded(); import json; print(json.dumps(_pool.get_status(), indent=2))"
```
Expected output includes:
- `"total_keys": 19`
- `"providers": {"groq": {"total": 14, ...}, "nim": {"total": 5, ...}}`

- [ ] **Step 3: Import check — no circular imports or syntax errors**

Run: `python -c "import ai.analyzer; import dashboard.app; print('OK')"`
Expected: `OK`.

- [ ] **Step 4: Update Obsidian knowledge base (per project rules)**

Append a session log entry to `C:\Users\vos\Desktop\obsidian_brain\01-projects\TEXTING AUDIT AUTOMATION.md`:

```markdown
### 2026-04-14 — Shared Groq pool refactor
**What we worked on:**
- Removed Gemini provider entirely
- Refactored `KeyPoolManager` to use shared Groq pool + dedicated NIM keys
- Added LRU selection, quota-exhaustion tracking, rate-limit rotation
- Added unit tests (`tests/test_key_pool_manager.py`)

**Decisions made:**
- All non-NIM agents share `config/groq_keys.json` pool
- `agent_keys.json` is NIM-only
- No-skip guarantee: rotate up to 10 full pool cycles on rate limits

**Problems / Gotchas:**
- `agent_keys.json` previously had Gemini + null entries — removed
- Put Groq keys in `groq_keys.json` only (not `agent_keys.json`)

**Next steps:** Run against real SmarterContact data and verify dashboard status.
```

- [ ] **Step 5: Verify clean working tree**

Run: `git status`
Expected: Working tree clean (the Obsidian update in Step 4 is outside the repo and does not need committing here).
