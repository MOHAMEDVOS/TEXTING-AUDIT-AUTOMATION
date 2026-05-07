"""
Offline: train the Tier 3 classifier.

Trains two models on top of sentence-transformer embeddings:

  flag_clf  — LogisticRegression that predicts P(conversation has a real red flag)
  score_reg — MultiOutputRegressor(Ridge) that predicts the 4 audit scores

Inputs:
  - conversations + conversation_scores (filtered to Groq-sourced rows only)
  - flag_feedback (used to mask false-positive flags)

Outputs:
  - ai/prefilter/artifacts/classifier.joblib

Usage:
    python -m ai.prefilter.train
    python -m ai.prefilter.train --test-split 0.2   # report metrics on holdout
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path

import numpy as np

from config import settings

from . import embedder
from .index_builder import (
    fetch_invalid_flag_patterns,
    fetch_negative_example_ids,
    fetch_training_rows,
    is_clean,
    _connect,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("train")


def main(test_split: float = 0.0) -> None:
    settings.PREFILTER_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.multioutput import MultiOutputRegressor
        from sklearn.metrics import (
            accuracy_score, classification_report, mean_absolute_error,
        )
        import joblib
    except ImportError as e:
        logger.error(f"scikit-learn / joblib missing: {e}")
        sys.exit(1)

    if embedder.get_model() is None:
        logger.error("sentence-transformers not installed.")
        sys.exit(1)

    logger.info("Connecting to Postgres...")
    conn = _connect()
    try:
        invalid_patterns = fetch_invalid_flag_patterns(conn)
        rows = fetch_training_rows(conn)
        negative_ids = fetch_negative_example_ids(conn)
    finally:
        conn.close()
    logger.info(f"Hard negatives (human-rejected): {len(negative_ids)}")

    if len(rows) < 50:
        logger.error(
            f"Only {len(rows)} labeled conversations — need ≥50 for a useful classifier."
        )
        sys.exit(1)

    logger.info(f"Training on {len(rows)} conversations")

    texts = [r["conversation_text"] for r in rows]
    vecs = embedder.embed_batch(texts)
    X = np.asarray(vecs, dtype=np.float32)

    # Target: y_flag = 1 if conversation has any real flag, else 0
    y_flag = np.array(
        [0 if is_clean(r["red_flags"], invalid_patterns) else 1 for r in rows]
    )

    # Target: y_scores = [comp, sent, prof, script]
    def _val(r, k):
        v = r.get(k)
        return float(v) if v is not None else 90.0  # default for missing scores

    y_scores = np.array([
        [_val(r, "compliance_score"),
         _val(r, "sentiment_score"),
         _val(r, "professionalism_score"),
         _val(r, "script_adherence_score")]
        for r in rows
    ], dtype=np.float32)

    # Build sample weights before the split: hard negatives get 2x weight
    conv_ids = [r["conversation_id"] for r in rows]
    weights_all = np.array(
        [2.0 if cid in negative_ids else 1.0 for cid in conv_ids],
        dtype=np.float32,
    )

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
    if n_hard_neg < 10:
        if n_hard_neg > 0:
            logger.warning(
                f"Only {n_hard_neg} hard negatives — too few to weight reliably, "
                f"ignoring sample_weight for this run."
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
    bundle = {
        "flag_clf":    flag_clf,
        "score_reg":   score_reg,
        "feature_dim": int(X.shape[1]),
        "model_name":  settings.PREFILTER_EMBEDDING_MODEL,
        "trained_at":  datetime.datetime.utcnow().isoformat() + "Z",
        "n_train":     int(len(yf_tr)),
        "n_clean":     int((yf_tr == 0).sum()),
        "n_flagged":   int((yf_tr == 1).sum()),
        "n_hard_neg":  n_hard_neg,
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
