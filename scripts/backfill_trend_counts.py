"""
One-time backfill: recompute every trend_snapshots row's total_issues,
late_response_flags, and wrong_label_flags with the corrected, per-account
scoped logic now in dashboard/app.py::_recompute_trend_counts.

Why this was needed: for a texter who worked more than one SmarterContact
account on the same day (multiple trend_snapshots rows sharing one
agent_name + audit_date, one row per account_email), the old recompute
query matched conversations by texter_name + audit_date only — with no
account_email filter — and then wrote that agent-wide combined total into
whichever single row happened to have the highest id. Every other account
row for that same day was left stale, sometimes showing flag counts far
exceeding its own conversations_analyzed (e.g. wrong_label_flags=405 on a
300-conversation row). This script re-runs the fixed, account-scoped
version of the recompute against every existing row so historical data
matches what new recomputes now produce.

Idempotent: safe to re-run; each row's counters are simply overwritten
with the current correct value.

Usage:
    python scripts/backfill_trend_counts.py --dry-run
    python scripts/backfill_trend_counts.py
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from config.settings import DATABASE_URL  # noqa: E402
from ai.response_time import FLAG_TEXT as LATE_RESPONSE_FLAG_TEXT  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_trend_counts")

# Shared with dashboard/app.py::_recompute_trend_counts — keep in sync.
_COUNT_CTE = """
WITH recomputed AS (
    SELECT
        ts.id,
        ts.agent_name,
        ts.audit_date,
        ts.account_email,
        ts.total_issues            AS old_total_issues,
        ts.late_response_flags     AS old_late_response_flags,
        ts.wrong_label_flags       AS old_wrong_label_flags,
        (SELECT COALESCE(SUM(jsonb_array_length(cs.red_flags::jsonb)), 0)
         FROM conversations c
         JOIN accounts a ON a.id = c.agent_id
         JOIN LATERAL (
             SELECT red_flags FROM conversation_scores cs2
             WHERE cs2.conversation_id = c.id
             ORDER BY cs2.id DESC LIMIT 1
         ) cs ON TRUE
         WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
           AND c.audit_date = ts.audit_date
           AND LOWER(a.email) = LOWER(ts.account_email)
           AND jsonb_array_length(cs.red_flags::jsonb) > 0
           AND EXISTS (SELECT 1 FROM validation_log vl, contacts ct2
                       WHERE ct2.id = c.contact_id
                         AND vl.agent_id = c.agent_id
                         AND LOWER(vl.contact_name) = LOWER(ct2.name)
                         AND vl.status = 'valid')
        ) AS new_total_issues,
        (SELECT COUNT(*)
         FROM conversations c
         JOIN accounts a ON a.id = c.agent_id
         JOIN LATERAL (
             SELECT red_flags FROM conversation_scores cs2
             WHERE cs2.conversation_id = c.id
             ORDER BY cs2.id DESC LIMIT 1
         ) cs ON TRUE
         WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
           AND c.audit_date = ts.audit_date
           AND LOWER(a.email) = LOWER(ts.account_email)
           AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(cs.red_flags::jsonb) ft
                       WHERE LOWER(ft) = LOWER($1))
           AND EXISTS (SELECT 1 FROM validation_log vl, contacts ct2
                       WHERE ct2.id = c.contact_id
                         AND vl.agent_id = c.agent_id
                         AND LOWER(vl.contact_name) = LOWER(ct2.name)
                         AND vl.status = 'valid')
        ) AS new_late_response_flags,
        (SELECT COUNT(*)
         FROM conversations c
         JOIN accounts a ON a.id = c.agent_id
         JOIN LATERAL (
             SELECT red_flags FROM conversation_scores cs2
             WHERE cs2.conversation_id = c.id
             ORDER BY cs2.id DESC LIMIT 1
         ) cs ON TRUE
         WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
           AND c.audit_date = ts.audit_date
           AND LOWER(a.email) = LOWER(ts.account_email)
           AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(cs.red_flags::jsonb) ft
                       WHERE LOWER(ft) LIKE 'wrong label:%')
           AND EXISTS (SELECT 1 FROM validation_log vl, contacts ct2
                       WHERE ct2.id = c.contact_id
                         AND vl.agent_id = c.agent_id
                         AND LOWER(vl.contact_name) = LOWER(ct2.name)
                         AND vl.status = 'valid')
        ) AS new_wrong_label_flags
    FROM trend_snapshots ts
)
"""


async def main(dry_run: bool) -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            _COUNT_CTE + """
            SELECT * FROM recomputed
            WHERE old_total_issues        IS DISTINCT FROM new_total_issues
               OR old_late_response_flags IS DISTINCT FROM new_late_response_flags
               OR old_wrong_label_flags   IS DISTINCT FROM new_wrong_label_flags
            ORDER BY agent_name, audit_date
            """,
            LATE_RESPONSE_FLAG_TEXT,
        )

        if not rows:
            logger.info("No trend_snapshots rows need correction. Nothing to do.")
            return

        logger.info(f"{len(rows)} row(s) will change:")
        for r in rows:
            logger.info(
                f"  {r['agent_name']} | {r['audit_date']} | {r['account_email']}: "
                f"issues {r['old_total_issues']}->{r['new_total_issues']}, "
                f"late {r['old_late_response_flags']}->{r['new_late_response_flags']}, "
                f"wrong {r['old_wrong_label_flags']}->{r['new_wrong_label_flags']}"
            )

        if dry_run:
            logger.info("Dry run — no changes written.")
            return

        await conn.execute(
            _COUNT_CTE
            + """UPDATE trend_snapshots ts
               SET total_issues        = r.new_total_issues,
                   late_response_flags = r.new_late_response_flags,
                   wrong_label_flags   = r.new_wrong_label_flags
               FROM recomputed r
               WHERE r.id = ts.id""",
            LATE_RESPONSE_FLAG_TEXT,
        )
        logger.info(f"Updated {len(rows)} row(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing them")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
