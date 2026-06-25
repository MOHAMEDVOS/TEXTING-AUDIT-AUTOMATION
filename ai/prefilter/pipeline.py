"""
Pipeline orchestrator for the 4-tier ML pre-filter.

Called by ai/analyzer.py before the Groq path. Returns a fully-formed
analyzer-style dict if a tier short-circuited, or None to escalate to Groq.

Tier stack:
  T1 — Phrase matching (deterministic, instant)
  T2 — FAISS kNN neighbor lookup (embedding-based)
  T3 — Multi-label classifier (predicts flags + scores)
  T4 — Deterministic flag generator (terminal tier, zero API calls)

When PREFILTER_T4_LIVE is True (default), T4 is the terminal tier and
Groq is NEVER called.  When False, conversations that pass all tiers
still escalate to Groq (pre-elimination behavior).

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
from . import tier1_phrases_v2 as tier1_phrases
# Tiers 2, 3, 4 are imported lazily — T2/T3 pull in heavy ML deps (sentence-transformers,
# faiss, sklearn) and we don't want to pay the import cost when prefilter is disabled.
# T4 is lightweight but kept lazy for consistency.

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
    funnel_tier: str = "NF",
    assigned_labels: list[str] | None = None,
    db_pool=None,
) -> Optional[dict]:
    """
    Main entry point. Runs tiers in order, records the decision, returns
    a Groq-shaped result dict if a tier short-circuited (and we're not in
    shadow mode), otherwise None.

    tier_hit values:
      0 — pre-flight bypass (flag trigger or label-requires-AI)
      1 — Tier 1 phrase match
      2 — Tier 2 embedding kNN
      3 — Tier 3 classifier
      4 — Tier 4 deterministic flag generator (terminal)
      5 — escalated to Groq (T4 disabled, no tier confident)

    `db_pool` — optional asyncpg pool for recording the decision. The recording
    happens fire-and-forget; failure to record never blocks the pipeline.
    """
    if not settings.PREFILTER_ENABLED:
        return None

    if not messages:
        return None



    ft = (funnel_tier or "NF").upper().strip()
    if ft not in ("WF", "MF", "NF"):
        ft = "NF"

    started = time.perf_counter()
    decision = _run_tiers(messages, agent_name, contact_name, ft, assigned_labels=assigned_labels)
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
        if decision.tier_hit == 4 and not settings.PREFILTER_T4_LIVE:
            return None
        logger.info(
            f"[Prefilter] {contact_name}: SHORT-CIRCUIT at tier {decision.tier_hit} "
            f"(conf={decision.confidence}, {decision.elapsed_ms:.1f}ms) — "
            f"skipping Groq"
        )
        # ── Semantic learner: capture high-quality short-circuits ──────
        _try_semantic_capture(conversation_id, messages, decision.result)
        return decision.result

    # Decision was "escalate" (T1 suspicious, or all tiers passed + T4 off).
    # ML-only mode: never hand off to Groq — finalize with terminal T4 instead.
    if settings.PREFILTER_DISABLE_GROQ:
        terminal = _force_terminal_t4(messages, agent_name, contact_name, ft, assigned_labels)
        if terminal is not None:
            logger.info(
                f"[Prefilter] {contact_name}: ML-ONLY terminal T4 "
                f"(would have escalated at tier {decision.tier_hit}) — Groq disabled"
            )
            _try_semantic_capture(conversation_id, messages, terminal.result)
            return terminal.result
        logger.warning(
            f"[Prefilter] {contact_name}: ML-only terminal T4 produced no result — "
            f"returning None (analyzer will apply a Groq-free default)"
        )

    return None


def _force_terminal_t4(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    funnel_tier: str,
    assigned_labels: list[str] | None,
) -> Optional[PrefilterResult]:
    """Run the deterministic Tier 4 generator as a guaranteed terminal result
    (used by ML-only mode so a would-be Groq escalation stays local). Never raises."""
    try:
        from . import tier4_flag_generator
        t4_result = tier4_flag_generator.generate(
            messages, agent_name, contact_name,
            assigned_labels=assigned_labels, funnel_tier=funnel_tier,
        )
        return PrefilterResult(
            tier_hit=4,
            decision="short_circuit",
            confidence=0.60,
            result=t4_result,
            notes="t4-terminal-ml-only",
        )
    except Exception as e:
        logger.warning(f"[Prefilter] terminal T4 failed for {contact_name}: {e}")
        return None


def _try_semantic_capture(
    conversation_id: int | None,
    messages: list[dict],
    result: dict | None,
) -> None:
    """Best-effort capture for the semantic auto-learning queue. Never raises."""
    if not result or not conversation_id:
        return
    try:
        from .semantic_learner import capture_candidate
        capture_candidate(
            conversation_id=conversation_id,
            messages=messages,
            result=result,
        )
    except Exception as e:
        logger.debug(f"[Prefilter] Semantic capture failed (non-fatal): {e}")


def _run_tiers(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    funnel_tier: str = "NF",
    assigned_labels: list[str] | None = None,
) -> PrefilterResult:
    """Run tiers in order. First confident short-circuit wins."""
    # ── Tier 1: exact phrases (funnel-aware) ───────────────────────
    t1 = tier1_phrases.evaluate(messages, funnel_tier, agent_name, contact_name, assigned_labels=assigned_labels)
    if t1 is not None and t1.decision == "short_circuit":
        return t1
    # If Tier 1 explicitly escalated (suspicious phrase detected), respect it.
    if t1 is not None and t1.decision == "escalate":
        return t1

    # ── Tier 2: embedding kNN (funnel-aware) ───────────────────────
    if settings.PREFILTER_T2_LIVE or settings.PREFILTER_SHADOW_MODE:
        try:
            from . import tier2_embedding
            t2 = tier2_embedding.evaluate(messages, agent_name, contact_name, funnel_tier=funnel_tier, assigned_labels=assigned_labels)
            if t2 is not None and t2.decision == "short_circuit":
                return t2
        except Exception as e:
            logger.warning(f"[Prefilter] Tier 2 failed for {contact_name}: {e}")

    # ── Tier 3: classifier (funnel-aware) ──────────────────────────
    if settings.PREFILTER_T3_LIVE or settings.PREFILTER_SHADOW_MODE:
        try:
            from . import tier3_classifier
            t3 = tier3_classifier.evaluate(messages, agent_name, contact_name, funnel_tier=funnel_tier, assigned_labels=assigned_labels)
            if t3 is not None and t3.decision == "short_circuit":
                return t3
        except Exception as e:
            logger.warning(f"[Prefilter] Tier 3 failed for {contact_name}: {e}")

    # ── Tier 4: deterministic flag generator (terminal tier) ────────
    if settings.PREFILTER_T4_LIVE or settings.PREFILTER_SHADOW_MODE:
        try:
            from . import tier4_flag_generator
            t4_result = tier4_flag_generator.generate(
                messages, agent_name, contact_name,
                assigned_labels=assigned_labels,
                funnel_tier=funnel_tier,
            )
            return PrefilterResult(
                tier_hit=4,
                decision="short_circuit",
                confidence=0.65,  # conservative baseline for deterministic
                result=t4_result,
                notes="t4-deterministic",
            )
        except Exception as e:
            logger.warning(f"[Prefilter] Tier 4 failed for {contact_name}: {e}")

    # No tier was confident + T4 disabled → escalate to Groq.
    return PrefilterResult(tier_hit=5, decision="escalate", notes="all tiers passed, t4 disabled")


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
