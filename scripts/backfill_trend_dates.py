"""
One-time migration: re-key trend_snapshots.audit_date from "day the audit
script ran" to "the conversation's real date" (SmarterContact's own
convo_date) -- the same convention dashboard/app.py's INSERT paths and
_recompute_trend_counts, and the Detailed Dashboard, now all use.

Why this is needed: the department audits a day behind -- today's run covers
yesterday's conversations. trend_snapshots.audit_date was always set to
get_now().date() at scoring time (the scrape day), one day ahead of the
conversations it actually covers. The write paths and _recompute_trend_counts
were fixed to key snapshots by the conversation's real date going forward;
this script moves every EXISTING row onto the same convention. Skipping this
would silently break _recompute_trend_counts on old rows: it now matches
conversations by their real date, so a row still keyed by scrape date would
match nothing and get zeroed out the next time an auditor clicks Valid.

For each row:
  1. Find its own account (by account_email) and look at the conversations
     that account had for this texter on the row's stored audit_date (the
     OLD, scrape-day meaning).
  2. Compute the dominant real conversation date among them (same CASE
     expression used everywhere else: convo_date parsed as MM/DD/YYYY,
     falling back to audit_date when blank).
  3. If that differs from the row's stored audit_date, the row needs to move.
     If nothing already exists at (agent_name, new_date, account_email), it's
     a plain UPDATE. If a row already exists there (two adjacent runs landed
     on the same real conversation day), the two are merged: conversations_
     analyzed sums, score columns become a conversations_analyzed-weighted
     average (same convention the Trends UI already uses for grouped rows),
     and the now-redundant row is deleted. total_issues/late_response_flags/
     wrong_label_flags are NOT merged by arithmetic -- every touched
     (agent_name, date) gets a full _recompute_trend_counts pass after this
     script finishes, which derives those three purely from conversations +
     validation_log, not from stored history.
  4. Rows whose conversations can't be found at all (e.g. reset/deleted
     data) are left untouched and reported, never guessed at.

Idempotent: safe to re-run -- a row already on the correct date is a no-op,
and re-running after a partial run just finds fewer rows left to move.

Usage:
    python scripts/backfill_trend_dates.py --dry-run
    python scripts/backfill_trend_dates.py
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Captured before importing config.settings, which calls
# load_dotenv(..., override=True) and would otherwise silently clobber a
# DATABASE_URL passed on the command line with this machine's local .env.
_env_database_url = os.environ.get("DATABASE_URL")

import asyncpg  # noqa: E402

from config.settings import DATABASE_URL as _DOTENV_DATABASE_URL  # noqa: E402
from ai.response_time import FLAG_TEXT as LATE_RESPONSE_FLAG_TEXT  # noqa: E402

DATABASE_URL = _env_database_url or _DOTENV_DATABASE_URL

# Mirrors dashboard/app.py::_recompute_trend_counts exactly (kept as a plain
# copy rather than importing dashboard/app.py, which would pull in the whole
# FastAPI app and its startup side effects for a one-time migration script).
_RECOMPUTE_SQL = """
    UPDATE trend_snapshots ts
    SET total_issues = (
        SELECT COALESCE(SUM(jsonb_array_length(lc.red_flags::jsonb)), 0)
        FROM (
            SELECT DISTINCT ON (c.contact_id) c.contact_id, c.agent_id, cs.red_flags
            FROM conversations c
            JOIN accounts a ON a.id = c.agent_id
            JOIN LATERAL (
                SELECT red_flags FROM conversation_scores cs2
                WHERE cs2.conversation_id = c.id
                ORDER BY cs2.id DESC LIMIT 1
            ) cs ON TRUE
            WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
              AND (CASE WHEN c.convo_date <> '' THEN TO_DATE(c.convo_date, 'MM/DD/YYYY')
                        ELSE c.audit_date END) = ts.audit_date
              AND LOWER(a.email) = LOWER(ts.account_email)
            ORDER BY c.contact_id, c.id DESC
        ) lc
        WHERE jsonb_array_length(lc.red_flags::jsonb) > 0
          AND EXISTS (SELECT 1 FROM validation_log vl, contacts ct2
                      WHERE ct2.id = lc.contact_id
                        AND vl.agent_id = lc.agent_id
                        AND LOWER(vl.contact_name) = LOWER(ct2.name)
                        AND vl.status = 'valid')
    ),
    late_response_flags = (
        SELECT COUNT(*)
        FROM (
            SELECT DISTINCT ON (c.contact_id) c.contact_id, c.agent_id, cs.red_flags
            FROM conversations c
            JOIN accounts a ON a.id = c.agent_id
            JOIN LATERAL (
                SELECT red_flags FROM conversation_scores cs2
                WHERE cs2.conversation_id = c.id
                ORDER BY cs2.id DESC LIMIT 1
            ) cs ON TRUE
            WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
              AND (CASE WHEN c.convo_date <> '' THEN TO_DATE(c.convo_date, 'MM/DD/YYYY')
                        ELSE c.audit_date END) = ts.audit_date
              AND LOWER(a.email) = LOWER(ts.account_email)
            ORDER BY c.contact_id, c.id DESC
        ) lc
        WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(lc.red_flags::jsonb) ft
                      WHERE LOWER(ft) = LOWER($2))
          AND EXISTS (SELECT 1 FROM validation_log vl, contacts ct2
                      WHERE ct2.id = lc.contact_id
                        AND vl.agent_id = lc.agent_id
                        AND LOWER(vl.contact_name) = LOWER(ct2.name)
                        AND vl.status = 'valid')
    ),
    wrong_label_flags = (
        SELECT COUNT(*)
        FROM (
            SELECT DISTINCT ON (c.contact_id) c.contact_id, c.agent_id, cs.red_flags
            FROM conversations c
            JOIN accounts a ON a.id = c.agent_id
            JOIN LATERAL (
                SELECT red_flags FROM conversation_scores cs2
                WHERE cs2.conversation_id = c.id
                ORDER BY cs2.id DESC LIMIT 1
            ) cs ON TRUE
            WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
              AND (CASE WHEN c.convo_date <> '' THEN TO_DATE(c.convo_date, 'MM/DD/YYYY')
                        ELSE c.audit_date END) = ts.audit_date
              AND LOWER(a.email) = LOWER(ts.account_email)
            ORDER BY c.contact_id, c.id DESC
        ) lc
        WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(lc.red_flags::jsonb) ft
                      WHERE LOWER(ft) LIKE 'wrong label:%')
          AND EXISTS (SELECT 1 FROM validation_log vl, contacts ct2
                      WHERE ct2.id = lc.contact_id
                        AND vl.agent_id = lc.agent_id
                        AND LOWER(vl.contact_name) = LOWER(ct2.name)
                        AND vl.status = 'valid')
    )
    WHERE LOWER(ts.agent_name) = LOWER($1)
      AND ($3::date IS NULL OR ts.audit_date = $3::date)
"""


async def _recompute_trend_counts(conn, agent_name: str, audit_date=None) -> None:
    await conn.execute(_RECOMPUTE_SQL, agent_name, LATE_RESPONSE_FLAG_TEXT, audit_date)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_trend_dates")

# One round trip for every row's dominant effective date, instead of two
# per-row queries each (account lookup + date lookup) -- the earlier
# row-by-row version took 18+ minutes against this database's proxy
# (network round-trip latency dominates, not computation); this does the
# same work as a single JOIN, matching how every other fix in this session
# already does bulk work.
_BULK_EFFECTIVE_DATE_SQL = """
    SELECT ts.id, ts.agent_name, ts.audit_date, ts.account_email,
           ts.conversations_analyzed, ts.overall_score, ts.compliance_score,
           ts.sentiment_score, ts.professionalism_score, ts.script_adherence_score,
           dist.effective_date
    FROM trend_snapshots ts
    LEFT JOIN LATERAL (
        SELECT (CASE WHEN c.convo_date <> '' THEN TO_DATE(c.convo_date, 'MM/DD/YYYY')
                     ELSE c.audit_date END) AS effective_date
        FROM conversations c
        JOIN accounts a ON a.id = c.agent_id
        WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
          AND LOWER(a.email) = LOWER(ts.account_email)
          AND c.audit_date = ts.audit_date
        GROUP BY 1
        ORDER BY COUNT(*) DESC, effective_date DESC
        LIMIT 1
    ) dist ON TRUE
    ORDER BY ts.agent_name, ts.audit_date
"""


async def main(dry_run: bool) -> None:
    host = DATABASE_URL.split("@")[-1].split("/")[0] if "@" in DATABASE_URL else DATABASE_URL
    logger.info(f"Connecting to {host} (from {'$DATABASE_URL' if _env_database_url else '.env'})")

    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    try:
        total_ts = await conn.fetchval("SELECT COUNT(*) FROM trend_snapshots")
        rows = await conn.fetch(_BULK_EFFECTIVE_DATE_SQL)
        if len(rows) != total_ts:
            logger.error(
                f"Aborting: fetched {len(rows)} row(s) but COUNT(*) says {total_ts}. "
                "Not trusting a partial result -- re-run."
            )
            return
        logger.info(f"Sanity check passed: {total_ts} trend_snapshots rows accounted for.")

        _SCORE_KEYS = ("overall_score", "compliance_score", "sentiment_score",
                       "professionalism_score", "script_adherence_score")

        def _combine_score(a_v, a_n, b_v, b_n):
            """Weighted average by conversations_analyzed, falling back to a
            plain average when neither side has a count to weight by -- same
            convention the Trends UI uses for grouped Snapshot Records rows."""
            if a_v is None and b_v is None:
                return None
            if a_v is None:
                return b_v
            if b_v is None:
                return a_v
            if (a_n + b_n) > 0:
                return (a_v * a_n + b_v * b_n) / (a_n + b_n)
            return (a_v + b_v) / 2

        unchanged = 0
        no_conversations_found = 0

        # Group EVERY row (not just the ones that need to move) by its target
        # identity. A row already sitting at its own target is a candidate
        # "keeper" for its group -- covers both "collides with an existing
        # correctly-dated row" and "N rows all moving into a date none of them
        # started at" (e.g. a Thu and a Fri run that both only found Wednesday's
        # leftover conversations) with the same logic, for any group size.
        by_target: dict[tuple, list] = {}
        for r in rows:
            new_date = r["effective_date"]
            if new_date is None:
                no_conversations_found += 1
                logger.warning(
                    f"  no matching conversations for '{r['account_email']}' "
                    f"(row {r['id']}, {r['agent_name']} | {r['audit_date']}) -- skipping"
                )
                continue
            key = (r["agent_name"].lower(), new_date, (r["account_email"] or "").lower())
            by_target.setdefault(key, []).append(dict(r))

        moves: list[dict] = []
        merge_groups: list[dict] = []

        for (agent_lower, new_date, email_lower), members in by_target.items():
            if len(members) == 1:
                m = members[0]
                if m["audit_date"] == new_date:
                    unchanged += 1
                else:
                    moves.append({
                        "id": m["id"], "agent_name": m["agent_name"], "account_email": m["account_email"],
                        "old_date": m["audit_date"], "new_date": new_date,
                    })
                continue

            # Multi-row target: prefer the member already sitting on the
            # correct date as keeper (no audit_date UPDATE needed for it),
            # else the one with the most conversations analyzed.
            keeper = next((m for m in members if m["audit_date"] == new_date), None)
            if keeper is None:
                keeper = max(members, key=lambda m: (m["conversations_analyzed"] or 0, -m["id"]))
            absorbed = [m for m in members if m["id"] != keeper["id"]]

            merged_scores = {k: keeper[k] for k in _SCORE_KEYS}
            running_n = keeper["conversations_analyzed"] or 0
            for m in absorbed:
                b_n = m["conversations_analyzed"] or 0
                for k in _SCORE_KEYS:
                    merged_scores[k] = _combine_score(merged_scores[k], running_n, m[k], b_n)
                running_n += b_n

            merge_groups.append({
                "keeper_id": keeper["id"], "agent_name": keeper["agent_name"],
                "account_email": keeper["account_email"],
                "keeper_old_date": keeper["audit_date"], "new_date": new_date,
                "absorbed_ids": [m["id"] for m in absorbed],
                "absorbed_old_dates": [m["audit_date"] for m in absorbed],
                "total_conversations_analyzed": running_n,
                "merged_scores": merged_scores,
            })

        logger.info(
            f"{len(rows)} total | {unchanged} already correct | "
            f"{len(moves)} simple re-date(s) | {len(merge_groups)} merge group(s) "
            f"({sum(len(g['absorbed_ids']) for g in merge_groups)} row(s) absorbed) | "
            f"{no_conversations_found} skipped (no matching conversations)"
        )
        for m in moves:
            logger.info(f"  MOVE  {m['agent_name']} | {m['account_email']}: {m['old_date']} -> {m['new_date']}")
        for g in merge_groups:
            logger.info(
                f"  MERGE {g['agent_name']} | {g['account_email']}: "
                f"{g['absorbed_old_dates']} + keeper {g['keeper_old_date']} (row {g['keeper_id']}) "
                f"-> {g['new_date']} ({g['total_conversations_analyzed']} convos total)"
            )

        if dry_run:
            logger.info("Dry run -- no changes written.")
            return

        touched_targets: set[tuple] = set()

        # Dates only ever shift BACKWARD (a conversation's real date can't be
        # later than the day it was scraped), and every row's original date is
        # already unique per (agent_name, account_email). So whatever row
        # currently occupies a given target slot must have an original date
        # strictly earlier than the row moving into it. Applying every
        # UPDATE/DELETE in ascending order of the row's OWN original date
        # guarantees each target slot is already vacated by the time
        # something else claims it -- out of order, a move can land on a slot
        # another row hasn't vacated yet and hit the unique constraint even
        # though the FINAL state has no real collision (confirmed against
        # production: Ahmed Mohammed Saad Youseef's Aug 6 row moving into
        # Aug 5 while the ORIGINAL Aug 5 row hadn't yet moved to Aug 4).
        ops = []
        for m in moves:
            ops.append((m["old_date"], "move", m))
        for g in merge_groups:
            for absorbed_id, absorbed_old_date in zip(g["absorbed_ids"], g["absorbed_old_dates"]):
                ops.append((absorbed_old_date, "delete", absorbed_id))
            ops.append((g["keeper_old_date"], "merge_update", g))
        ops.sort(key=lambda o: o[0])

        async with conn.transaction():
            for _, kind, payload in ops:
                if kind == "move":
                    m = payload
                    await conn.execute(
                        "UPDATE trend_snapshots SET audit_date = $1 WHERE id = $2",
                        m["new_date"], m["id"],
                    )
                    touched_targets.add((m["agent_name"], m["new_date"]))
                elif kind == "delete":
                    await conn.execute("DELETE FROM trend_snapshots WHERE id = $1", payload)
                elif kind == "merge_update":
                    g = payload
                    s = g["merged_scores"]
                    await conn.execute(
                        """UPDATE trend_snapshots
                           SET audit_date = $1, conversations_analyzed = $2, overall_score = $3,
                               compliance_score = $4, sentiment_score = $5, professionalism_score = $6,
                               script_adherence_score = $7
                           WHERE id = $8""",
                        g["new_date"], g["total_conversations_analyzed"], s["overall_score"],
                        s["compliance_score"], s["sentiment_score"], s["professionalism_score"],
                        s["script_adherence_score"], g["keeper_id"],
                    )
                    touched_targets.add((g["agent_name"], g["new_date"]))

        logger.info(f"Applied {len(moves)} move(s) and {len(merge_groups)} merge group(s).")

        # Refresh the three flag-count columns for every (agent_name, date) this
        # script touched, now that _recompute_trend_counts matches by real
        # conversation date -- the moved/merged rows' total_issues/late_
        # response_flags/wrong_label_flags reflect their OLD identity until this runs.
        for agent_name, date_ in touched_targets:
            await _recompute_trend_counts(conn, agent_name, date_)
        logger.info(f"Recomputed flag counts for {len(touched_targets)} touched (agent, date) pair(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing them")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
