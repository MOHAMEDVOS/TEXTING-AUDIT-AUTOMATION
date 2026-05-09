"""
Offline: train the Tier 3 classifier.

Trains three models on top of sentence-transformer embeddings:

  flag_clf  — LogisticRegression that predicts P(conversation has a real red flag)
  label_clf — OneVsRestClassifier(LogisticRegression) for multi-label flag prediction
              (predicts which of the 12 whitelist flags are present)
  score_reg — MultiOutputRegressor(Ridge) that predicts the 4 audit scores

Inputs:
  - eval_500_conversations.json (conversation texts)
  - eval_baseline_v2.json (ground truth: outcome, red_flags, pillars, rebuttal_quality)

Outputs:
  - ai/prefilter/artifacts/classifier.joblib

Usage:
    python -m ai.prefilter.train
    python -m ai.prefilter.train --test-split 0.2   # report metrics on holdout
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

import numpy as np

from config import settings

from . import embedder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("train")


def _load_baseline_training_data() -> tuple[list[dict], dict]:
    """
    Load eval_500_conversations.json and eval_baseline_v2.json.
    Build label map: conversation_id → baseline entry.
    """
    conv_path = Path(__file__).parent.parent.parent / "scripts" / "eval_500_conversations.json"
    baseline_path = Path(__file__).parent.parent.parent / "scripts" / "eval_baseline_v2.json"

    if not conv_path.exists():
        raise FileNotFoundError(f"Missing {conv_path}. Run eval_baseline_v2.py first.")
    if not baseline_path.exists():
        raise FileNotFoundError(f"Missing {baseline_path}. Run eval_baseline_v2.py first.")

    with open(conv_path, "r", encoding="utf-8") as f:
        conversations = json.load(f)
    with open(baseline_path, "r", encoding="utf-8") as f:
        baseline_list = json.load(f)

    baseline_map = {b["conversation_id"]: b for b in baseline_list}

    scripts_dir = Path(__file__).parent.parent.parent / "scripts"
    for synthetic_path in sorted(scripts_dir.glob("synthetic_*_training.json")):
        with open(synthetic_path, "r", encoding="utf-8") as f:
            synthetic = json.load(f)
        synthetic_conversations = synthetic.get("conversations", [])
        synthetic_baselines = synthetic.get("baselines", [])
        conversations.extend(synthetic_conversations)
        baseline_map.update({b["conversation_id"]: b for b in synthetic_baselines})
        logger.info(
            "Loaded %s synthetic conversations from %s",
            len(synthetic_conversations),
            synthetic_path.name,
        )

    return conversations, baseline_map


def _infer_scores_from_baseline(baseline: dict) -> dict:
    """
    Infer audit scores from baseline outcome, pillars, rebuttal_quality, red_flags.
    Returns: {compliance_score, sentiment_score, professionalism_score, script_adherence_score}
    """
    base_score = 90.0
    outcome = baseline.get("outcome", "")
    pillars = baseline.get("pillars_gathered", [])
    rebuttal = baseline.get("rebuttal_quality", "none")
    red_flags = baseline.get("red_flags", [])

    # Compliance: penalize for flags, helped by good rebuttal
    compliance = base_score
    if red_flags:
        compliance -= 20  # Has compliance violations
    if rebuttal == "good":
        compliance += 5
    compliance = max(0, min(100, compliance))

    # Sentiment: penalize for "not_interested", helped by "interested"
    sentiment = base_score
    if outcome in ("not_interested", "maybe"):
        sentiment -= 15
    elif outcome in ("interested", "abv_mv"):
        sentiment += 10
    sentiment = max(0, min(100, sentiment))

    # Professionalism: penalize for flags, helped by pillar coverage
    professionalism = base_score
    if red_flags:
        professionalism -= 15
    if len(pillars) >= 3:
        professionalism += 5
    professionalism = max(0, min(100, professionalism))

    # Script adherence: penalize for opt_out/dnc, helped by rebuttal quality
    script_adherence = base_score
    if outcome in ("opt_out",):
        script_adherence -= 20
    if rebuttal == "good":
        script_adherence += 5
    script_adherence = max(0, min(100, script_adherence))

    return {
        "compliance_score": compliance,
        "sentiment_score": sentiment,
        "professionalism_score": professionalism,
        "script_adherence_score": script_adherence,
    }


def main(test_split: float = 0.0) -> None:
    settings.PREFILTER_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.multioutput import MultiOutputRegressor
        from sklearn.multiclass import OneVsRestClassifier
        from sklearn.metrics import (
            accuracy_score, classification_report, mean_absolute_error,
        )
        import joblib
    except ImportError as e:
        logger.error(f"scikit-learn / joblib missing: {e}")
        sys.exit(1)

    # Import the whitelist for multi-label target construction
    from ai.prefilter._guards import WHITELIST_FLAG_OUTPUTS, _canon_flag_text

    if embedder.get_model() is None:
        logger.error("sentence-transformers not installed.")
        sys.exit(1)

    # Load eval baseline data
    conversations, baseline_map = _load_baseline_training_data()
    logger.info(f"Loaded {len(conversations)} conversations + {len(baseline_map)} baseline entries")

    # Filter to only conversations with baseline labels
    labeled = [c for c in conversations if c["conversation_id"] in baseline_map]
    logger.info(f"Training on {len(labeled)} conversations with baseline labels")

    if len(labeled) < 50:
        logger.error(
            f"Only {len(labeled)} labeled conversations — need ≥50 for a useful classifier."
        )
        sys.exit(1)

    # Build training data with funnel-aware text
    rows = []
    for conv in labeled:
        cid = conv["conversation_id"]
        baseline = baseline_map[cid]
        messages = conv["messages"]
        agent_name = conv.get("account_name", "Agent")
        funnel_tier = (conv.get("funnel_tier") or "NF").upper().strip()
        if funnel_tier not in ("WF", "MF", "NF"):
            funnel_tier = "NF"

        # Build funnel-aware text (matching index_builder.py and tier2_embedding.py)
        text = embedder.conversation_to_text(messages, agent_name)
        text = f"[{funnel_tier}]\n{text}"

        scores = _infer_scores_from_baseline(baseline)
        red_flags = baseline.get("red_flags", [])
        is_clean = len([f for f in red_flags if isinstance(f, str) and f.strip()]) == 0

        rows.append({
            "conversation_id": cid,
            "text": text,
            "funnel_tier": funnel_tier,
            "is_clean": is_clean,
            "scores": scores,
            "outcome": baseline.get("outcome", ""),
            "red_flags_raw": [f for f in red_flags if isinstance(f, str) and f.strip()],
        })

    texts = [r["text"] for r in rows]
    logger.info("Embedding conversations (this may take a minute)...")
    vecs = embedder.embed_batch(texts)
    X = np.asarray(vecs, dtype=np.float32)

    # Target: y_flag = 1 if conversation has any real flag, else 0
    y_flag = np.array([0 if r["is_clean"] else 1 for r in rows], dtype=np.int32)

    # Target: y_scores = [comp, sent, prof, script]
    y_scores = np.array([
        [r["scores"]["compliance_score"],
         r["scores"]["sentiment_score"],
         r["scores"]["professionalism_score"],
         r["scores"]["script_adherence_score"]]
        for r in rows
    ], dtype=np.float32)

    # Build sample weights: hard negatives (flagged outcomes with no agent error) get 2x weight
    weights_all = np.array(
        [2.0 if (not r["is_clean"] and r["outcome"] not in ("neutral", "maybe")) else 1.0
         for r in rows],
        dtype=np.float32,
    )

    # ── Multi-label targets for flag prediction ────────────────────
    # Build binary matrix: rows x 12 whitelist flags
    flag_labels = WHITELIST_FLAG_OUTPUTS  # The 12 canonical flag strings
    flag_canon_map = {_canon_flag_text(f): i for i, f in enumerate(flag_labels)}
    n_labels = len(flag_labels)

    y_multilabel = np.zeros((len(rows), n_labels), dtype=np.int32)
    for row_idx, r in enumerate(rows):
        raw_flags = r.get("red_flags_raw", [])
        for flag_text in raw_flags:
            if not isinstance(flag_text, str):
                continue
            canon = _canon_flag_text(flag_text)
            if canon in flag_canon_map:
                y_multilabel[row_idx, flag_canon_map[canon]] = 1

    # Count how many rows have at least one flag label
    n_with_labels = int((y_multilabel.sum(axis=1) > 0).sum())
    logger.info(f"Multi-label targets: {n_with_labels}/{len(rows)} rows have ≥1 flag label")

    # Train/test split (optional)
    if test_split > 0:
        from sklearn.model_selection import train_test_split
        X_tr, X_te, yf_tr, yf_te, ys_tr, ys_te, w_tr, _ = train_test_split(
            X, y_flag, y_scores, weights_all,
            test_size=test_split, random_state=42, stratify=y_flag,
        )
    else:
        X_tr, X_te = X, None
        yf_tr, yf_te = y_flag, None
        ys_tr, ys_te = y_scores, None
        w_tr = weights_all

    logger.info(f"Class balance — clean: {(yf_tr==0).sum()}, flagged: {(yf_tr==1).sum()}")

    n_hard_neg = int((w_tr == 2.0).sum())
    if n_hard_neg < 5:
        if n_hard_neg > 0:
            logger.warning(
                f"Only {n_hard_neg} hard negatives — not enough to weight reliably, "
                f"using balanced class weights instead."
            )
        w_tr = None

    # ── Flag classifier ─────────────────────────────────────────────
    logger.info("Training flag classifier (LogisticRegression)...")
    flag_clf = LogisticRegression(
        max_iter=1000, class_weight="balanced", C=1.0,
    )
    flag_clf.fit(X_tr, yf_tr, sample_weight=w_tr)

    # ── Score regressor ─────────────────────────────────────────────
    logger.info("Training score regressor (MultiOutput Ridge)...")
    score_reg = MultiOutputRegressor(Ridge(alpha=1.0))
    score_reg.fit(X_tr, ys_tr)

    # ── Multi-label flag classifier ─────────────────────────────────
    label_clf = None
    active_cols = []
    if n_with_labels >= 10:
        if test_split > 0:
            from sklearn.model_selection import train_test_split as _tts
            ym_tr = y_multilabel[:X_tr.shape[0]]
        else:
            ym_tr = y_multilabel

        logger.info("Training multi-label flag classifier (OneVsRest LogReg)...")
        # Only train on columns that have at least 2 positive examples
        active_cols = [i for i in range(n_labels) if ym_tr[:, i].sum() >= 2]
        if active_cols:
            label_clf = OneVsRestClassifier(
                LogisticRegression(max_iter=1000, class_weight="balanced", C=1.0)
            )
            label_clf.fit(X_tr, ym_tr[:, active_cols])
            logger.info(
                f"Multi-label classifier trained on {len(active_cols)}/{n_labels} active flag types"
            )
        else:
            logger.warning("No flag types have enough examples for multi-label training")
    else:
        logger.info(
            f"Skipping multi-label classifier: only {n_with_labels} rows with labels (need ≥10)"
        )

    # ── Holdout metrics ─────────────────────────────────────────────
    if X_te is not None:
        yf_pred = flag_clf.predict(X_te)
        ys_pred = score_reg.predict(X_te)
        logger.info(
            f"Flag classifier accuracy on holdout: "
            f"{accuracy_score(yf_te, yf_pred):.3f}"
        )
        logger.info("Flag classification report:")
        for line in classification_report(
            yf_te, yf_pred, target_names=["clean", "flagged"], digits=3
        ).splitlines():
            logger.info("  " + line)
        for i, k in enumerate([
            "compliance", "sentiment", "professionalism", "script_adherence"
        ]):
            mae = mean_absolute_error(ys_te[:, i], ys_pred[:, i])
            logger.info(f"Score regressor MAE [{k:<16}]: {mae:.2f}")

    # ── Save bundle ─────────────────────────────────────────────────
    active_flag_labels = (
        [flag_labels[i] for i in active_cols] if (label_clf and active_cols) else []
    )
    bundle = {
        "flag_clf":           flag_clf,
        "score_reg":          score_reg,
        "label_clf":          label_clf,           # NEW: multi-label classifier
        "flag_labels":        flag_labels,          # NEW: all 12 whitelist flag strings
        "active_flag_labels": active_flag_labels,   # NEW: subset with enough training data
        "feature_dim":        int(X.shape[1]),
        "model_name":         settings.PREFILTER_EMBEDDING_MODEL,
        "trained_at":         datetime.datetime.utcnow().isoformat() + "Z",
        "n_train":            int(len(yf_tr)),
        "n_clean":            int((yf_tr == 0).sum()),
        "n_flagged":          int((yf_tr == 1).sum()),
        "n_hard_neg":         n_hard_neg,
        "n_multilabel":       n_with_labels,
        "n_active_labels":    len(active_flag_labels),
    }

    out = Path(settings.PREFILTER_CLASSIFIER_PATH)
    joblib.dump(bundle, out)
    logger.info(f"Saved classifier bundle → {out}")

    # ── Write manifest.json (merge with existing if present) ─────────
    manifest_path = settings.PREFILTER_DIR / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                import json
                manifest = json.load(f)
        except Exception:
            pass
    manifest["classifier"] = {
        "trained_at": bundle["trained_at"],
        "n_train": bundle["n_train"],
        "n_clean": bundle["n_clean"],
        "n_flagged": bundle["n_flagged"],
        "n_hard_neg": bundle["n_hard_neg"],
        "embedding_model": bundle["model_name"],
        "feature_dim": bundle["feature_dim"],
    }
    import json
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    logger.info(f"Updated manifest → {manifest_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Train Tier 3 classifier.")
    p.add_argument("--test-split", type=float, default=0.0,
                   help="If > 0, hold out this fraction for evaluation.")
    args = p.parse_args()
    main(test_split=args.test_split)
