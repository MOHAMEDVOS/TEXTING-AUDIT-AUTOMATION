"""Shared types for the prefilter pipeline."""
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class TierHit(IntEnum):
    """Which tier produced the result."""
    T1_PHRASE = 1
    T2_EMBEDDING = 2
    T3_CLASSIFIER = 3
    T4_GROQ_ESCALATED = 4


@dataclass
class PrefilterResult:
    """
    Result returned by run_prefilter.

    short_circuited=True  → caller must NOT call Groq, use predicted
    short_circuited=False → caller MUST call Groq; predicted is advisory only
    """
    tier_hit: TierHit
    short_circuited: bool
    confidence: float
    predicted: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_jsonable(self) -> dict:
        return {
            "tier_hit": int(self.tier_hit),
            "short_circuited": self.short_circuited,
            "confidence": self.confidence,
            "predicted": self.predicted,
            "reason": self.reason,
        }
