"""
Token-bucket rate limiter — thread-safe, in-memory, zero-dependency.

Architecture (mirrors the diagram):
  REQUESTS ──► LIMITER (token bucket check)
                  │
           ┌──────┴──────┐
           │             │
        WITHIN         OVER LIMIT
        LIMIT          → 429 immediately (no queue, no wait)
           │
      USAGE STORE (rules applied)
           │
       API CALLS

Usage
-----
    from config.rate_limiter import get_rate_limiter

    rl = get_rate_limiter()
    allowed, retry_after = rl.check("route_key", capacity=5, rate=0.5)
    if not allowed:
        raise SomeRateLimitError(retry_after=retry_after)

Bucket configs used across the codebase
----------------------------------------
  /api/run-audit route: capacity=3,  rate=0.1   (3 burst, 1 req/10 s sustained)
  /api/* default       : capacity=20, rate=2.0
"""

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token Bucket
# ---------------------------------------------------------------------------

@dataclass
class TokenBucket:
    """
    Classic token-bucket implementation.

    Tokens accumulate at `rate` per second up to `capacity`.
    Each call to consume() removes `tokens` from the bucket.

    Returns (True, 0.0) when the request is allowed.
    Returns (False, retry_after) when the bucket is empty — instant reject,
    no sleeping, no queuing.
    """

    capacity: float          # maximum number of tokens
    rate: float              # tokens added per second (refill rate)

    # Internal state — not part of the constructor signature
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # Stats
    _allowed_count: int = field(default=0, init=False)
    _rejected_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    # ------------------------------------------------------------------

    def consume(self, tokens: float = 1.0) -> tuple[bool, float]:
        """
        Attempt to consume `tokens` from the bucket.

        Returns
        -------
        (True,  0.0)         — request is allowed, proceed.
        (False, retry_after) — bucket empty, reject with 429.
                               retry_after is seconds until 1 token refills.
        """
        with self._lock:
            # Refill tokens based on elapsed time
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                self._allowed_count += 1
                return True, 0.0

            # Bucket is empty — reject immediately
            retry_after = (tokens - self._tokens) / self.rate
            self._rejected_count += 1
            return False, round(retry_after, 2)

    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Snapshot of bucket state (for monitoring endpoints)."""
        with self._lock:
            return {
                "tokens_remaining": round(self._tokens, 2),
                "capacity": self.capacity,
                "rate_per_sec": self.rate,
                "fill_pct": round((self._tokens / self.capacity) * 100, 1),
                "allowed_total": self._allowed_count,
                "rejected_total": self._rejected_count,
            }


# ---------------------------------------------------------------------------
# RateLimiter — named collection of buckets
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Named registry of TokenBuckets.

    Buckets are created lazily on first check() call with a given key.
    The same capacity/rate must be passed consistently for each key, or
    the first call wins (subsequent capacity/rate args are ignored for
    that key after creation).
    """

    # Buckets idle longer than this are discarded. Without eviction the dict grew
    # without bound — one entry per (actor, route) forever (deep review F26).
    _IDLE_EVICT_SECONDS = 3600.0
    _EVICT_EVERY = 500  # checks between sweeps

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._global_rejected: int = 0
        self._checks_since_evict: int = 0

    def _evict_idle_locked(self) -> int:
        """Drop buckets untouched for _IDLE_EVICT_SECONDS. Caller must hold _lock.

        A full bucket that has been idle carries no state worth keeping — a fresh
        one starts full too — so eviction cannot let anyone exceed their limit.
        """
        cutoff = time.monotonic() - self._IDLE_EVICT_SECONDS
        stale = [
            k for k, b in self._buckets.items()
            if b._last_refill < cutoff and b._tokens >= b.capacity
        ]
        for k in stale:
            del self._buckets[k]
        if stale:
            logger.debug(f"[RateLimiter] Evicted {len(stale)} idle bucket(s)")
        return len(stale)

    # ------------------------------------------------------------------

    def check(self, key: str, capacity: float, rate: float) -> tuple[bool, float]:
        """
        Check (and consume from) the named bucket.

        Parameters
        ----------
        key      : unique string identifying this bucket
                   (e.g. "groq_abc123", "route_127.0.0.1_/api/run-audit")
        capacity : max burst tokens
        rate     : tokens refilled per second

        Returns
        -------
        (True,  0.0)         — allowed
        (False, retry_after) — rejected, retry after N seconds
        """
        # Lazy bucket creation (thread-safe)
        with self._lock:
            self._checks_since_evict += 1
            if self._checks_since_evict >= self._EVICT_EVERY:
                self._checks_since_evict = 0
                self._evict_idle_locked()
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(capacity=capacity, rate=rate)
                logger.debug(
                    f"[RateLimiter] Created bucket '{key}' "
                    f"(capacity={capacity}, rate={rate}/s)"
                )

        allowed, retry_after = self._buckets[key].consume()

        if not allowed:
            with self._lock:
                self._global_rejected += 1
            logger.info(
                f"[RateLimiter] REJECTED '{key}' — "
                f"retry_after={retry_after:.2f}s  "
                f"(total_rejected={self._global_rejected})"
            )

        return allowed, retry_after

    # ------------------------------------------------------------------

    def status(self) -> dict:
        """
        Full status snapshot — exposed via /api/rate-limit/status.

        Returns a dict of {bucket_key: {...bucket stats...}} plus a
        top-level summary.
        """
        with self._lock:
            self._evict_idle_locked()
            # Keys embed the actor (an email address, or ip:<addr>). Returning them
            # raw disclosed every user's identity/IP to any authenticated caller,
            # so they are masked here (deep review F26).
            buckets = {
                _mask_bucket_key(name): bucket.status()
                for name, bucket in self._buckets.items()
            }
            total_rejected = self._global_rejected

        return {
            "summary": {
                "total_buckets": len(buckets),
                "total_rejected_all_time": total_rejected,
            },
            "buckets": buckets,
        }

    # ------------------------------------------------------------------

    def reset(self, key: str) -> bool:
        """
        Reset a specific bucket back to full capacity.
        Useful for testing or manual admin resets.
        Returns True if the bucket existed and was reset.
        """
        with self._lock:
            if key in self._buckets:
                b = self._buckets[key]
                b._tokens = b.capacity
                b._last_refill = time.monotonic()
                logger.info(f"[RateLimiter] Bucket '{key}' manually reset to full")
                return True
        return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_rate_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Return the global RateLimiter singleton."""
    return _rate_limiter


# ---------------------------------------------------------------------------
# Bucket key helpers (keeps naming consistent across modules)
# ---------------------------------------------------------------------------

def _mask_bucket_key(key: str) -> str:
    """Hide the actor portion of a bucket key for monitoring output.

    Keys look like "route_<actor>_<route>" where actor is an email address or
    "ip:<addr>". Both identify a person, so neither belongs in a response any
    authenticated user can fetch (deep review F26).
    """
    if not key.startswith("route_"):
        return key
    rest = key[len("route_"):]
    parts = rest.split("_")
    if len(parts) < 2:
        return key
    actor, route = parts[0], "_".join(parts[1:])
    if "@" in actor:
        name, _, domain = actor.partition("@")
        actor = f"{name[:2]}***@{domain}"
    elif actor.startswith("ip:"):
        actor = "ip:***"
    else:
        actor = actor[:2] + "***"
    return f"route_{actor}_{route}"


def route_bucket(actor: str, route_prefix: str) -> str:
    """Bucket key for a dashboard route + actor (user email, or ip:<addr>)."""
    safe_route = route_prefix.replace("/", "_").strip("_")
    return f"route_{actor}_{safe_route}"
