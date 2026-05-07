"""
Pipeline orchestrator for the 4-tier ML pre-filter.

Called by ai/analyzer.py before the Groq path. Returns a fully-formed
analyzer-style dict if a tier short-circuited, or None to escalate to Groq.

In shadow mode, ALL tiers run and record decisions to prefilter_decisions,
but the result is always None so Groq still produces the final score.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from config import settings

from ._pipeline_types import PipelineResult
from . import tier1_phrases
# Tier 2 and 3 are imported lazily — they pull in heavy ML deps (sentence-transformers,
# faiss, xgboost) and we don't want to pay the import cost when prefilter is disabled.

logger = logging.getLogger(__name__)

# Alias for backwards compatibility (existing code that does
# `from .pipeline import PrefilterResult` keeps working).
PrefilterResult = PipelineResult


def run_prefilter(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    conversation_id: Optional[int] = None,
    *,
    db_pool=None,
) -> Optional[dict]:
    """
    Main entry point. Runs tiers in order, records the decision, returns
    a Groq-shaped result dict if a tier short-circuited (and we're not in
    shadow mode), otherwise None.

    tier_hit values:
      0 — pre-flight bypass (flag trigger detected, sent straight to Groq)
      1 — Tier 1 phrase match
      2 — Tier 2 embedding kNN
      3 — Tier 3 classifier
      4 — escalated to Groq (no tier was confident)

    `db_pool` — optional asyncpg pool for recording the decision. The recording
    happens fire-and-forget; failure to record never blocks the pipeline.
    """
    if not settings.PREFILTER_ENABLED:
        return None

    if not messages:
        return None

    # ── Pre-flight: flag trigger? Skip ML entirely → Groq. ────────────────
    if settings.PREFILTER_FLAG_ROUTING_ENABLED:
        from . import flag_triggers
        triggered, pattern = flag_triggers.has_flag_trigger(messages, agent_name)
        if triggered:
            logger.info(
                f"[Prefilter] {contact_name}: flag trigger '{pattern}' — "
                f"bypassing ML, escalating to Groq"
            )
            bypass_decision = PrefilterResult(
                tier_hit=0,
                decision="escalate",
                confidence=1.0,
                notes=f"flag-trigger:{pattern}",
            )
            if conversation_id is not None and db_pool is not None:
                _record_decision_async(db_pool, conversation_id, bypass_decision)
            return None  # escalate to Groq

    started = time.perf_counter()
    decision = _run_tiers(messages, agent_name, contact_name)
    decision.elapsed_ms = (time.perf_counter() - started) * 1000.0

    # Record the decision (best-effort). We do this even in shadow mode —
    # that's the whole point of shadow mode.
    if conversation_id is not None and db_pool is not None:
        _record_decision_async(db_pool, conversation_id, decision)

    # In shadow mode, ALWAYS escalate so Groq still produces the truth.
    # The decision row will get its `groq_scores` filled in later by scorer.py.
    if settings.PREFILTER_SHADOW_MODE:
        logger.debug(
            f"[Prefilter] {contact_name}: tier {decision.tier_hit} "
            f"({decision.decision}) — SHADOW, escalating to Groq"
        )
        return None

    # Live mode: honor the per-tier kill switches.
    if decision.decision == "short_circuit":
        if decision.tier_hit == 1 and not settings.PREFILTER_T1_LIVE:
            return None
        if decision.tier_hit == 2 and not settings.PREFILTER_T2_LIVE:
            return None
        if decision.tier_hit == 3 and not settings.PREFILTER_T3_LIVE:
            return None
        logger.info(
            f"[Prefilter] {contact_name}: SHORT-CIRCUIT at tier {decision.tier_hit} "
            f"(conf={decision.confidence}, {decision.elapsed_ms:.1f}ms) — "
            f"skipping Groq"
        )
        return decision.result

    return None


def _run_tiers(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
) -> PrefilterResult:
    """Run tiers in order. First confident short-circuit wins."""
    # ── Tier 1: exact phrases ──────────────────────────────────────
    t1 = tier1_phrases.evaluate(messages, agent_name, contact_name)
    if t1 is not None and t1.decision == "short_circuit":
        return t1
    # If Tier 1 explicitly escalated (suspicious phrase detected), respect it.
    if t1 is not None and t1.decision == "escalate":
        return t1

    # ── Tier 2: embedding kNN ──────────────────────────────────────
    if settings.PREFILTER_T2_LIVE or settings.PREFILTER_SHADOW_MODE:
        try:
            from . import tier2_embedding
            t2 = tier2_embedding.evaluate(messages, agent_name, contact_name)
            if t2 is not None and t2.decision == "short_circuit":
                return t2
        except Exception as e:
            logger.warning(f"[Prefilter] Tier 2 failed for {contact_name}: {e}")

    # ── Tier 3: classifier ─────────────────────────────────────────
    if settings.PREFILTER_T3_LIVE or settings.PREFILTER_SHADOW_MODE:
        try:
            from . import tier3_classifier
            t3 = tier3_classifier.evaluate(messages, agent_name, contact_name)
            if t3 is not None and t3.decision == "short_circuit":
                return t3
        except Exception as e:
            logger.warning(f"[Prefilter] Tier 3 failed for {contact_name}: {e}")

    # No tier was confident → escalate to Groq.
    return PrefilterResult(tier_hit=4, decision="escalate", notes="all tiers passed")


def _record_decision_async(db_pool, conversation_id: int, decision: PrefilterResult) -> None:
    """
    Fire-and-forget record into prefilter_decisions. Uses asyncio.create_task
    if we're inside a running loop; otherwise no-ops. Never raises.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Not in an async context — skip recording. The eval harness records
        # decisions through its own sync path.
        return

    async def _do_record():
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO prefilter_decisions
                        (conversation_id, tier_hit, decision, confidence,
                         predicted_scores, shadow_mode, notes)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                    """,
                    conversation_id,
                    decision.tier_hit,
                    decision.decision,
                    decision.confidence,
                    json.dumps(decision.predicted_scores) if decision.predicted_scores else None,
                    bool(settings.PREFILTER_SHADOW_MODE),
                    decision.notes or None,
                )
        except Exception as e:
            logger.debug(f"[Prefilter] failed to record decision: {e}")

    loop.create_task(_do_record())
