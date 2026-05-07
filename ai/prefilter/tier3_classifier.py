"""
Tier 3 — Small classifier on top of embeddings.

Loads a joblib bundle produced by `python -m ai.prefilter.train`:

  {
    "flag_clf":   sklearn.LogisticRegression,   # P(this conversation has any red flag)
    "score_reg":  sklearn.MultiOutputRegressor, # predicts [comp, sent, prof, script]
    "feature_dim": 384,
    "model_name":  "all-MiniLM-L6-v2",
    "trained_at":  "...",
  }

Decision rule:
  - flag_prob < PREFILTER_T3_MAX_FLAG_PROB
    AND all 4 predicted scores ≥ PREFILTER_T3_MIN_SCORE
    → SHORT-CIRCUIT with predicted scores (no flags).
  - Otherwise → escalate.

The classifier never decides flags itself — it only decides whether a
conversation is *safe to skip Groq*. Anything borderline goes to Groq.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from config import settings

from . import embedder
from ._pipeline_types import PipelineResult as PrefilterResult

logger = logging.getLogger(__name__)

_bundle = None
_load_failed = False
_bundle_lock = threading.Lock()


def _load_bundle() -> bool:
    global _bundle, _load_failed
    if _bundle is not None:
        return True
    if _load_failed:
        return False

    with _bundle_lock:
        # Double-checked: another thread may have loaded while we waited.
        if _bundle is not None:
            return True
        if _load_failed:
            return False

        path = Path(settings.PREFILTER_CLASSIFIER_PATH)
        if not path.exists():
            logger.info(
                f"[Prefilter T3] No classifier at {path}. "
                f"Run `python -m ai.prefilter.train` to build it."
            )
            _load_failed = True
            return False

        try:
            import joblib  # heavy import
        except ImportError:
            logger.warning(
                "[Prefilter T3] joblib not installed. Install with: pip install joblib"
            )
            _load_failed = True
            return False

        try:
            _bundle = joblib.load(path)
            logger.info(
                f"[Prefilter T3] Loaded classifier (trained {_bundle.get('trained_at', '?')})"
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
) -> Optional[PrefilterResult]:
    if not _load_bundle():
        return None

    text = embedder.conversation_to_text(messages, agent_name)
    if not text.strip():
        return None

    vec = embedder.embed(text)
    if vec is None:
        return None

    X = np.asarray([vec], dtype=np.float32)

    flag_clf = _bundle.get("flag_clf")
    score_reg = _bundle.get("score_reg")
    if flag_clf is None or score_reg is None:
        return None

    try:
        flag_prob = float(flag_clf.predict_proba(X)[0][1])
        scores = score_reg.predict(X)[0].tolist()
    except Exception as e:
        logger.warning(f"[Prefilter T3] Inference failed for {contact_name}: {e}")
        return None

    score_dict = {
        "compliance_score": round(float(scores[0]), 1),
        "sentiment_score": round(float(scores[1]), 1),
        "professionalism_score": round(float(scores[2]), 1),
        "script_adherence_score": round(float(scores[3]), 1),
    }

    flag_safe = flag_prob < settings.PREFILTER_T3_MAX_FLAG_PROB
    score_safe = all(v >= settings.PREFILTER_T3_MIN_SCORE for v in score_dict.values())

    if not (flag_safe and score_safe):
        # Borderline → escalate.
        return PrefilterResult(
            tier_hit=3,
            decision="escalate",
            confidence=1.0 - flag_prob,
            predicted_scores=score_dict,
            notes=f"flag_prob={flag_prob:.3f}, scores={score_dict}",
        )

    return PrefilterResult(
        tier_hit=3,
        decision="short_circuit",
        confidence=1.0 - flag_prob,
        predicted_scores=score_dict,
        notes=f"flag_prob={flag_prob:.3f}",
        result=_build_result(contact_name, score_dict, flag_prob, messages, agent_name),
    )


def _build_result(
    contact_name: str,
    scores: dict,
    flag_prob: float,
    messages: list[dict] | None = None,
    agent_name: str = "",
) -> dict:
    from . import summary_builder

    if messages:
        smart_summary = summary_builder.build_summary(
            messages, agent_name, contact_name, scores, model_used="prefilter_t3",
        )
        label, label_reason = summary_builder.detect_label(messages, contact_name)
        funnel = summary_builder.detect_funnel_stage(messages)
    else:
        smart_summary = (
            f"Classifier predicted no red flags "
            f"(flag_prob={flag_prob:.2%}) and all four scores above threshold."
        )
        label, label_reason = "Lead", "Classifier confident this conversation is clean."
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
