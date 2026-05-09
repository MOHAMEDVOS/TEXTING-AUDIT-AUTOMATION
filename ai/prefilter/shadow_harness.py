"""
Shadow Harness — Compare local ML pipeline (T4) results against Groq ground truth.

Runs both the local Tier 4 deterministic generator AND the existing Groq analyzer
on the same conversations, then produces an accuracy report with:
  - Score MAE (Mean Absolute Error) per dimension
  - Flag agreement rate (precision, recall, F1)
  - Label correctness agreement
  - Per-conversation disagreement breakdown

Usage:
    python -m ai.prefilter.shadow_harness                  # run on 50 recent convos
    python -m ai.prefilter.shadow_harness --limit 200      # run on 200
    python -m ai.prefilter.shadow_harness --csv report.csv # export CSV

This harness is the gatekeeper for Phase C (Strip):
  - Score MAE must be < 5.0 on all 4 dimensions
  - Flag agreement F1 must be > 0.80
  - Label correctness agreement must be > 0.90
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("shadow_harness")


def fetch_groq_scored_conversations(conn, limit: int = 50) -> list[dict]:
    """
    Fetch conversations that have Groq scores (ground truth).
    Returns rows with messages, agent name, contact name, labels, and Groq scores.
    """
    sql = """
    SELECT
        c.id AS conversation_id,
        ct.name AS contact_name,
        COALESCE(ac.name, 'Agent') AS agent_name,
        cs.compliance_score,
        cs.sentiment_score,
        cs.professionalism_score,
        cs.script_adherence_score,
        cs.red_flags,
        cs.label_assigned,
        cs.label_correct,
        cs.label_should_be
    FROM conversations c
    JOIN conversation_scores cs ON cs.conversation_id = c.id
    JOIN contacts ct            ON ct.id = c.contact_id
    LEFT JOIN accounts ac       ON ac.id = c.agent_id
    WHERE
        COALESCE(cs.source, 'groq') = 'groq'
        AND cs.model_used IS NOT NULL
        AND cs.model_used <> ''
    GROUP BY c.id, ct.name, ac.name,
             cs.compliance_score, cs.sentiment_score,
             cs.professionalism_score, cs.script_adherence_score,
             cs.red_flags, cs.label_assigned, cs.label_correct, cs.label_should_be
    ORDER BY c.id DESC
    LIMIT %s
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (limit,))
        rows = list(cur.fetchall())
    return rows


def fetch_messages(conn, conversation_id: int) -> list[dict]:
    """Fetch messages for a single conversation."""
    sql = """
    SELECT sender, body AS message, sent_at
    FROM messages
    WHERE conversation_id = %s
    ORDER BY sent_at NULLS LAST, id
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (conversation_id,))
        return [dict(r) for r in cur.fetchall()]


def run_shadow(limit: int = 50, csv_path: str | None = None) -> dict:
    """
    Run the shadow comparison harness.

    Returns summary dict with accuracy metrics.
    """
    from ai.prefilter.tier4_flag_generator import generate as t4_generate

    logger.info(f"Shadow Harness: comparing T4 vs Groq on {limit} conversations...")

    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        rows = fetch_groq_scored_conversations(conn, limit=limit)
        logger.info(f"Fetched {len(rows)} Groq-scored conversations")

        if not rows:
            logger.error("No Groq-scored conversations found. Run audits first.")
            return {"error": "No data"}

        comparisons = []
        for row in rows:
            cid = row["conversation_id"]
            messages = fetch_messages(conn, cid)
            if not messages:
                continue

            # Run T4
            label_str = row.get("label_assigned") or ""
            assigned_labels = [l.strip() for l in label_str.split(",") if l.strip()] if label_str else []
            t4_result = t4_generate(
                messages,
                row["agent_name"],
                row["contact_name"],
                assigned_labels=assigned_labels,
            )

            # Parse Groq flags
            groq_flags = row.get("red_flags") or []
            if isinstance(groq_flags, str):
                try:
                    groq_flags = json.loads(groq_flags)
                except Exception:
                    groq_flags = []
            groq_flags = [f for f in (groq_flags or []) if isinstance(f, str) and f.strip()]

            comparisons.append({
                "conversation_id": cid,
                "contact_name": row["contact_name"],
                # Groq scores
                "groq_compliance": row["compliance_score"],
                "groq_sentiment": row["sentiment_score"],
                "groq_professionalism": row["professionalism_score"],
                "groq_script": row["script_adherence_score"],
                "groq_flags": groq_flags,
                "groq_label_correct": row.get("label_correct"),
                # T4 scores
                "t4_compliance": t4_result["compliance_score"],
                "t4_sentiment": t4_result["sentiment_score"],
                "t4_professionalism": t4_result["professionalism_score"],
                "t4_script": t4_result["script_adherence_score"],
                "t4_flags": t4_result.get("red_flags", []),
                "t4_label_correct": t4_result.get("label_correct"),
            })

    finally:
        conn.close()

    if not comparisons:
        logger.error("No conversations could be compared.")
        return {"error": "No valid comparisons"}

    # ── Compute metrics ──────────────────────────────────────────────
    n = len(comparisons)

    # Score MAE
    dims = ["compliance", "sentiment", "professionalism", "script"]
    mae = {}
    for dim in dims:
        errors = [
            abs((c.get(f"groq_{dim}") or 0) - (c.get(f"t4_{dim}") or 0))
            for c in comparisons
        ]
        mae[dim] = round(sum(errors) / len(errors), 2) if errors else 0

    # Flag agreement
    flag_tp, flag_fp, flag_fn = 0, 0, 0
    for c in comparisons:
        groq_set = set(c["groq_flags"])
        t4_set = set(c["t4_flags"])
        flag_tp += len(groq_set & t4_set)
        flag_fp += len(t4_set - groq_set)
        flag_fn += len(groq_set - t4_set)

    precision = flag_tp / (flag_tp + flag_fp) if (flag_tp + flag_fp) > 0 else 1.0
    recall = flag_tp / (flag_tp + flag_fn) if (flag_tp + flag_fn) > 0 else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    # Label correctness agreement
    label_agree = sum(
        1 for c in comparisons
        if c["groq_label_correct"] == c["t4_label_correct"]
    )
    label_agreement = round(label_agree / n, 3) if n > 0 else 0.0

    # ── Phase C readiness check ──────────────────────────────────────
    score_gate = all(mae[d] < 5.0 for d in dims)
    flag_gate = f1 > 0.80
    label_gate = label_agreement > 0.90
    ready = score_gate and flag_gate and label_gate

    results = {
        "n_conversations": n,
        "score_mae": mae,
        "flag_precision": round(precision, 3),
        "flag_recall": round(recall, 3),
        "flag_f1": round(f1, 3),
        "flag_tp": flag_tp,
        "flag_fp": flag_fp,
        "flag_fn": flag_fn,
        "label_agreement": label_agreement,
        "phase_c_ready": ready,
        "gates": {
            "score_mae_all_under_5": score_gate,
            "flag_f1_above_0.80": flag_gate,
            "label_agree_above_0.90": label_gate,
        },
    }

    # ── Print report ─────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"SHADOW HARNESS REPORT — {n} conversations")
    logger.info("=" * 60)
    logger.info("")
    logger.info("Score MAE (lower is better, target < 5.0):")
    for d in dims:
        status = "✓" if mae[d] < 5.0 else "✗"
        logger.info(f"  {d:<20}: {mae[d]:>6.2f}  {status}")
    logger.info("")
    logger.info("Flag Agreement:")
    logger.info(f"  Precision:    {precision:.3f}")
    logger.info(f"  Recall:       {recall:.3f}")
    logger.info(f"  F1:           {f1:.3f}  {'✓' if flag_gate else '✗'} (target > 0.80)")
    logger.info(f"  TP={flag_tp}  FP={flag_fp}  FN={flag_fn}")
    logger.info("")
    logger.info(f"Label Agreement: {label_agreement:.3f}  {'✓' if label_gate else '✗'} (target > 0.90)")
    logger.info("")
    logger.info(f"Phase C Ready: {'✓ YES' if ready else '✗ NOT YET'}")
    logger.info("=" * 60)

    # ── Optional CSV export ──────────────────────────────────────────
    if csv_path:
        _write_csv(comparisons, csv_path)
        logger.info(f"Wrote CSV → {csv_path}")

    return results


def _write_csv(comparisons: list[dict], path: str) -> None:
    """Write per-conversation comparison to CSV."""
    fieldnames = [
        "conversation_id", "contact_name",
        "groq_compliance", "t4_compliance",
        "groq_sentiment", "t4_sentiment",
        "groq_professionalism", "t4_professionalism",
        "groq_script", "t4_script",
        "groq_flags", "t4_flags",
        "groq_label_correct", "t4_label_correct",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for c in comparisons:
            row = {k: c.get(k) for k in fieldnames}
            # Stringify flag lists
            row["groq_flags"] = "; ".join(c.get("groq_flags", []))
            row["t4_flags"] = "; ".join(c.get("t4_flags", []))
            writer.writerow(row)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Shadow Harness: T4 vs Groq comparison")
    parser.add_argument("--limit", type=int, default=50,
                        help="Number of conversations to compare")
    parser.add_argument("--csv", type=str, default=None,
                        help="Path to export CSV report")
    args = parser.parse_args()
    result = run_shadow(limit=args.limit, csv_path=args.csv)
    sys.exit(0 if result.get("phase_c_ready") else 1)
