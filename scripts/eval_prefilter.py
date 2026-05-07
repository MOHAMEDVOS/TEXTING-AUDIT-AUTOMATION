"""
Shadow-mode evaluation harness for the ML pre-filter.

Replays past Groq-scored conversations through the prefilter and reports:
  - per-tier hit rate
  - false-clean rate (prefilter said clean but Groq flagged) — the dangerous one
  - false-escalate rate (prefilter escalated but Groq saw no flag) — wasted Groq cost
  - score MAE for short-circuited convos

Usage:
    python scripts/eval_prefilter.py
    python scripts/eval_prefilter.py --limit 500
    python scripts/eval_prefilter.py --since 2026-04-01
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

# Make project root importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import psycopg2
import psycopg2.extras

from config import settings
from ai.prefilter import run_prefilter
from ai.prefilter.index_builder import (
    fetch_invalid_flag_patterns,
    is_clean,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("eval_prefilter")


def fetch_eval_set(conn, limit: int, since: str | None) -> list[dict]:
    where = [
        "cs.model_used IS NOT NULL",
        "cs.model_used <> ''",
        "COALESCE(cs.source, 'groq') = 'groq'",
    ]
    params: list = []
    if since:
        where.append("cs.scored_at >= %s")
        params.append(since)

    sql = f"""
    SELECT
        c.id          AS conversation_id,
        c.texter_name AS agent_name,
        co.name       AS contact_name,
        cs.compliance_score,
        cs.sentiment_score,
        cs.professionalism_score,
        cs.script_adherence_score,
        cs.red_flags,
        json_agg(json_build_object(
            'sender', m.sender,
            'body', m.body,
            'sent_at', m.sent_at
        ) ORDER BY m.sent_at NULLS LAST, m.id) AS messages
    FROM conversations c
    JOIN conversation_scores cs ON cs.conversation_id = c.id
    JOIN contacts co            ON co.id = c.contact_id
    LEFT JOIN messages m        ON m.conversation_id = c.id
    WHERE {' AND '.join(where)}
    GROUP BY c.id, c.texter_name, co.name,
             cs.compliance_score, cs.sentiment_score,
             cs.professionalism_score, cs.script_adherence_score,
             cs.red_flags, cs.scored_at
    ORDER BY cs.scored_at DESC
    LIMIT %s
    """
    params.append(limit)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def evaluate(limit: int, since: str | None, output_md: str | None = None) -> bool:
    # Force ENABLED + SHADOW so the prefilter runs but never short-circuits a real run.
    settings.PREFILTER_ENABLED = True
    settings.PREFILTER_SHADOW_MODE = True

    conn = psycopg2.connect(settings.DATABASE_URL)
    try:
        invalid_patterns = fetch_invalid_flag_patterns(conn)
        rows = fetch_eval_set(conn, limit=limit, since=since)
    finally:
        conn.close()

    if not rows:
        logger.error("No eval rows. Make sure conversation_scores has Groq-sourced data.")
        return False

    logger.info(f"Evaluating prefilter on {len(rows)} past Groq-scored convos...")

    # Counters
    tier_hits = Counter()
    tier_short_circuit = Counter()
    tier_escalate = Counter()
    false_clean = 0       # prefilter short_circuited but Groq found a flag — DANGEROUS
    correct_clean = 0     # prefilter short_circuited and Groq agreed it's clean
    false_escalate = 0    # prefilter escalated but Groq saw no flag — wasted cost
    correct_escalate = 0  # prefilter escalated and Groq found a flag — saved a miss
    score_mae_total = {"compliance_score": [], "sentiment_score": [],
                       "professionalism_score": [], "script_adherence_score": []}

    # Force the pipeline into "would-short-circuit" mode by simulating live mode
    # in a copy of settings — we evaluate what WOULD happen if shadow mode was off.
    settings.PREFILTER_SHADOW_MODE = False
    settings.PREFILTER_T1_LIVE = True
    settings.PREFILTER_T2_LIVE = True
    settings.PREFILTER_T3_LIVE = True

    for row in rows:
        msgs = [m for m in (row["messages"] or []) if m and m.get("body")]
        if not msgs:
            continue

        groq_clean = is_clean(row["red_flags"], invalid_patterns)
        groq_scores = {
            "compliance_score": row["compliance_score"] or 0,
            "sentiment_score": row["sentiment_score"] or 0,
            "professionalism_score": row["professionalism_score"] or 0,
            "script_adherence_score": row["script_adherence_score"] or 0,
        }

        result = run_prefilter(
            msgs,
            row["agent_name"],
            row["contact_name"],
            conversation_id=row["conversation_id"],
            db_pool=None,  # sync context — recording skips
        )

        if result is not None:
            # Short-circuited.
            model_used = result.get("model_used", "")
            tier = {"prefilter_t1": 1, "prefilter_t2": 2, "prefilter_t3": 3}.get(
                model_used, 0
            )
            tier_hits[tier] += 1
            tier_short_circuit[tier] += 1

            if groq_clean:
                correct_clean += 1
                # Compare predicted vs. groq scores
                for k in score_mae_total:
                    if result.get(k) is not None:
                        score_mae_total[k].append(abs(result[k] - groq_scores[k]))
            else:
                false_clean += 1
                logger.warning(
                    f"FALSE-CLEAN: convo {row['conversation_id']} "
                    f"({row['contact_name']}) — tier {tier} said clean, "
                    f"Groq flagged: {row['red_flags']}"
                )
        else:
            # Escalated to Groq.
            tier_hits[4] += 1
            tier_escalate[4] += 1
            if groq_clean:
                false_escalate += 1
            else:
                correct_escalate += 1

    # ── Report ──────────────────────────────────────────────────────
    total = sum(tier_hits.values())
    sc_total = sum(tier_short_circuit.values())

    # Compute rates
    fc_rate = (100 * false_clean / sc_total) if sc_total else 0.0
    savings_pct = (100 * sc_total / total) if total else 0.0

    # Per-tier savings
    t1_savings = (100 * tier_short_circuit[1] / total) if total else 0.0
    t12_savings = (100 * (tier_short_circuit[1] + tier_short_circuit[2]) / total) if total else 0.0
    t123_savings = savings_pct

    lines: list[str] = []
    def _p(s: str = ""):
        lines.append(s)
        print(s)

    _p()
    _p("=" * 68)
    _p(f"  PREFILTER SHADOW EVAL — {total} conversations")
    _p("=" * 68)
    _p(f"  Tier 1 short-circuits:  {tier_short_circuit[1]:>5}  "
       f"({100*tier_short_circuit[1]/total:.1f}%)")
    _p(f"  Tier 2 short-circuits:  {tier_short_circuit[2]:>5}  "
       f"({100*tier_short_circuit[2]/total:.1f}%)")
    _p(f"  Tier 3 short-circuits:  {tier_short_circuit[3]:>5}  "
       f"({100*tier_short_circuit[3]/total:.1f}%)")
    _p(f"  Escalated to Groq:      {tier_escalate[4]:>5}  "
       f"({100*tier_escalate[4]/total:.1f}%)")
    _p()
    if sc_total:
        _p(f"  Short-circuit accuracy: {correct_clean}/{sc_total}  "
           f"({100*correct_clean/sc_total:.1f}%)")
        _p(f"  FALSE-CLEAN (BAD):      {false_clean}/{sc_total}  "
           f"({fc_rate:.2f}%)   "
           f"<-- target <= 5%")
    esc_total = tier_escalate[4]
    if esc_total:
        _p(f"  Escalation precision:   {correct_escalate}/{esc_total}  "
           f"({100*correct_escalate/esc_total:.1f}%)")
        _p(f"  Wasted Groq calls:      {false_escalate}/{esc_total}  "
           f"({100*false_escalate/esc_total:.1f}%)")
    _p()
    _p("  Score MAE on short-circuited convos:")
    for k, vals in score_mae_total.items():
        if vals:
            mae = sum(vals) / len(vals)
            _p(f"    {k:<24} {mae:6.2f} (n={len(vals)})")
    _p("=" * 68)

    # Cost projection
    _p(f"\n  Projected Groq calls saved per {total} convos: "
       f"{sc_total} ({savings_pct:.0f}%)")
    _p()

    # ── Promotion Gate Checks ──────────────────────────────────────
    FC_GATE = 5.0       # ≤5% FALSE-CLEAN
    T1_SAVE_GATE = 5.0  # ≥5% savings for T1
    T2_SAVE_GATE = 10.0 # ≥10% savings for T1+T2
    T3_SAVE_GATE = 20.0 # ≥20% savings for T1+T2+T3

    gates_passed = True

    def _gate(tier_label: str, fc: float, fc_limit: float,
              save: float, save_limit: float) -> bool:
        fc_ok = fc <= fc_limit
        save_ok = save >= save_limit
        fc_tag = "PASS" if fc_ok else "FAIL"
        save_tag = "PASS" if save_ok else "FAIL"
        _p(f"  Gate Check -- {tier_label}:  "
           f"FALSE-CLEAN {fc:.1f}% <= {fc_limit:.1f}% -> {fc_tag}   |  "
           f"Savings {save:.1f}% >= {save_limit:.1f}% -> {save_tag}")
        return fc_ok and save_ok

    _p()
    _p("-" * 68)
    g1 = _gate("Tier 1", fc_rate, FC_GATE, t1_savings, T1_SAVE_GATE)
    g2 = _gate("Tier 2", fc_rate, FC_GATE, t12_savings, T2_SAVE_GATE)
    g3 = _gate("Tier 3", fc_rate, FC_GATE, t123_savings, T3_SAVE_GATE)
    _p("-" * 68)
    _p()

    if not (g1 and g2 and g3):
        gates_passed = False

    # ── Write markdown report if requested ─────────────────────────
    if output_md:
        import datetime
        out_path = Path(output_md)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Prefilter Eval — {datetime.date.today().isoformat()}\n\n"
            f"**Conversations evaluated**: {total}  \n"
            f"**FALSE-CLEAN rate**: {fc_rate:.2f}%  \n"
            f"**Projected Groq savings**: {savings_pct:.0f}%  \n\n"
            f"```\n"
        )
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(header)
            for line in lines:
                f.write(line + "\n")
            f.write("```\n")
        logger.info(f"Report written → {out_path}")

    return gates_passed


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Shadow-eval the prefilter.")
    p.add_argument("--limit", type=int, default=500,
                   help="Max number of past convos to replay.")
    p.add_argument("--since", default=None,
                   help="ISO date — only eval convos scored on/after this date.")
    p.add_argument("--output-md", default=None,
                   help="Write report to this markdown file path.")
    args = p.parse_args()
    passed = evaluate(limit=args.limit, since=args.since, output_md=args.output_md)
    sys.exit(0 if passed else 1)
