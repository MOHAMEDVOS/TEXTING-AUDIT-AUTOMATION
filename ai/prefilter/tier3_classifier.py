"""
Tier 3 — Trained logistic regression + score regressor on embeddings.

How it works:
  1. Embed the incoming conversation with sentence-transformers (with [FT] prefix).
  2. Run through trained flag_clf to get P(red_flag).
  3. If P < flag_prob_threshold, predict the scores and short-circuit.
  4. Otherwise, return None to escalate.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import joblib

from config import settings
from . import embedder
from ._pipeline_types import PipelineResult as PrefilterResult

logger = logging.getLogger(__name__)

_classifier_bundle = None
_loaded = False
_load_failed = False


def _load_classifier() -> bool:
    """Lazily load the trained T3 classifier bundle. Returns True on success."""
    global _classifier_bundle, _loaded, _load_failed

    if _loaded:
        return _classifier_bundle is not None
    if _load_failed:
        return False

    classifier_path = Path(settings.PREFILTER_CLASSIFIER_PATH)
    if not classifier_path.exists():
        logger.info(
            f"[Prefilter T3] No classifier found at {classifier_path}. "
            f"Run `python -m ai.prefilter.train` to build it."
        )
        _load_failed = True
        return False

    try:
        _classifier_bundle = joblib.load(classifier_path)
        _loaded = True
        logger.info(
            f"[Prefilter T3] Loaded classifier: "
            f"{_classifier_bundle['n_train']} training examples, "
            f"dim={_classifier_bundle['feature_dim']}"
        )
        return True
    except Exception as e:
        logger.error(f"[Prefilter T3] Failed to load classifier: {e}")
        _load_failed = True
        return False


def evaluate(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    funnel_tier: str = "NF",
) -> Optional[PrefilterResult]:
    """
    Embed conversation and run through trained models.
    Return short-circuit + scores if confident, else None.

    funnel_tier: "WF" | "MF" | "NF" — prepended to the query text.
    """
    if not _load_classifier():
        return None

    ft = (funnel_tier or "NF").upper().strip()
    if ft not in ("WF", "MF", "NF"):
        ft = "NF"

    # Build funnel-aware text (matching index_builder.py and tier2_embedding.py)
    base_text = embedder.conversation_to_text(messages, agent_name)
    if not base_text.strip():
        return None
    text = f"[{ft}]\n{base_text}"

    vec = embedder.embed(text)
    if vec is None:
        return None

    query = np.asarray([vec], dtype=np.float32)

    flag_clf = _classifier_bundle["flag_clf"]
    score_reg = _classifier_bundle["score_reg"]

    # Predict flag probability
    flag_prob = float(flag_clf.predict_proba(query)[0, 1])

    # Threshold: if P(flag) >= 0.35, escalate (too risky)
    # Clean conversations cluster at ~0.30, flagged at ~0.63, so 0.35 is safe
    flag_prob_threshold = getattr(settings, "PREFILTER_T3_FLAG_PROB_THRESHOLD", 0.35)
    if flag_prob >= flag_prob_threshold:
        return PrefilterResult(
            tier_hit=3,
            decision="escalate",
            confidence=flag_prob,
            notes=f"flag_prob={flag_prob:.3f} >= {flag_prob_threshold}",
        )

    # Predict scores
    scores_pred = score_reg.predict(query)[0]
    avg_scores = {
        "compliance_score": float(scores_pred[0]),
        "sentiment_score": float(scores_pred[1]),
        "professionalism_score": float(scores_pred[2]),
        "script_adherence_score": float(scores_pred[3]),
    }

    return PrefilterResult(
        tier_hit=3,
        decision="short_circuit",
        confidence=1.0 - flag_prob,
        predicted_scores=avg_scores,
        notes=f"flag_prob={flag_prob:.3f} < {flag_prob_threshold}",
        result=_build_result(contact_name, avg_scores, messages, agent_name),
    )


def _build_result(
    contact_name: str,
    scores: dict,
    messages: list[dict] | None = None,
    agent_name: str = "",
) -> dict:
    """Assemble a Groq-shaped output dict."""
    from . import summary_builder

    if messages:
        smart_summary = summary_builder.build_summary(
            messages, agent_name, contact_name, scores, model_used="prefilter_t3",
        )
        label, label_reason = summary_builder.detect_label(messages, contact_name)
        funnel = summary_builder.detect_funnel_stage(messages)
    else:
        smart_summary = "Classified as clean by Tier 3 embedding classifier."
        label, label_reason = "Lead", "Label deferred — no message data available."
        funnel = "none"

    return {
        "compliance_score": scores["compliance_score"],
        "sentiment_score": scores["sentiment_score"],
        "professionalism_score": scores["professionalism_score"],
        "script_adherence_score": scores["script_adherence_score"],
        "funnel_stage_reached": funnel,
        "pillars_gathered": [],
        "rebuttals_used": [],
        "label_assigned": label,
        "label_correct": True,
        "label_should_be": label,
        "label_reason": label_reason,
        "red_flags": [],
        "actions_triggered": [],
        "summary": smart_summary,
        "model_used": "prefilter_t3",
        "contact_name": contact_name,
    }
