# Shared Groq Pool — Design Spec

**Date:** 2026-04-14
**Project:** TEXTING AUDIT AUTOMATION
**Status:** Approved — ready for implementation planning

---

## Problem

After removing Gemini support, 15 agents that previously had Gemini keys are broken. Additionally, 1 agent (`resva1059`) has no key assigned. The current per-agent dedicated-key model in `config/agent_keys.json` does not scale when we have more agents than available Groq keys.

We have:
- 34 total agents in `config/agents.json`
- 14 Groq API keys available in `config/groq_keys.json`
- 5 NIM agents with dedicated NIM keys
- 15 agents orphaned after Gemini removal
- 1 agent with null key

We need a way for all 29 non-NIM agents to share the 14-key Groq pool without any conversation being skipped.

## Goals

1. **Every conversation gets scored** — never skip a conversation due to rate limits
2. **Shared pool model** — 14 Groq keys shared across all non-NIM agents
3. **NIM agents unchanged** — 5 NIM agents keep their dedicated keys
4. **Load balancing** — spread API calls evenly across the pool (LRU selection)
5. **Graceful degradation** — when a key hits daily quota, remove it from rotation; continue with remaining keys

## Non-Goals

- No migration of existing data required (pool rebuilt from config on startup)
- No changes to `main.py`, scraper layer, or database layer
- No Gemini support (already removed)
- No changes to the provider interface (`AIProvider` base class stays as-is)

---

## Architecture

**Two-tier key assignment:**

1. **Groq Pool (shared)** — A flat list of Groq API keys in `config/groq_keys.json`. All Groq-eligible agents share this pool. No per-agent assignment.

2. **NIM Dedicated (per-agent)** — NIM agents stay in `config/agent_keys.json` with `{"provider": "nim", "key": "..."}` entries. Existing behavior unchanged.

**Agent classification at startup:**
- If an agent appears in `agent_keys.json` with `provider: "nim"` → NIM agent (dedicated key)
- Otherwise → Groq agent (pulls from shared pool)
- `agent_keys.json` becomes NIM-only after migration

**Single `KeyPoolManager`** with two selection paths:
- `_pick_nim_key(agent_name)` → returns that agent's dedicated NIM `PooledKey`
- `_pick_groq_key()` → returns the least-recently-used, non-cooling Groq `PooledKey` from the shared pool
- If all Groq keys are cooling → returns the one recovering soonest (caller sleeps, then uses it)

---

## Pool Selection Logic

### Groq key selection algorithm

```python
def _pick_groq_key() -> PooledKey:
    with lock:
        # Filter out permanently exhausted keys
        usable = [k for k in groq_pool if not k.quota_exhausted]
        if not usable:
            raise RuntimeError("All Groq keys exhausted — cannot score conversation")

        ready = [k for k in usable if k.is_ready]
        if ready:
            # LRU: pick the key used least recently to spread load evenly
            chosen = min(ready, key=lambda k: k.last_used_at)
            chosen.last_used_at = time.monotonic()
            return chosen
        else:
            # All usable keys cooling — return the one recovering soonest
            # Caller sleeps for wait_seconds before invoking provider.generate()
            return min(usable, key=lambda k: k.cool_until)
```

### Rate-limit retry loop in `analyze_conversation()`

```python
MAX_POOL_CYCLES = 10   # safety cap: 10 full rotations through the pool before raising

for cycle in range(MAX_POOL_CYCLES):
    key = _pick_groq_key()   # raises if all exhausted
    if not key.is_ready:
        time.sleep(key.wait_seconds + 0.5)
    try:
        return key.provider.generate(...)
    except ProviderRateLimitError as e:
        _pool.mark_rate_limited(key, e.retry_after)
        continue
    except ProviderQuotaExhaustedError:
        _pool.mark_quota_exhausted(key)   # permanently remove from rotation
        continue
    except (json.JSONDecodeError, Exception):
        # Model output issue or transient error — do not retry
        return _empty_result(...)

raise RuntimeError(f"Could not score {contact_name} after {MAX_POOL_CYCLES} pool cycles")
```

**Guarantee:** Every conversation gets scored as long as at least one Groq key has quota remaining. Up to 140 attempts (14 keys × 10 cycles) before raising.

---

## Data Model

### `PooledKey` dataclass (updated)

```python
@dataclass
class PooledKey:
    key: str
    provider: AIProvider
    provider_type: str                 # "groq" or "nim"
    cool_until: float = 0.0            # monotonic timestamp when usable again
    last_used_at: float = 0.0          # NEW: for LRU selection in shared pool
    quota_exhausted: bool = False      # NEW: permanent removal from rotation
    success_count: int = 0
    fail_count: int = 0

    @property
    def is_ready(self) -> bool:
        return not self.quota_exhausted and time.monotonic() >= self.cool_until

    @property
    def wait_seconds(self) -> float:
        return max(0.0, self.cool_until - time.monotonic())
```

### `KeyPoolManager` internal state

```python
class KeyPoolManager:
    _groq_pool: list[PooledKey]          # shared pool from groq_keys.json
    _nim_keys: dict[str, PooledKey]      # agent_name.lower() → NIM PooledKey
    _lock: threading.Lock
    _loaded: bool
```

### Config file changes

**`config/groq_keys.json`** — unchanged structure, now the source of truth for all Groq keys:
```json
[
  "[REDACTED]",
  "[REDACTED]",
  ...
]
```

**`config/agent_keys.json`** — reduced to NIM-only entries:
```json
{
  "resva1054": {"provider": "nim", "key": "nvapi-..."},
  "resva1055": {"provider": "nim", "key": "nvapi-..."},
  "resva1056": {"provider": "nim", "key": "nvapi-..."},
  "resva1057": {"provider": "nim", "key": "nvapi-..."},
  "resva1058": {"provider": "nim", "key": "nvapi-..."}
}
```
- All Groq entries removed (now come from `groq_keys.json`)
- All Gemini entries deleted
- Null entry for `resva1059` deleted (falls through to Groq pool)

**`config/gemini_keys.json`** — deleted entirely (leftover from previous removal).

---

## Agent Routing

In `analyze_conversation(agent_name, ...)`:

1. Look up `_nim_keys[agent_name.lower()]` → if found, use that dedicated NIM key (existing serial behavior with cooldown retry on the single key)
2. Otherwise → call `_pick_groq_key()` and use the shared pool retry loop
3. No agent is ever "unknown" — every agent not in `_nim_keys` routes through the Groq pool

---

## Dashboard Status Endpoint

`get_pool_status()` returns:

```json
{
  "total_keys": 19,
  "available_keys": 17,
  "cooling_keys": 2,
  "exhausted_keys": 0,
  "providers": {
    "groq": {
      "total": 14,
      "available": 13,
      "cooling": 1,
      "exhausted": 0,
      "model": "llama-3.3-70b-versatile",
      "success": 42,
      "failures": 1
    },
    "nim": {
      "total": 5,
      "available": 4,
      "cooling": 1,
      "exhausted": 0,
      "model": "...",
      "success": 38,
      "failures": 1
    }
  }
}
```

New field `exhausted` per provider tracks keys removed from rotation due to daily quota.

---

## Error Handling

| Error | Action |
|-------|--------|
| `ProviderRateLimitError` (429) | Mark key cooling via `mark_rate_limited()`, pick next key, retry |
| `ProviderQuotaExhaustedError` (daily limit: 0) | Mark key `quota_exhausted=True`, remove from rotation, pick next |
| `json.JSONDecodeError` | Return `_empty_result()` — model output issue, retry won't help |
| Network / unknown exception | Return `_empty_result()` — log the error |
| All Groq keys quota-exhausted | Raise `RuntimeError` — true failure, caller must surface |

**Note on "never skip":** The no-skip guarantee covers rate limits and transient failures. A malformed JSON response from the model still returns `_empty_result()` because retrying the same content yields the same output. This matches existing behavior.

---

## Concurrency Model

- `main.py` runs up to `MAX_PARALLEL_WORKERS=5` agents in parallel (Playwright workers)
- Each worker calls `analyze_conversation()` / `analyze_batch()` serially per agent
- Peak concurrent Groq pool access = 5 threads
- `threading.Lock` in `KeyPoolManager` serializes key selection and state mutation (microseconds)
- `provider.generate()` (network I/O) runs **outside** the lock — no contention on the slow path

---

## Migration Steps (one-time)

1. Delete `config/gemini_keys.json`
2. Rewrite `config/agent_keys.json` to contain only the 5 NIM entries (`resva1054` through `resva1058`)
3. Verify `config/groq_keys.json` has the 14 Groq keys
4. No data migration needed — pool is rebuilt from config on every startup

---

## Testing Plan

### Unit tests (mock `AIProvider`)

- **Single ready key** → `_pick_groq_key()` returns that key, updates `last_used_at`
- **Multiple ready keys** → returns the LRU key (lowest `last_used_at`)
- **All keys cooling** → returns the one with lowest `cool_until`, caller receives non-zero `wait_seconds`
- **Quota exhaustion** → key marked `quota_exhausted`, next `_pick_groq_key()` skips it
- **All keys exhausted** → `_pick_groq_key()` raises `RuntimeError`
- **Rate limit then success** → retry loop picks next key, scores successfully
- **NIM agent** → bypasses Groq pool, uses dedicated NIM key

### Integration tests

- `python main.py --single "Resva1028"` (former Gemini agent) — verify it scores end-to-end via a Groq pool key
- `python main.py --single "Resva1054"` (NIM agent) — verify it still uses its dedicated NIM key (regression)
- `python main.py --single "Resva1059"` (formerly null) — verify it now scores via Groq pool

---

## Files Modified

| File | Change |
|------|--------|
| `ai/analyzer.py` | Refactored `KeyPoolManager`, new `_groq_pool` + `_nim_keys`, LRU selection, quota tracking |
| `ai/providers/__init__.py` | Docstring update |
| `config/agent_keys.json` | Trimmed to 5 NIM entries only |
| `config/gemini_keys.json` | Deleted |
| `dashboard/app.py` | Updated `get_pool_status()` response shape in docstring, added `exhausted_keys` field |
| `CLAUDE.md` | Brief note on the new shared-pool model |

## Files NOT Modified

- `main.py`, `scraper/`, `database/` — no changes
- `config/agents.json`, `config/groq_keys.json`, `config/agent_roster.json` — unchanged
- `ai/providers/base.py`, `ai/providers/groq_provider.py`, `ai/providers/nim_provider.py` — unchanged

---

## Success Criteria

1. All 34 agents in `agents.json` can score conversations successfully
2. No conversation is skipped due to rate limits (only due to malformed model output, which matches existing behavior)
3. Groq API calls are distributed evenly across the 14-key pool (verified via `success_count` per key after a full run)
4. NIM agents continue using dedicated NIM keys with no regression
5. Dashboard `/api/ai/status` endpoint shows accurate pool state including cooling and exhausted counts
