"""
Internal pipeline result dataclass.

Separate module to break the circular import:
  pipeline.py → tier1_phrases.py → pipeline.py (was circular)
Now:
  pipeline.py → tier1_phrases.py → _pipeline_types.py  (acyclic)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PipelineResult:
    """
    Internal result used within the pipeline tiers.

    Not exported publicly — external callers use types.PrefilterResult.
    """
    tier_hit: int                           # 1, 2, 3 (short-circuit) or 4 (escalated)
    decision: str                           # "short_circuit" | "escalate"
    confidence: Optional[float] = None
    result: Optional[dict] = None
    notes: str = ""
    elapsed_ms: float = 0.0
    predicted_scores: Optional[dict] = field(default=None)
