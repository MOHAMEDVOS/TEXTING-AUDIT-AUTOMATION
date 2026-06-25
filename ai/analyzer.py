"""
Groq AI analyzer — shared key pool with LRU rotation.

Keys are loaded from the api_keys database table (provider='groq').
LRU selection spreads load evenly; rate-limited keys rotate automatically.

Public API:
    analyze_conversation(...)  → dict
    analyze_batch(...)         → list[dict]
    get_pool_status()          → dict   (for /api/ai/status)
"""
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import GROQ_MODEL, PROJECT_ROOT, PREFILTER_DISABLE_GROQ
from config.rate_limiter import get_rate_limiter, groq_key_bucket
from ai.prompts import get_system_prompt, format_for_analysis
from ai.providers.base import (
    AIProvider,
    ProviderPayloadTooLargeError,
    ProviderQuotaExhaustedError,
    ProviderRateLimitError,
)

# ── Rate-limiter singleton ────────────────────────────────────────────────────
_rl = get_rate_limiter()

# Groq free-tier bucket defaults — 5 burst, 1 request per 2 s sustained.
# Override via env vars if you have paid-tier keys.
_GROQ_BUCKET_CAPACITY = float(os.getenv("GROQ_RL_CAPACITY", "5"))
_GROQ_BUCKET_RATE     = float(os.getenv("GROQ_RL_RATE",     "0.5"))

logger = logging.getLogger(__name__)

_DEFAULT_COOLDOWN = 60  # seconds
_MAX_POOL_CYCLES  = 10  # max full rotations through the Groq pool before giving up
_LEASE_SECONDS    = 15  # how long a reserved Groq key is held before auto-expiring
_MIN_NO_KEY_WAIT  = 2.0
_MAX_NO_KEY_WAIT  = 30.0
_MAX_NO_KEY_POLLS = 40  # safety guard when DB never yields an available key
_PINNED_FALLBACK_AFTER_429S = 1
_ANALYSIS_INPUT_BUDGET_BYTES = 42_000
_COMPACT_ANALYSIS_INPUT_BUDGET_BYTES = 36_000
_ANALYSIS_INPUT_SLACK_BYTES = 1_200
_MIN_TRANSCRIPT_BYTES = 1_200

# ── 30-day rolling window for conversation auditing ──────────────────────────
# Only messages within the last 30 days from the newest message are audited.
# This prevents stale history (months/years old) from skewing current scores.
_AUDIT_WINDOW_DAYS = 30


def _parse_message_date(date_str: str):
    """Parse a SmarterContact date string like 'Thursday, March 26, 2026' into a date object.

    Returns None if the string can't be parsed.
    """
    from datetime import datetime as _dt
    if not date_str or not date_str.strip():
        return None
    s = date_str.strip()
    # Format: "Thursday, March 26, 2026" → "%A, %B %d, %Y"
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def filter_recent_messages(
    messages: list[dict],
    window_days: int = _AUDIT_WINDOW_DAYS,
) -> list[dict]:
    """Return only messages within `window_days` of the latest message date.

    Messages without a parseable date are assigned the most recently seen date
    (same-day messages at the top of the conversation have date="").

    If no dates can be parsed at all, returns the original list unchanged.
    """
    from datetime import timedelta

    if not messages:
        return messages

    # Pass 1: parse all dates, propagate to dateless messages
    dated: list[tuple[dict, "date | None"]] = []
    last_known_date = None
    for msg in messages:
        d = _parse_message_date(msg.get("date") or "")
        if d is not None:
            last_known_date = d
        dated.append((msg, d if d is not None else last_known_date))

    # Find the latest date across all messages
    all_dates = [d for _, d in dated if d is not None]
    if not all_dates:
        return messages  # no parseable dates → audit everything

    latest_date = max(all_dates)
    cutoff = latest_date - timedelta(days=window_days)

    # Pass 2: keep only messages on or after the cutoff
    filtered = [msg for msg, d in dated if d is not None and d >= cutoff]

    if not filtered:
        return messages  # safety: never return empty if input had messages

    if len(filtered) < len(messages):
        dropped = len(messages) - len(filtered)
        logger.info(
            f"[Analyzer] 30-day window: kept {len(filtered)}/{len(messages)} messages "
            f"(dropped {dropped} older than {cutoff.isoformat()})"
        )

    return filtered

# ── Global concurrency cap ────────────────────────────────────────────────────
# Limits how many Groq API calls are in-flight at the SAME MOMENT across ALL
# parallel agents. With 14 free-tier keys (~2 burst req/sec each), capping at
# 4 simultaneous calls prevents TPM burst spikes that trigger 429s even when
# total per-minute usage is within limits.
_GROQ_CALL_SEMAPHORE = threading.Semaphore(4)


# ── Cross-process key reservation (Postgres-backed) ───────────────────────────
# Multiple subprocesses share the same Groq key pool. In-memory LRU can't
# coordinate across processes, so reservation lives in the api_keys table.

def _build_label_line(assigned_labels: list[str] | None) -> str:
    if assigned_labels:
        return f"\nLabel(s) assigned by agent: {', '.join(assigned_labels)}\n"
    return "\nLabel(s) assigned by agent: (none recorded)\n"


def _build_single_analysis_payload(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    assigned_labels: list[str] | None,
    funnel_tier: str | None,
    guidelines: str | None,
    *,
    include_learned_rules: bool,
    total_budget_bytes: int,
) -> tuple[str, str]:
    prefix = "Analyze this conversation and return your JSON audit."
    label_line = _build_label_line(assigned_labels)

    system_prompt = get_system_prompt(
        batch=False,
        funnel_tier=funnel_tier,
        guidelines=guidelines,
        include_learned_rules=include_learned_rules,
    )
    static_user_content = f"{prefix}{label_line}\n"

    available_bytes = (
        total_budget_bytes
        - len(system_prompt.encode("utf-8"))
        - len(static_user_content.encode("utf-8"))
        - _ANALYSIS_INPUT_SLACK_BYTES
    )
    if include_learned_rules and available_bytes < _MIN_TRANSCRIPT_BYTES:
        logger.info(
            f"[Analyzer] {contact_name} prompt budget too tight with learned rules "
            f"({len(system_prompt.encode('utf-8'))}B) — retrying without them"
        )
        system_prompt = get_system_prompt(
            batch=False,
            funnel_tier=funnel_tier,
            guidelines=guidelines,
            include_learned_rules=False,
        )
        available_bytes = (
            total_budget_bytes
            - len(system_prompt.encode("utf-8"))
            - len(static_user_content.encode("utf-8"))
            - _ANALYSIS_INPUT_SLACK_BYTES
        )

    transcript_budget = max(_MIN_TRANSCRIPT_BYTES, available_bytes)
    transcript = format_for_analysis(
        messages,
        agent_name,
        contact_name,
        max_bytes=transcript_budget,
    )
    return system_prompt, f"{static_user_content}{transcript}"


def _db_reserve_groq_key(lease_seconds: int = _LEASE_SECONDS) -> "tuple[int, str] | None":
    """
    Atomically reserve the best-available shared Groq key across all processes.
    Returns (id, api_key) or None if nothing is currently available.
    """
    import psycopg2
    from config.settings import DATABASE_URL
    conn = psycopg2.connect(DATABASE_URL)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("""
            SELECT id, api_key FROM api_keys
             WHERE provider = 'groq'
               AND agent_name IS NULL
               AND (reserved_until IS NULL OR reserved_until < now())
               AND (cool_until     IS NULL OR cool_until     < now())
             ORDER BY last_used_at_db NULLS FIRST
             LIMIT 1
             FOR UPDATE SKIP LOCKED
        """)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        key_id, api_key = row
        cur.execute("""
            UPDATE api_keys
               SET reserved_until  = now() + (%s || ' seconds')::interval,
                   last_used_at_db = now()
             WHERE id = %s
        """, (str(lease_seconds), key_id))
        conn.commit()
        return (key_id, api_key)
    finally:
        conn.close()


def _db_reserve_specific_groq_key(api_key: str, lease_seconds: int = _LEASE_SECONDS) -> "tuple[int, str] | None":
    """
    Reserve one specific shared Groq key (for per-run agent isolation).
    Returns (id, api_key) or None if this key is reserved/cooling/unavailable.
    """
    import psycopg2
    from config.settings import DATABASE_URL
    conn = psycopg2.connect(DATABASE_URL)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, api_key FROM api_keys
             WHERE provider = 'groq'
               AND agent_name IS NULL
               AND api_key = %s
               AND (reserved_until IS NULL OR reserved_until < now())
               AND (cool_until     IS NULL OR cool_until     < now())
             LIMIT 1
             FOR UPDATE SKIP LOCKED
            """,
            (api_key,),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        key_id, key_value = row
        cur.execute(
            """
            UPDATE api_keys
               SET reserved_until  = now() + (%s || ' seconds')::interval,
                   last_used_at_db = now()
             WHERE id = %s
            """,
            (str(lease_seconds), key_id),
        )
        conn.commit()
        return (key_id, key_value)
    finally:
        conn.close()


def _db_release_groq_key(key_id: int) -> None:
    """Clear reservation after a successful call (key is immediately reusable)."""
    import psycopg2
    from config.settings import DATABASE_URL
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE api_keys SET reserved_until = NULL WHERE id = %s", (key_id,))
        conn.commit()
    finally:
        conn.close()


def _db_cooldown_groq_key(key_id: int, seconds: float) -> None:
    """Mark a key as cooling (after 429 / quota / JSON error). Clears reservation."""
    import psycopg2
    from config.settings import DATABASE_URL
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE api_keys
               SET cool_until     = GREATEST(COALESCE(cool_until, now()), now() + (%s || ' seconds')::interval),
                   reserved_until = NULL
             WHERE id = %s
        """, (str(max(seconds, 10.0)), key_id))
        conn.commit()
    finally:
        conn.close()


def _db_seconds_until_groq_key_available() -> float:
    """
    Return seconds until the next shared Groq key becomes eligible for reservation.
    Uses DB truth (cool_until + reserved_until) so all processes wait intelligently.
    """
    import psycopg2
    from config.settings import DATABASE_URL

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(next_ready_at)
                FROM (
                    SELECT GREATEST(
                        COALESCE(reserved_until, now()),
                        COALESCE(cool_until, now())
                    ) AS next_ready_at
                    FROM api_keys
                    WHERE provider = 'groq'
                      AND agent_name IS NULL
                ) t
                """
            )
            row = cur.fetchone()
            next_ready_at = row[0] if row else None
    finally:
        conn.close()

    if next_ready_at is None:
        return _MIN_NO_KEY_WAIT

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    wait_seconds = (next_ready_at - now).total_seconds()
    return max(0.0, wait_seconds)


def _db_seconds_until_specific_groq_key_available(api_key: str) -> float:
    """
    Return seconds until one specific shared Groq key is eligible again.
    """
    import psycopg2
    from config.settings import DATABASE_URL

    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT GREATEST(
                    COALESCE(reserved_until, now()),
                    COALESCE(cool_until, now())
                )
                FROM api_keys
                WHERE provider = 'groq'
                  AND agent_name IS NULL
                  AND api_key = %s
                LIMIT 1
                """,
                (api_key,),
            )
            row = cur.fetchone()
            next_ready_at = row[0] if row else None
    finally:
        conn.close()

    if next_ready_at is None:
        return _MIN_NO_KEY_WAIT

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return max(0.0, (next_ready_at - now).total_seconds())


# ── Pooled Key ────────────────────────────────────────────────────────────────

@dataclass
class PooledKey:
    """One API key + its provider instance + health bookkeeping."""
    key: str
    provider: AIProvider
    provider_type: str = "groq"
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


# ── Key Pool Manager ──────────────────────────────────────────────────────────

class KeyPoolManager:
    """
    Manages the shared Groq key pool (api_keys table, provider='groq').
    Agents select keys via LRU; rate-limited keys rotate automatically.
    """

    def __init__(self):
        self._groq_pool: list[PooledKey] = []           # shared pool, LRU-selected
        self._groq_by_key: dict[str, PooledKey] = {}    # api_key → PooledKey (for DB-reservation lookup)
        self._lock = threading.Lock()
        self._loaded = False

    # ── Loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._loaded:
            return

        self._load_groq_pool()

        logger.info(f"[Pool] Loaded {len(self._groq_pool)} Groq keys (shared pool)")
        self._loaded = True

    def _load_groq_pool(self) -> None:
        """Load Groq shared-pool keys from the api_keys table."""
        import psycopg2
        from config.settings import DATABASE_URL

        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE api_keys
                SET cool_until = NULL, reserved_until = NULL
                WHERE provider = 'groq'
                  AND agent_name IS NULL
                  AND (
                      cool_until      < now() - INTERVAL '30 minutes'
                      OR reserved_until < now() - INTERVAL '30 minutes'
                  )
                """
            )
            conn.commit()
            cur.execute("SELECT api_key FROM api_keys WHERE provider = 'groq' AND agent_name IS NULL ORDER BY id")
            key_list = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()
        except Exception as e:
            # ML-only mode: Groq is optional. A DB/key error must not crash the
            # system — leave the pool empty and let the (guarded) callers no-op.
            logger.warning(f"[Pool] Could not load Groq keys (Groq disabled): {e}")
            return

        if not key_list:
            logger.warning(
                "[Pool] No Groq keys in api_keys table — Groq pool empty "
                "(expected in ML-only mode)"
            )
            return

        from ai.providers.groq_provider import GroqProvider

        for api_key in key_list:
            if not api_key or not isinstance(api_key, str):
                logger.warning(f"[Pool] Skipping invalid Groq key entry: {api_key!r}")
                continue
            try:
                prov = GroqProvider(api_key=api_key, model=GROQ_MODEL)
                pk = PooledKey(key=api_key, provider=prov, provider_type="groq")
                self._groq_pool.append(pk)
                self._groq_by_key[api_key] = pk
                logger.debug(f"[Pool] Groq pool +1 [...{api_key[-6:]}]")
            except Exception as e:
                logger.warning(f"[Pool] Failed to init Groq key [...{api_key[-6:]}]: {e}")

    def ensure_loaded(self) -> None:
        with self._lock:
            self._load()

    # ── Key selection ─────────────────────────────────────────────────────

    def _pick_groq_key_excluding(self, exclude_keys: set[str]) -> "PooledKey":
        """
        Pick the best available Groq key, skipping any key in exclude_keys.
        Raises RuntimeError if all non-excluded keys are exhausted.
        Falls back to _pick_groq_key() if exclude_keys covers everything.
        """
        with self._lock:
            self._load()
            usable = [
                k for k in self._groq_pool
                if not k.quota_exhausted and k.key not in exclude_keys
            ]
            if not usable:
                raise RuntimeError("All untried Groq keys exhausted")
            ready = [k for k in usable if k.is_ready]
            if ready:
                chosen = min(ready, key=lambda k: k.last_used_at)
                chosen.last_used_at = time.monotonic()
                return chosen
            # All untried keys are cooling — return the one recovering soonest
            return min(usable, key=lambda k: k.cool_until)

    def _pick_groq_key(self) -> "PooledKey":
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

    def mark_rate_limited(self, pk: "PooledKey", retry_after: float | None = None) -> None:
        cooldown = (retry_after + 2.0) if retry_after else _DEFAULT_COOLDOWN
        cooldown = max(cooldown, 10.0)  # Floor it at 10s to prevent micro-cooldown pounding
        with self._lock:
            pk.cool_until = time.monotonic() + cooldown
            pk.fail_count += 1

    def mark_success(self, pk: "PooledKey") -> None:
        with self._lock:
            pk.success_count += 1

    def mark_quota_exhausted(self, pk: "PooledKey") -> None:
        """Permanently remove a key from rotation (daily quota hit)."""
        with self._lock:
            pk.quota_exhausted = True
            pk.fail_count += 1
            logger.warning(
                f"[Pool] Key […{pk.key[-6:]}] ({pk.provider_type}) "
                f"marked quota-exhausted — removed from rotation"
            )

    # ── Per-run key assignment ────────────────────────────────────────────

    def assign_run_keys(self, agent_names: list[str]) -> dict[str, "PooledKey"]:
        """
        Randomly assign one unique Groq key per agent for a single run.
        If there are more agents than available keys, keys are reused (round-robin).
        Returns {agent_name.lower(): PooledKey}.
        """
        import random
        with self._lock:
            self._load()
            usable = [k for k in self._groq_pool if not k.quota_exhausted]
            if not usable:
                return {}

            pool_copy = usable[:]
            random.shuffle(pool_copy)

            assignment: dict[str, "PooledKey"] = {}
            for i, name in enumerate(agent_names):
                assignment[name.lower()] = pool_copy[i % len(pool_copy)]
                logger.info(
                    f"[Pool] Run assignment: '{name}' → "
                    f"[…{pool_copy[i % len(pool_copy)].key[-6:]}]"
                )
            return assignment

    # ── Status (for /api/ai/status) ────────────────────────────────────

    def get_status(self) -> dict:
        with self._lock:
            self._load()

            all_keys = list(self._groq_pool)

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


# ── Singleton pool ────────────────────────────────────────────────────────────

_pool = KeyPoolManager()


def get_pool_status() -> dict:
    """Public accessor for the /api/ai/status endpoint."""
    return _pool.get_status()


# ── Helper ────────────────────────────────────────────────────────────────────

def _max_retries() -> int:
    return 5  # retries per key on rate-limit


# ── Main public function — single conversation ───────────────────────────────

def analyze_conversation(
    messages: list[dict],
    agent_name: str,
    contact_name: str = "Contact",
    assigned_labels: list[str] | None = None,
    *,
    model: str | None = None,
    funnel_tier: str | None = None,
    guidelines: str | None = None,
    pinned_key: "PooledKey | None" = None,
    conversation_id: int | None = None,
    db_pool=None,
) -> dict:
    """
    Analyze a single parsed conversation using the shared Groq pool.

    LRU key rotation; on rate-limit the analyzer cycles to the next Groq
    key — a conversation is never skipped due to key issues.

    `funnel_tier` and `guidelines` are per-account overrides injected into the
    system prompt. Pass None to use the global prompt only.

    `conversation_id` + `db_pool` are optional — when both are provided AND
    the ML pre-filter is enabled, the pre-filter may short-circuit Groq for
    confidently-clean conversations. In shadow mode the pre-filter only
    records its decision; Groq still produces the final score.

    Returns dict with audit scores or {scores=None, error=...} on failure.
    """
    if not messages:
        return _empty_result("No messages to analyze", contact_name)

    # ── 30-day rolling window: drop messages older than 30 days ──────
    messages = filter_recent_messages(messages)

    # ── ML pre-filter (Tier 1/2/3) — may short-circuit Groq ──────────
    try:
        from ai.prefilter import run_prefilter
        prefilter_result = run_prefilter(
            messages,
            agent_name,
            contact_name,
            conversation_id=conversation_id,
            funnel_tier=funnel_tier or "NF",
            assigned_labels=assigned_labels or [],
            db_pool=db_pool,
        )
        if prefilter_result is not None:
            if isinstance(prefilter_result, dict):
                flags = list(prefilter_result.get("red_flags") or [])
                if "Continued texting after explicit opt-out." in flags and not _agent_continued_after_opt_out(messages):
                    flags = [f for f in flags if f != "Continued texting after explicit opt-out."]
                if "Gave up after first no with zero rebuttal." in flags and _agent_replied_after_first_soft_no(messages):
                    flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
                if _last_message_from_contact(messages) and "Gave up after first no with zero rebuttal." in flags:
                    flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
                if "Continued original pitch after wrong number." in flags and not _agent_continued_pitch_after_wn(messages):
                    flags = [f for f in flags if f != "Continued original pitch after wrong number."]
                prefilter_result["red_flags"] = flags
                _apply_label_guards(prefilter_result, messages)
            if conversation_id is not None:
                prefilter_result.setdefault("conversation_id", conversation_id)
            return prefilter_result
    except Exception as e:
        logger.warning(f"[Analyzer] Prefilter failed for {contact_name}: {e}")

    # ── ML-only mode: Groq is disabled — never reach the Groq pool ──────
    # run_prefilter already returns a terminal T4 result in this mode; getting
    # here means an edge case (prefilter disabled, no messages, or T4 errored).
    # Produce a deterministic result instead of calling Groq.
    if PREFILTER_DISABLE_GROQ:
        result = _ml_only_fallback(messages, agent_name, contact_name, assigned_labels)
        if conversation_id is not None:
            result.setdefault("conversation_id", conversation_id)
        return result

    system_prompt, user_content = _build_single_analysis_payload(
        messages,
        agent_name,
        contact_name,
        assigned_labels,
        funnel_tier,
        guidelines,
        include_learned_rules=True,
        total_budget_bytes=_ANALYSIS_INPUT_BUDGET_BYTES,
    )

    # Token cost estimation — critical for TPD budget awareness
    _prompt_bytes = len(system_prompt.encode("utf-8")) + len(user_content.encode("utf-8"))
    _est_input_tokens = _prompt_bytes // 4  # rough: 1 token ~ 4 bytes
    logger.info(
        f"[Analyzer] {contact_name} — payload {_prompt_bytes:,}B "
        f"(~{_est_input_tokens:,} input tokens + 1200 output = "
        f"~{_est_input_tokens + 1200:,} total)"
    )

    def _dispatch(current_system_prompt: str, current_user_content: str) -> dict:
        return _run_with_groq_pool(
            current_user_content,
            contact_name,
            current_system_prompt,
            pinned_key=pinned_key,
        )

    try:
        result = _dispatch(system_prompt, user_content)
        # Deterministic guards for flags that require an agent response after the last contact message.
        if isinstance(result, dict):
            flags = list(result.get("red_flags") or [])
            if "Continued texting after explicit opt-out." in flags and not _agent_continued_after_opt_out(messages):
                flags = [f for f in flags if f != "Continued texting after explicit opt-out."]
            if "Gave up after first no with zero rebuttal." in flags and _agent_replied_after_first_soft_no(messages):
                flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
            if _last_message_from_contact(messages) and "Gave up after first no with zero rebuttal." in flags:
                flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
            if "Continued original pitch after wrong number." in flags and not _agent_continued_pitch_after_wn(messages):
                flags = [f for f in flags if f != "Continued original pitch after wrong number."]
            result["red_flags"] = flags
            _apply_label_guards(result, messages)
        return result
    except ProviderPayloadTooLargeError:
        logger.warning(
            f"[Analyzer] Payload too large for {contact_name} - retrying with compact prompt"
        )
        compact_system_prompt, compact_user_content = _build_single_analysis_payload(
            messages,
            agent_name,
            contact_name,
            assigned_labels,
            funnel_tier,
            guidelines,
            include_learned_rules=False,
            total_budget_bytes=_COMPACT_ANALYSIS_INPUT_BUDGET_BYTES,
        )
        try:
            result = _dispatch(compact_system_prompt, compact_user_content)
            if isinstance(result, dict):
                flags = list(result.get("red_flags") or [])
                if "Continued texting after explicit opt-out." in flags and not _agent_continued_after_opt_out(messages):
                    flags = [f for f in flags if f != "Continued texting after explicit opt-out."]
                if "Gave up after first no with zero rebuttal." in flags and _agent_replied_after_first_soft_no(messages):
                    flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
                if _last_message_from_contact(messages) and "Gave up after first no with zero rebuttal." in flags:
                    flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
                result["red_flags"] = flags
                _apply_label_guards(result, messages)
            logger.info(
                f"[Analyzer] Compact retry succeeded for {contact_name} "
                f"(prompt {len(system_prompt.encode('utf-8'))}B -> {len(compact_system_prompt.encode('utf-8'))}B, "
                f"user {len(user_content.encode('utf-8'))}B -> {len(compact_user_content.encode('utf-8'))}B)"
            )
            return result
        except ProviderPayloadTooLargeError:
            msg = (
                f"Could not score {contact_name}: request stayed too large "
                f"even after compact retry"
            )
            logger.error(f"[Analyzer] {msg}")
            return _empty_result(msg, contact_name)

def _run_with_groq_pool(
    user_content: str, contact_name: str, system_prompt: str,
    pinned_key: "PooledKey | None" = None,
) -> dict:
    # If a pinned Groq key is provided, isolate this run to that key only.
    if pinned_key is not None and pinned_key.provider_type == "groq":
        return _run_with_pinned_groq_key(user_content, contact_name, system_prompt, pinned_key)

    _pool.ensure_loaded()

    raw = ""
    tried_ids: set[int] = set()
    cycles = 0
    no_key_polls = 0

    while cycles < _MAX_POOL_CYCLES:
        reservation = _db_reserve_groq_key()
        if reservation is None:
            # All keys reserved/cooling right now — wait until DB says next key is ready.
            wait_s = _db_seconds_until_groq_key_available()
            wait_s = min(max(wait_s, _MIN_NO_KEY_WAIT), _MAX_NO_KEY_WAIT)
            no_key_polls += 1
            logger.info(
                f"[Analyzer] No Groq key available for {contact_name} "
                f"(poll {no_key_polls}, cycle {cycles + 1}/{_MAX_POOL_CYCLES}) "
                f"— waiting {wait_s:.1f}s"
            )
            if no_key_polls >= _MAX_NO_KEY_POLLS:
                msg = (
                    f"Could not score {contact_name}: no Groq key became available "
                    f"after {no_key_polls} waits"
                )
                logger.error(f"[Analyzer] {msg}")
                return _empty_result(msg, contact_name)
            time.sleep(wait_s)
            continue

        no_key_polls = 0
        cycles += 1
        key_id, api_key = reservation
        if key_id in tried_ids:
            # We've already tried this key this conversation; release and wait briefly
            _db_release_groq_key(key_id)
            time.sleep(0.5)
            continue

        pk = _pool._groq_by_key.get(api_key)
        if pk is None:
            # Key exists in DB but not in this process's in-memory pool
            # (e.g. added after startup). Release and skip.
            logger.warning(f"[Analyzer] Reserved key id={key_id} not in local pool — releasing")
            _db_release_groq_key(key_id)
            tried_ids.add(key_id)
            continue

        try:
            # ── Token-bucket pre-check (before hitting the API) ──────────
            _rl_allowed, _rl_retry = _rl.check(
                groq_key_bucket(api_key),
                capacity=_GROQ_BUCKET_CAPACITY,
                rate=_GROQ_BUCKET_RATE,
            )
            if not _rl_allowed:
                # Bucket empty — rotate to the next key instantly (no wait)
                _db_release_groq_key(key_id)
                tried_ids.add(key_id)
                logger.info(
                    f"[RateLimit] Key […{pk.key[-6:]}] bucket empty for {contact_name} "
                    f"— rotating (retry_after={_rl_retry:.1f}s, tried={len(tried_ids)})"
                )
                raise ProviderRateLimitError(retry_after=_rl_retry)

            with _GROQ_CALL_SEMAPHORE:
                raw = pk.provider.generate(
                    system_prompt=system_prompt,
                    user_content=user_content,
                    max_tokens=1200,
                    temperature=0.1,
                )
            _db_release_groq_key(key_id)
            return _finalize_result(raw, pk, contact_name)

        except ProviderQuotaExhaustedError:
            _pool.mark_quota_exhausted(pk)
            _db_cooldown_groq_key(key_id, 24 * 3600)  # out for the day
            tried_ids.add(key_id)
            continue
        except ProviderRateLimitError as e:
            cooldown = (e.retry_after or _DEFAULT_COOLDOWN)
            _db_cooldown_groq_key(key_id, cooldown)
            _pool.mark_rate_limited(pk, e.retry_after)
            tried_ids.add(key_id)
            logger.warning(
                f"[Analyzer] Groq key […{pk.key[-6:]}] rate-limited for {contact_name} "
                f"— trying next key ({len(tried_ids)} tried so far)"
            )
            continue
        except ProviderPayloadTooLargeError:
            _db_release_groq_key(key_id)
            logger.warning(
                f"[Analyzer] Groq request too large for {contact_name} "
                f"on key [...{pk.key[-6:]}] - escalating to caller"
            )
            raise
        except json.JSONDecodeError as e:
            logger.error(
                f"[Analyzer] JSON parse error for {contact_name} (groq): {e}\nRaw: {raw[:300]}"
            )
            _db_cooldown_groq_key(key_id, 5.0)
            tried_ids.add(key_id)
            continue
        except Exception as e:
            logger.error(f"[Analyzer] Groq failed for {contact_name}: {e}")
            _db_cooldown_groq_key(key_id, 5.0)
            tried_ids.add(key_id)
            continue

    msg = f"Could not score {contact_name} after {_MAX_POOL_CYCLES} Groq pool cycles"
    logger.error(f"[Analyzer] {msg}")
    return _empty_result(msg, contact_name)


def _run_with_pinned_groq_key(
    user_content: str,
    contact_name: str,
    system_prompt: str,
    pinned_key: "PooledKey",
) -> dict:
    _pool.ensure_loaded()
    strict_assignment = os.getenv("GROQ_ASSIGNMENT_STRICT", "").strip() == "1"
    raw = ""
    cycles = 0
    no_key_polls = 0
    pinned_rate_limits = 0
    key_suffix = pinned_key.key[-6:]

    while cycles < _MAX_POOL_CYCLES:
        reservation = _db_reserve_specific_groq_key(pinned_key.key)
        if reservation is None:
            wait_s = _db_seconds_until_specific_groq_key_available(pinned_key.key)
            wait_s = min(max(wait_s, _MIN_NO_KEY_WAIT), _MAX_NO_KEY_WAIT)
            no_key_polls += 1
            logger.info(
                f"[Analyzer] Pinned Groq key […{key_suffix}] unavailable for {contact_name} "
                f"(poll {no_key_polls}, cycle {cycles + 1}/{_MAX_POOL_CYCLES}) "
                f"— waiting {wait_s:.1f}s"
            )
            if no_key_polls >= _MAX_NO_KEY_POLLS:
                msg = (
                    f"Could not score {contact_name}: pinned key […{key_suffix}] "
                    f"did not become available after {no_key_polls} waits"
                )
                logger.error(f"[Analyzer] {msg}")
                return _empty_result(msg, contact_name)
            time.sleep(wait_s)
            continue

        no_key_polls = 0
        cycles += 1
        key_id, api_key = reservation
        pk = _pool._groq_by_key.get(api_key) or pinned_key

        try:
            # ── Token-bucket pre-check for pinned key ────────────────────
            _rl_allowed, _rl_retry = _rl.check(
                groq_key_bucket(api_key),
                capacity=_GROQ_BUCKET_CAPACITY,
                rate=_GROQ_BUCKET_RATE,
            )
            if not _rl_allowed:
                # Pinned key bucket empty — treat as a rate-limit hit so the
                # existing fallback logic (pinned_rate_limits counter) takes over.
                _db_release_groq_key(key_id)
                logger.info(
                    f"[RateLimit] Pinned key […{key_suffix}] bucket empty for {contact_name} "
                    f"— deferring to fallback logic (retry_after={_rl_retry:.1f}s)"
                )
                raise ProviderRateLimitError(retry_after=_rl_retry)

            with _GROQ_CALL_SEMAPHORE:
                raw = pk.provider.generate(
                    system_prompt=system_prompt,
                    user_content=user_content,
                    max_tokens=1200,
                    temperature=0.1,
                )
            _db_release_groq_key(key_id)
            return _finalize_result(raw, pk, contact_name)
        except ProviderQuotaExhaustedError:
            _pool.mark_quota_exhausted(pk)
            _db_cooldown_groq_key(key_id, 24 * 3600)
            continue
        except ProviderRateLimitError as e:
            cooldown = (e.retry_after or _DEFAULT_COOLDOWN)
            _db_cooldown_groq_key(key_id, cooldown)
            _pool.mark_rate_limited(pk, e.retry_after)
            pinned_rate_limits += 1
            logger.warning(
                f"[Analyzer] Pinned Groq key […{key_suffix}] rate-limited for {contact_name} "
                f"(cycle {cycles}/{_MAX_POOL_CYCLES})"
            )
            if pinned_rate_limits >= _PINNED_FALLBACK_AFTER_429S:
                if strict_assignment:
                    # In strict mode, wait once for the key to recover, then fall
                    # back to the shared pool if it's still exhausted.
                    wait_s = (e.retry_after or _DEFAULT_COOLDOWN)
                    wait_s = min(max(wait_s, 10), 120)  # clamp 10-120s
                    logger.warning(
                        f"[Analyzer] Pinned Groq key […{key_suffix}] rate-limited for {contact_name} "
                        f"— strict mode: waiting {wait_s:.0f}s for cooldown (429 #{pinned_rate_limits})"
                    )
                    time.sleep(wait_s)
                    # Don't reset pinned_rate_limits — allow fallback after next 429
                    continue
                logger.warning(
                    f"[Analyzer] Pinned Groq fallback engaged for {contact_name} "
                    f"after {pinned_rate_limits} consecutive 429s on […{key_suffix}]"
                )
                return _run_with_groq_pool(
                    user_content=user_content,
                    contact_name=contact_name,
                    system_prompt=system_prompt,
                    pinned_key=None,
                )
            continue
        except ProviderPayloadTooLargeError:
            _db_release_groq_key(key_id)
            logger.warning(
                f"[Analyzer] Pinned Groq request too large for {contact_name} "
                f"on key [...{key_suffix}] - escalating to caller"
            )
            raise
        except json.JSONDecodeError as e:
            logger.error(
                f"[Analyzer] JSON parse error for {contact_name} (pinned groq): {e}\nRaw: {raw[:300]}"
            )
            _db_cooldown_groq_key(key_id, 5.0)
            continue
        except Exception as e:
            logger.error(f"[Analyzer] Pinned Groq failed for {contact_name}: {e}")
            _db_cooldown_groq_key(key_id, 5.0)
            continue

    msg = f"Could not score {contact_name} after {_MAX_POOL_CYCLES} pinned-key cycles"
    logger.error(f"[Analyzer] {msg}")
    return _empty_result(msg, contact_name)


_PILLAR_FLAG_RE = re.compile(r"did(?:n'?t| not) gather\s+([a-z _-]+?)\s+pillar", re.I)
# Flags that are hallucinations — behaviours the prompt explicitly allows
# Two-condition check: flag mentions "referral" AND a dollar amount → hallucination
# (The $1k referral close is always correct scripted behaviour — never a real flag)
_REFERRAL_RE = re.compile(r"referral", re.I)
_DOLLAR_RE    = re.compile(r"\$[\d]", re.I)

# ── Shared guard imports (authoritative source: ai.prefilter._guards) ────────
# All deterministic guard logic lives in _guards.py and is shared with Tier 4.
from ai.prefilter._guards import (
    WHITELIST_FLAG_OUTPUTS as _WHITELIST_FLAG_OUTPUTS,
    _canon_flag_text,
    normalize_red_flags as _normalize_red_flags,
    agent_continued_after_opt_out as _agent_continued_after_opt_out,
    agent_continued_pitch_after_wn as _agent_continued_pitch_after_wn,
    last_message_from_contact as _last_message_from_contact,
    agent_replied_after_first_soft_no as _agent_replied_after_first_soft_no,
    apply_label_guards as _apply_label_guards,
)

_WHITELIST_CANON = {_canon_flag_text(x): x for x in _WHITELIST_FLAG_OUTPUTS}




def _finalize_result(raw: str, pk: "PooledKey", contact_name: str) -> dict:
    result = json.loads(raw)
    result["red_flags"] = _normalize_red_flags(result.get("red_flags") or [])
    result["model_used"] = pk.provider.model_name
    result["contact_name"] = contact_name
    _pool.mark_success(pk)
    logger.debug(
        f"[Analyzer] {contact_name} scored via "
        f"{pk.provider_type}/{pk.provider.model_name}"
    )
    return result


# ── Batch analysis ────────────────────────────────────────────────────────────

def analyze_batch(
    batch: list[dict],
    agent_name: str,
    *,
    model: str | None = None,
    funnel_tier: str | None = None,
    guidelines: str | None = None,
    pinned_key: "PooledKey | None" = None,
) -> list[dict]:
    """
    Analyze multiple conversations in a single API call.

    Each item in `batch` must have:
      - "parsed_messages": list[dict]
      - "contact_name": str
      - "assigned_labels": list[str] | None

    `funnel_tier` and `guidelines` are per-account overrides injected into the
    system prompt (same for every conversation in this batch — all conversations
    in a batch come from the same account).

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
            sections.append(
                f"────── CONVERSATION {i}: {contact} ──────\n(No messages)\n"
            )
            continue

        # 30-day rolling window: audit only recent messages
        parsed = filter_recent_messages(parsed)

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

    system_prompt = get_system_prompt(
        batch=True, funnel_tier=funnel_tier, guidelines=guidelines
    )

    try:
        if pinned_key is not None and pinned_key.provider_type == "groq":
            return _run_batch_with_pinned_groq_key(
                user_content, contact_names, len(batch), system_prompt, pinned_key
            )
        return _run_batch_with_groq_pool(
            user_content, contact_names, len(batch), system_prompt
        )
    except ProviderPayloadTooLargeError:
        logger.warning(
            f"[Analyzer] Batch payload too large for {agent_name} — falling back to per-conversation scoring"
        )
        return [
            analyze_conversation(
                convo.get("parsed_messages") or [],
                agent_name,
                convo.get("contact_name") or "Contact",
                assigned_labels=convo.get("assigned_labels") or [],
                model=model,
                funnel_tier=funnel_tier,
                guidelines=guidelines,
                pinned_key=pinned_key,
                conversation_id=convo.get("conversation_id"),
                db_pool=None,
            )
            for convo in batch
        ]


def _run_batch_with_groq_pool(
    user_content: str,
    contact_names: list[str],
    batch_size: int,
    system_prompt: str,
) -> list[dict]:
    _pool.ensure_loaded()
    raw = ""
    tried_ids: set[int] = set()
    cycles = 0
    no_key_polls = 0

    while cycles < _MAX_POOL_CYCLES:
        reservation = _db_reserve_groq_key()
        if reservation is None:
            wait_s = _db_seconds_until_groq_key_available()
            wait_s = min(max(wait_s, _MIN_NO_KEY_WAIT), _MAX_NO_KEY_WAIT)
            no_key_polls += 1
            logger.info(
                f"[Analyzer] No Groq key available for batch "
                f"(poll {no_key_polls}, cycle {cycles + 1}/{_MAX_POOL_CYCLES}) "
                f"— waiting {wait_s:.1f}s"
            )
            if no_key_polls >= _MAX_NO_KEY_POLLS:
                msg = (
                    f"Batch could not be scored: no Groq key became available "
                    f"after {no_key_polls} waits"
                )
                logger.error(f"[Analyzer] {msg}")
                return [_empty_result(msg, c) for c in contact_names]
            time.sleep(wait_s)
            continue

        no_key_polls = 0
        cycles += 1
        key_id, api_key = reservation
        if key_id in tried_ids:
            _db_release_groq_key(key_id)
            time.sleep(0.5)
            continue

        pk = _pool._groq_by_key.get(api_key)
        if pk is None:
            logger.warning(f"[Analyzer] Reserved batch key id={key_id} not in local pool — releasing")
            _db_release_groq_key(key_id)
            tried_ids.add(key_id)
            continue

        try:
            raw = pk.provider.generate(
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=1200 * batch_size,
                temperature=0.1,
            )
            _db_release_groq_key(key_id)
            return _finalize_batch_results(raw, pk, contact_names, batch_size)
        except ProviderQuotaExhaustedError:
            _pool.mark_quota_exhausted(pk)
            _db_cooldown_groq_key(key_id, 24 * 3600)
            tried_ids.add(key_id)
            continue
        except ProviderRateLimitError as e:
            cooldown = (e.retry_after or _DEFAULT_COOLDOWN)
            _db_cooldown_groq_key(key_id, cooldown)
            _pool.mark_rate_limited(pk, e.retry_after)
            tried_ids.add(key_id)
            continue
        except ProviderPayloadTooLargeError:
            _db_release_groq_key(key_id)
            logger.warning("[Analyzer] Groq batch request too large - escalating to caller")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"[Analyzer] Batch JSON parse (groq): {e}\nRaw: {raw[:500]}")
            _db_cooldown_groq_key(key_id, 5.0)
            _pool.mark_rate_limited(pk, 5.0)
            tried_ids.add(key_id)
            continue
        except Exception as e:
            logger.error(f"[Analyzer] Batch Groq failed: {e}")
            _db_cooldown_groq_key(key_id, 5.0)
            _pool.mark_rate_limited(pk, 5.0)
            tried_ids.add(key_id)
            continue

    msg = f"Batch could not be scored after {_MAX_POOL_CYCLES} Groq pool cycles"
    return [_empty_result(msg, c) for c in contact_names]


def _run_batch_with_pinned_groq_key(
    user_content: str,
    contact_names: list[str],
    batch_size: int,
    system_prompt: str,
    pinned_key: "PooledKey",
) -> list[dict]:
    _pool.ensure_loaded()
    strict_assignment = os.getenv("GROQ_ASSIGNMENT_STRICT", "").strip() == "1"
    raw = ""
    cycles = 0
    no_key_polls = 0
    pinned_rate_limits = 0
    key_suffix = pinned_key.key[-6:]

    while cycles < _MAX_POOL_CYCLES:
        reservation = _db_reserve_specific_groq_key(pinned_key.key)
        if reservation is None:
            wait_s = _db_seconds_until_specific_groq_key_available(pinned_key.key)
            wait_s = min(max(wait_s, _MIN_NO_KEY_WAIT), _MAX_NO_KEY_WAIT)
            no_key_polls += 1
            logger.info(
                f"[Analyzer] No pinned Groq key available for batch […{key_suffix}] "
                f"(poll {no_key_polls}, cycle {cycles + 1}/{_MAX_POOL_CYCLES}) "
                f"— waiting {wait_s:.1f}s"
            )
            if no_key_polls >= _MAX_NO_KEY_POLLS:
                msg = (
                    f"Batch could not be scored: pinned key […{key_suffix}] "
                    f"did not become available after {no_key_polls} waits"
                )
                logger.error(f"[Analyzer] {msg}")
                return [_empty_result(msg, c) for c in contact_names]
            time.sleep(wait_s)
            continue

        no_key_polls = 0
        cycles += 1
        key_id, api_key = reservation
        pk = _pool._groq_by_key.get(api_key) or pinned_key

        try:
            raw = pk.provider.generate(
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=1200 * batch_size,
                temperature=0.1,
            )
            _db_release_groq_key(key_id)
            return _finalize_batch_results(raw, pk, contact_names, batch_size)
        except ProviderQuotaExhaustedError:
            _pool.mark_quota_exhausted(pk)
            _db_cooldown_groq_key(key_id, 24 * 3600)
            continue
        except ProviderRateLimitError as e:
            cooldown = (e.retry_after or _DEFAULT_COOLDOWN)
            _db_cooldown_groq_key(key_id, cooldown)
            _pool.mark_rate_limited(pk, e.retry_after)
            pinned_rate_limits += 1
            if pinned_rate_limits >= _PINNED_FALLBACK_AFTER_429S:
                if strict_assignment:
                    # Strict mode: wait once for the key to recover, then fall
                    # back to the shared pool if it's still exhausted.
                    wait_s = (e.retry_after or _DEFAULT_COOLDOWN)
                    wait_s = min(max(wait_s, 10), 120)  # clamp 10-120s
                    logger.warning(
                        f"[Analyzer] Batch pinned key […{key_suffix}] rate-limited "
                        f"— strict mode: waiting {wait_s:.0f}s for cooldown (429 #{pinned_rate_limits})"
                    )
                    time.sleep(wait_s)
                    # Don't reset pinned_rate_limits — allow fallback after next 429
                    continue
                logger.warning(
                    f"[Analyzer] Batch pinned fallback engaged for key […{key_suffix}] "
                    f"after {pinned_rate_limits} consecutive 429s"
                )
                return _run_batch_with_groq_pool(
                    user_content=user_content,
                    contact_names=contact_names,
                    batch_size=batch_size,
                    system_prompt=system_prompt,
                )
            continue
        except ProviderPayloadTooLargeError:
            _db_release_groq_key(key_id)
            logger.warning(
                f"[Analyzer] Pinned Groq batch request too large on key [...{key_suffix}] - escalating to caller"
            )
            raise
        except json.JSONDecodeError as e:
            logger.error(f"[Analyzer] Batch JSON parse (pinned groq): {e}\nRaw: {raw[:500]}")
            _db_cooldown_groq_key(key_id, 5.0)
            _pool.mark_rate_limited(pk, 5.0)
            continue
        except Exception as e:
            logger.error(f"[Analyzer] Batch pinned Groq failed: {e}")
            _db_cooldown_groq_key(key_id, 5.0)
            _pool.mark_rate_limited(pk, 5.0)
            continue

    msg = f"Batch could not be scored after {_MAX_POOL_CYCLES} pinned-key cycles"
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
        r["red_flags"] = _normalize_red_flags(r.get("red_flags") or [])
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


# ── ML-only fallback ──────────────────────────────────────────────────────────

def _ml_only_fallback(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    assigned_labels: list[str] | None,
) -> dict:
    """Groq-free terminal result for ML-only mode. Runs the deterministic Tier 4
    generator directly; if that fails, returns an empty (skipped) result. Never
    calls Groq."""
    try:
        from ai.prefilter import tier4_flag_generator
        result = tier4_flag_generator.generate(
            messages, agent_name, contact_name, assigned_labels=assigned_labels or [],
        )
        if isinstance(result, dict):
            result.setdefault("model_used", "prefilter_t4")
            result.setdefault("contact_name", contact_name)
            return result
    except Exception as e:
        logger.warning(f"[Analyzer] ML-only T4 fallback failed for {contact_name}: {e}")
    return _empty_result("ML-only mode: no deterministic result available", contact_name)


# ── Empty result ──────────────────────────────────────────────────────────────

def _empty_result(reason: str, contact_name: str = "Contact") -> dict:
    return {
        "compliance_score": None,
        "sentiment_score": None,
        "professionalism_score": None,
        "script_adherence_score": None,
        "red_flags": [],
        "summary": f"Analysis skipped: {reason}",
        "model_used": None,
        "contact_name": contact_name,
        "error": reason,
    }
