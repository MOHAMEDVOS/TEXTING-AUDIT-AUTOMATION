"""
Semantic Learner — Auto-capture + promote loop for self-learning pipeline.

After each scoring run, conversations that are:
  1. Scored with high compliance (>= SEMANTIC_MIN_SCORE)
  2. Semantically novel (top kNN similarity <= SEMANTIC_MAX_SIMILARITY)
  3. Flagless (zero red flags)
get inserted into the `semantic_candidates` table.

A separate `auto_promote()` call (invoked by the dream_worker or CLI)
checks the queue, promotes clean candidates into the FAISS index, and
triggers an automatic index rebuild + T3 retrain when thresholds are met.

This creates a continuous improvement loop where the ML pipeline learns
from every high-quality conversation it processes — with no human
intervention required.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from config.settings import (
    DATABASE_URL,
    SEMANTIC_LEARNING_ENABLED,
    SEMANTIC_MIN_SCORE,
    SEMANTIC_MAX_SIMILARITY,
    SEMANTIC_MIN_PROMOTE,
    SEMANTIC_MAX_PER_RUN,
)

logger = logging.getLogger(__name__)


def capture_candidate(
    conversation_id: int | None,
    messages: list[dict],
    result: dict,
    top_similarity: float | None = None,
    nearest_conversation_id: int | None = None,
    dsn: str | None = None,
) -> bool:
    """
    Evaluate a scored conversation for auto-capture.

    Returns True if the conversation was inserted into semantic_candidates.
    Returns False (with no error) if it doesn't qualify or learning is disabled.
    """
    if not SEMANTIC_LEARNING_ENABLED:
        return False

    if conversation_id is None:
        return False

    # ── Quality gate: must be high-scoring and flag-free ──────────────
    compliance = result.get("compliance_score")
    sentiment = result.get("sentiment_score")
    professionalism = result.get("professionalism_score")
    script = result.get("script_adherence_score")

    scores = [s for s in [compliance, sentiment, professionalism, script] if s is not None]
    if not scores:
        return False

    avg_score = sum(scores) / len(scores)
    if avg_score < SEMANTIC_MIN_SCORE:
        return False

    # Must be flagless
    flags = result.get("red_flags") or []
    if flags:
        return False

    # ── Novelty gate: must be semantically distinct ──────────────────
    if top_similarity is not None and top_similarity > SEMANTIC_MAX_SIMILARITY:
        return False

    # ── Build embedding hash from message content ────────────────────
    text_blob = " ".join(
        (m.get("message") or m.get("body") or "") for m in messages
    ).strip()
    if len(text_blob) < 20:
        return False

    embedding_hash = hashlib.sha256(text_blob.encode("utf-8")).hexdigest()[:32]

    # ── Extract distinctive phrases for later review ─────────────────
    # Take first 3 contact messages as representative
    contact_msgs = [
        m for m in messages
        if (m.get("sender") or "").strip().lower() in ("contact", "lead")
    ]
    phrases = [
        (m.get("message") or m.get("body") or "").strip()[:120]
        for m in contact_msgs[:3]
        if len((m.get("message") or m.get("body") or "").strip()) > 5
    ]

    # ── Insert into semantic_candidates ──────────────────────────────
    try:
        dsn = dsn or DATABASE_URL
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO semantic_candidates
                       (conversation_id, embedding_hash, top_similarity,
                        nearest_conversation_id, compliance_score, sentiment_score,
                        professionalism_score, script_adherence_score,
                        distinctive_phrases, is_clean, capture_reason)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, 'novelty')
                       ON CONFLICT (embedding_hash) DO NOTHING""",
                    (
                        conversation_id,
                        embedding_hash,
                        top_similarity,
                        nearest_conversation_id,
                        compliance,
                        sentiment,
                        professionalism,
                        script,
                        json.dumps(phrases),
                    ),
                )
            conn.commit()

        logger.debug(
            f"[SemanticLearner] Captured candidate conv_id={conversation_id} "
            f"(avg={avg_score:.1f}, sim={top_similarity or 0:.3f})"
        )
        return True

    except Exception as e:
        logger.warning(f"[SemanticLearner] Capture failed for conv_id={conversation_id}: {e}")
        return False


def auto_promote(dsn: str | None = None, dry_run: bool = False) -> dict:
    """
    Promote qualifying candidates into training data and trigger rebuild.

    Selection criteria:
      - is_clean = TRUE
      - promoted = FALSE
      - rejected = FALSE
      - Created at least 24h ago (stabilization window)

    Returns summary dict:
    {
        "candidates_reviewed": int,
        "promoted": int,
        "rebuild_triggered": bool,
        "reason": str | None,
    }
    """
    if not SEMANTIC_LEARNING_ENABLED:
        return {
            "candidates_reviewed": 0,
            "promoted": 0,
            "rebuild_triggered": False,
            "reason": "Semantic learning disabled",
        }

    try:
        dsn = dsn or DATABASE_URL
        with psycopg2.connect(dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Find eligible candidates (24h stabilization window)
                cur.execute(
                    """SELECT id, conversation_id, compliance_score, top_similarity
                       FROM semantic_candidates
                       WHERE is_clean = TRUE
                         AND promoted = FALSE
                         AND rejected = FALSE
                         AND created_at < NOW() - INTERVAL '24 hours'
                       ORDER BY compliance_score DESC
                       LIMIT %s""",
                    (SEMANTIC_MAX_PER_RUN,),
                )
                candidates = cur.fetchall()

        candidates_reviewed = len(candidates)

        if candidates_reviewed < SEMANTIC_MIN_PROMOTE:
            return {
                "candidates_reviewed": candidates_reviewed,
                "promoted": 0,
                "rebuild_triggered": False,
                "reason": f"Not enough candidates ({candidates_reviewed} < {SEMANTIC_MIN_PROMOTE})",
            }

        if dry_run:
            return {
                "candidates_reviewed": candidates_reviewed,
                "promoted": 0,
                "rebuild_triggered": False,
                "reason": "Dry run — would promote {candidates_reviewed} candidates",
            }

        # Mark candidates as promoted
        promoted_ids = [c["id"] for c in candidates]
        now_iso = datetime.now(timezone.utc)

        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE semantic_candidates
                       SET promoted = TRUE, promoted_at = %s
                       WHERE id = ANY(%s)""",
                    (now_iso, promoted_ids),
                )
            conn.commit()

        logger.info(
            f"[SemanticLearner] Promoted {len(promoted_ids)} candidates"
        )

        # Trigger index rebuild
        rebuild_ok = _trigger_rebuild(dsn)

        return {
            "candidates_reviewed": candidates_reviewed,
            "promoted": len(promoted_ids),
            "rebuild_triggered": rebuild_ok,
            "reason": None,
        }

    except Exception as e:
        logger.error(f"[SemanticLearner] auto_promote failed: {e}")
        return {
            "candidates_reviewed": 0,
            "promoted": 0,
            "rebuild_triggered": False,
            "reason": str(e),
        }


def _trigger_rebuild(dsn: str) -> bool:
    """
    Trigger FAISS index rebuild + T3 retrain.

    Imports are deferred to avoid circular imports at module load time.
    Returns True if rebuild completed successfully.
    """
    try:
        from ai.prefilter.index_builder import main as rebuild_index
        from ai.prefilter.train import main as retrain_classifier

        logger.info("[SemanticLearner] Rebuilding FAISS index...")
        rebuild_index()

        logger.info("[SemanticLearner] Retraining T3 classifier...")
        retrain_classifier()

        logger.info("[SemanticLearner] ✓ Rebuild + retrain complete")
        return True

    except Exception as e:
        logger.error(f"[SemanticLearner] Rebuild failed: {e}")
        return False


def get_queue_stats(dsn: str | None = None) -> dict:
    """Get current queue statistics for monitoring."""
    try:
        dsn = dsn or DATABASE_URL
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT
                         COUNT(*) FILTER (WHERE NOT promoted AND NOT rejected) AS pending,
                         COUNT(*) FILTER (WHERE promoted) AS promoted,
                         COUNT(*) FILTER (WHERE rejected) AS rejected,
                         COUNT(*) AS total,
                         AVG(compliance_score) FILTER (WHERE NOT promoted AND NOT rejected) AS avg_score,
                         AVG(top_similarity) FILTER (WHERE NOT promoted AND NOT rejected) AS avg_similarity
                       FROM semantic_candidates"""
                )
                row = cur.fetchone()

        return {
            "pending": row[0] or 0,
            "promoted": row[1] or 0,
            "rejected": row[2] or 0,
            "total": row[3] or 0,
            "avg_pending_score": round(row[4], 1) if row[4] else None,
            "avg_pending_similarity": round(row[5], 3) if row[5] else None,
        }

    except Exception as e:
        logger.warning(f"[SemanticLearner] Could not get queue stats: {e}")
        return {"pending": 0, "promoted": 0, "rejected": 0, "total": 0}
