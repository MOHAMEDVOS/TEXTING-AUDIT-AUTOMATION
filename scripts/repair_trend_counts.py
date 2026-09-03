"""
Repair pass: recompute every trend_snapshots row's validation-gated counters
(total_issues, late_response_flags, wrong_label_flags) with the canonical
query in database/trend_counts.py.

Why rows need repairing
-----------------------
Two writes put wrong numbers on these columns, and both are now fixed:

1. ai/scorer.py stamped total_issues with the RAW flag count from the audit
   run, un-gated by validation_log. The two flag columns stayed correctly at
   0, so the Trends table tinted rows red off un-validated audit output while
   the columns beside it showed validated output — the two disagreed by
   construction. The scorer now writes 0 and lets validation own the counters.

2. database/schema.sql tried to backfill the two flag columns on boot with a
   `c.audit_date = ts.audit_date` join. That is the SCRAPE day matched against
   a row keyed by the CONVERSATION day (the department audits a day behind),
   so it matched nothing, COALESCE wrote 0, and the `IS NULL` guard made that
   0 permanent. That block now only normalizes NULL to 0.

This script is what actually restores the true numbers on rows written before
those fixes.

Supersedes scripts/backfill_trend_counts.py, which was deleted: it still
carried the pre-migration-009 contact-NAME validation match and the same
scrape-day date join, so running it would have zeroed rows rather than
repaired them. Importing the shared query instead of copying it is the point —
that copy is exactly how the two drifted apart.

Idempotent: safe to re-run; each row's counters are overwritten with the
current correct value.

Usage:
    python scripts/repair_trend_counts.py --dry-run
    python scripts/repair_trend_counts.py
    DATABASE_URL=postgresql://... python scripts/repair_trend_counts.py --dry-run
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Captured BEFORE importing config.settings, which calls
# load_dotenv(..., override=True) and will otherwise silently clobber a
# DATABASE_URL passed on the command line (e.g. a production URL) with
# whatever this machine's local .env has. A caller-provided value always
# wins; only fall back to config.settings' value (the local .env) if the
# caller didn't set one.
_env_database_url = os.environ.get("DATABASE_URL")

import asyncpg  # noqa: E402

from config.settings import DATABASE_URL as _DOTENV_DATABASE_URL  # noqa: E402
from database.trend_counts import recompute_trend_counts  # noqa: E402

DATABASE_URL = _env_database_url or _DOTENV_DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("repair_trend_counts")

_SNAPSHOT_COLUMNS = """
    SELECT id, agent_name, audit_date, account_email,
           COALESCE(total_issues, 0)        AS total_issues,
           COALESCE(late_response_flags, 0) AS late_response_flags,
           COALESCE(wrong_label_flags, 0)   AS wrong_label_flags
      FROM trend_snapshots
     ORDER BY audit_date, agent_name, account_email
"""


async def _snapshot_counters(conn) -> dict[int, tuple[int, int, int]]:
    """Every row's three counters, keyed by trend_snapshots.id."""
    rows = await conn.fetch(_SNAPSHOT_COLUMNS)
    return {
        r["id"]: (r["total_issues"], r["late_response_flags"], r["wrong_label_flags"])
        for r in rows
    }


async def _row_labels(conn) -> dict[int, str]:
    rows = await conn.fetch(_SNAPSHOT_COLUMNS)
    return {
        r["id"]: f"{r['audit_date']}  {r['agent_name']}  <{r['account_email'] or '—'}>"
        for r in rows
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and roll back without committing.",
    )
    args = ap.parse_args()

    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set (checked the environment and .env)")
        return 2

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # One transaction for both modes. The dry run needs it to read the
        # post-recompute values back before rolling them away — computing the
        # diff any other way would mean a second copy of the query, which is
        # the mistake this script exists to undo.
        tx = conn.transaction()
        await tx.start()
        rolled_back = False
        try:
            labels = await _row_labels(conn)
            before = await _snapshot_counters(conn)
            if not before:
                logger.info("No trend_snapshots rows — nothing to repair.")
                await tx.rollback()
                return 0

            # agent_name=None sweeps every agent and every date in one UPDATE.
            await recompute_trend_counts(conn)
            after = await _snapshot_counters(conn)

            changed = [rid for rid, vals in after.items() if before.get(rid) != vals]
            for rid in changed:
                b, a = before.get(rid, (0, 0, 0)), after[rid]
                logger.info(
                    "%s | issues %s->%s, late %s->%s, wrong %s->%s",
                    labels.get(rid, f"id={rid}"),
                    b[0], a[0], b[1], a[1], b[2], a[2],
                )

            logger.info(
                "%d of %d snapshot row(s) would change." if args.dry_run
                else "%d of %d snapshot row(s) changed.",
                len(changed), len(before),
            )

            if args.dry_run:
                await tx.rollback()
                rolled_back = True
                logger.info("Dry run — rolled back, nothing was written.")
            else:
                await tx.commit()
                rolled_back = True
                logger.info("Committed.")
        except Exception:
            if not rolled_back:
                await tx.rollback()
            raise
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
