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
    allowed, retry_after = rl.check("groq_abc123", capacity=5, rate=0.5)
    if not allowed:
        raise SomeRateLimitError(retry_after=retry_after)

Bucket configs used across the codebase
----------------------------------------
  Groq free-tier key  : capacity=5,  rate=0.5   (5 burst, 1 req/2 s sustained)
  Groq paid-tier key  : capacity=20, rate=2.0
  /api/run-audit route: capacity=3,  rate=0.1   (3 burst, 1 req/10 s sustained)
  /api/ai/status      : capacity=10, rate=1.0   (relaxed — monitoring endpoint)
  /api/* default      : capacity=20, rate=2.0
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

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._global_rejected: int = 0

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
            buckets = {name: bucket.status() for name, bucket in self._buckets.items()}
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

def groq_key_bucket(api_key: str) -> str:
    """Anonymized bucket key for a Groq API key (last 8 chars only)."""
    return f"groq_{api_key[-8:]}"


def route_bucket(ip: str, route_prefix: str) -> str:
    """Bucket key for a dashboard route + client IP."""
    safe_route = route_prefix.replace("/", "_").strip("_")
    return f"route_{ip}_{safe_route}"
