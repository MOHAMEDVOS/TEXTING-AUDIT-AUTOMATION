"""
Backfill ownership periods from the legacy day-grained account_assignments
table, then attribute existing messages to whoever owned the account.

Before mid-day shuffles existed, the only thing recorded was "account X belonged
to texter Y on day D". The honest conversion of that is a period covering the
whole local day. Consecutive days with the same owner merge into ONE period, so
a texter who kept an account for a week produces one row rather than seven.

Runs are idempotent: a period is only created where nothing already covers that
day, so real recorded shuffles are never overwritten.

Usage:
    python scripts/backfill_assignment_periods.py --dry-run
    python scripts/backfill_assignment_periods.py
    python scripts/backfill_assignment_periods.py --reattribute-only
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from config.settings import DATABASE_URL, TIMEZONE, get_now  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_periods")

_OUTGOING_EXCLUDED = ("contact", "lead", "unknown")


def _day_start(day) -> datetime:
    return TIMEZONE.localize(datetime.combine(day, time.min))


def _build_runs(rows: list) -> list[dict]:
    """Collapse (account, day, texter) rows into consecutive same-owner runs."""
    runs: list[dict] = []
    for r in rows:
        email, day, texter = r["account_email"], r["assigned_date"], r["agent_name"]
        if not email or not texter:
            continue
        prev = runs[-1] if runs else None
        contiguous = (
            prev is not None
            and prev["account_email"] == email
            and prev["texter_name"] == texter
            and prev["last_day"] + timedelta(days=1) == day
        )
        if contiguous:
            prev["last_day"] = day
        else:
            runs.append({
                "account_email": email,
                "texter_name":   texter,
                "first_day":     day,
                "last_day":      day,
            })
    return runs


async def backfill_periods(conn, *, dry_run: bool) -> tuple[int, int]:
    rows = await conn.fetch(
        """SELECT account_email, agent_name, assigned_date
             FROM account_assignments
            WHERE account_email IS NOT NULL AND agent_name IS NOT NULL
            ORDER BY account_email, assigned_date"""
    )
    runs = _build_runs(rows)
    logger.info("Legacy rows: %d → %d ownership runs", len(rows), len(runs))

    today = get_now().date()
    created = skipped = 0

    for run in runs:
        start = _day_start(run["first_day"])
        # A run that reaches today is still current — leave it open so the next
        # shuffle closes it, instead of inventing an end at tonight's midnight.
        end = None if run["last_day"] >= today else _day_start(run["last_day"] + timedelta(days=1))

        covered = await conn.fetchval(
            """SELECT EXISTS (
                   SELECT 1 FROM assignment_periods
                    WHERE LOWER(account_email) = LOWER($1)
                      AND period && tstzrange($2, $3, '[)'))""",
            run["account_email"], start, end,
        )
        if covered:
            skipped += 1
            continue

        if dry_run:
            created += 1
            logger.info(
                "  would create %s → %s  [%s .. %s]",
                run["account_email"], run["texter_name"],
                start.isoformat(), end.isoformat() if end else "open",
            )
            continue

        period_id = await conn.fetchval(
            """INSERT INTO assignment_periods
                   (account_email, texter_name, started_at, ended_at,
                    started_by, source)
               VALUES ($1, $2, $3, $4, 'backfill', 'backfill')
               RETURNING id""",
            run["account_email"], run["texter_name"], start, end,
        )
        await conn.execute(
            """INSERT INTO assignment_audit_log
                   (period_id, account_email, action, from_texter, to_texter,
                    effective_at, performed_by, reason)
               VALUES ($1, $2, 'backfill', NULL, $3, $4, 'backfill',
                       'Reconstructed from account_assignments')""",
            period_id, run["account_email"], run["texter_name"], start,
        )
        created += 1

    return created, skipped


async def reattribute_messages(conn, *, dry_run: bool) -> dict[str, int]:
    """
    Stamp every outgoing message with the texter who owned its account at
    sent_at. Inbound messages stay NULL — they are not anyone's work product.
    """
    if dry_run:
        counts = {}
        counts["exact"] = await conn.fetchval(
            f"""SELECT COUNT(*)
                  FROM messages m
                  JOIN conversations c ON c.id = m.conversation_id
                  JOIN accounts a      ON a.id = c.agent_id
                  JOIN assignment_periods p
                    ON LOWER(p.account_email) = LOWER(a.email)
                   AND p.period @> m.sent_at
                 WHERE m.sent_at IS NOT NULL
                   AND LOWER(m.sender) NOT IN {_OUTGOING_EXCLUDED}"""
        )
        counts["no_timestamp"] = await conn.fetchval(
            f"""SELECT COUNT(*) FROM messages
                 WHERE sent_at IS NULL
                   AND LOWER(sender) NOT IN {_OUTGOING_EXCLUDED}"""
        )
        return counts

    exact = await conn.execute(
        f"""UPDATE messages m
               SET texter_name = p.texter_name,
                   attribution = 'exact'
              FROM conversations c
              JOIN accounts a ON a.id = c.agent_id
              JOIN assignment_periods p
                ON LOWER(p.account_email) = LOWER(a.email)
             WHERE m.conversation_id = c.id
               AND m.sent_at IS NOT NULL
               AND p.period @> m.sent_at
               AND LOWER(m.sender) NOT IN {_OUTGOING_EXCLUDED}"""
    )

    # Timestamped, but nobody owned the account then — say so rather than
    # blaming the nearest texter.
    unassigned = await conn.execute(
        f"""UPDATE messages m
               SET texter_name = NULL,
                   attribution = 'unassigned'
             WHERE m.sent_at IS NOT NULL
               AND m.attribution IS NULL
               AND LOWER(m.sender) NOT IN {_OUTGOING_EXCLUDED}"""
    )

    # No usable timestamp — fall back to the conversation's owner, flagged so
    # the weaker evidence is visible downstream.
    inferred = await conn.execute(
        f"""UPDATE messages m
               SET texter_name = c.texter_name,
                   attribution = 'inferred'
              FROM conversations c
             WHERE m.conversation_id = c.id
               AND m.sent_at IS NULL
               AND m.attribution IS NULL
               AND LOWER(m.sender) NOT IN {_OUTGOING_EXCLUDED}"""
    )

    def _n(tag: str) -> int:
        return int(tag.split()[-1]) if tag else 0

    return {
        "exact":      _n(exact),
        "unassigned": _n(unassigned),
        "inferred":   _n(inferred),
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    ap.add_argument("--reattribute-only", action="store_true",
                    help="skip period creation; only re-stamp messages")
    ap.add_argument("--reset", action="store_true",
                    help="clear existing attribution first (full recompute after "
                         "a period correction)")
    ap.add_argument("--dsn", default=DATABASE_URL, help="database URL override")
    args = ap.parse_args()

    conn = await asyncpg.connect(args.dsn)
    try:
        if args.reset and not args.dry_run:
            cleared = await conn.execute(
                "UPDATE messages SET texter_name = NULL, attribution = NULL "
                "WHERE attribution IS NOT NULL"
            )
            logger.info("Cleared attribution on %s message(s)", cleared.split()[-1])

        if not args.reattribute_only:
            created, skipped = await backfill_periods(conn, dry_run=args.dry_run)
            logger.info("Periods: %d created, %d already covered", created, skipped)

        counts = await reattribute_messages(conn, dry_run=args.dry_run)
        logger.info("Messages: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))

        if args.dry_run:
            logger.info("Dry run — nothing written.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
