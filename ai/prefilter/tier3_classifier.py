"""
Tier 3 — Trained logistic regression + score regressor + multi-label flag predictor.

How it works:
  1. Embed the incoming conversation with sentence-transformers (with [FT] prefix).
  2. Run through trained flag_clf to get P(red_flag).
  3. If label_clf exists, predict which specific flags are present.
  4. If P < flag_prob_threshold AND min predicted score >= threshold, short-circuit.
  5. Otherwise, return None to escalate.
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
    assigned_labels: list[str] | None = None,
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
    flag_prob_threshold = settings.PREFILTER_T3_MAX_FLAG_PROB
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

    # ── Multi-label flag prediction (if available) ────────────────────
    predicted_flags = []
    label_clf = _classifier_bundle.get("label_clf")
    active_flag_labels = _classifier_bundle.get("active_flag_labels", [])
    label_confidence = settings.PREFILTER_T3_LABEL_CONFIDENCE

    if label_clf is not None and active_flag_labels:
        try:
            # predict_proba returns probabilities for each active label
            label_probs = label_clf.predict_proba(query)
            # label_probs may be a list of arrays (one per class) or a 2D array
            if isinstance(label_probs, list):
                # OneVsRest returns list of arrays, each (1, 2) shaped
                for i, probs in enumerate(label_probs):
                    if i < len(active_flag_labels):
                        p_positive = float(probs[0, 1]) if probs.shape[1] == 2 else float(probs[0, 0])
                        if p_positive >= label_confidence:
                            predicted_flags.append(active_flag_labels[i])
            else:
                # 2D array: (1, n_labels)
                for i, p in enumerate(label_probs[0]):
                    if i < len(active_flag_labels) and float(p) >= label_confidence:
                        predicted_flags.append(active_flag_labels[i])
        except Exception as e:
            logger.debug(f"[Prefilter T3] Multi-label prediction failed (non-fatal): {e}")

    min_score = min(avg_scores.values())
    min_score_threshold = settings.PREFILTER_T3_MIN_SCORE

    # If predicted scores are too low, escalate
    if min_score < min_score_threshold:
        return PrefilterResult(
            tier_hit=3,
            decision="escalate",
            confidence=1.0 - flag_prob,
            notes=f"min_score={min_score:.1f} < {min_score_threshold}",
        )

    return PrefilterResult(
        tier_hit=3,
        decision="short_circuit",
        confidence=1.0 - flag_prob,
        predicted_scores=avg_scores,
        notes=(
            f"flag_prob={flag_prob:.3f} < {flag_prob_threshold}, "
            f"min_score={min_score:.1f}, "
            f"predicted_flags={len(predicted_flags)}"
        ),
        result=_build_result(
            contact_name, avg_scores, messages, agent_name,
            assigned_labels, predicted_flags=predicted_flags,
        ),
    )


def _build_result(
    contact_name: str,
    scores: dict,
    messages: list[dict] | None = None,
    agent_name: str = "",
    assigned_labels: list[str] | None = None,
    predicted_flags: list[str] | None = None,
) -> dict:
    """Assemble a Groq-shaped output dict."""
    from . import summary_builder

    # Use the label the texter actually set — never guess
    label = (assigned_labels or [""])[0].strip() if assigned_labels else ""
    from .label_validator import validate_label
    label_check = validate_label(messages, label)

    if messages:
        smart_summary = summary_builder.build_summary(
            messages, agent_name, contact_name, scores, model_used="prefilter_t3",
        )
        funnel = summary_builder.detect_funnel_stage(messages)
    else:
        smart_summary = "Classified as clean by Tier 3 embedding classifier."
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
        "label_correct": label_check["label_correct"],
        "label_should_be": label_check["label_should_be"],
        "label_reason": label_check["label_reason"],
        "red_flags": predicted_flags or [],
        "actions_triggered": [],
        "summary": smart_summary,
        "model_used": "prefilter_t3",
        "contact_name": contact_name,
    }
