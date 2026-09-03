"""Single definition of the trend_snapshots counter recompute.

Both the dashboard (dashboard/app.py, on every Mark Valid click) and the
repair pass (scripts/repair_trend_counts.py) run this exact query, so the
two can never drift. They did drift once: scripts/backfill_trend_counts.py
kept the pre-migration-009 contact-name validation match and the scrape-day
date join long after app.py had moved to conversation_id and the
convo_date-preferring effective date, which made "repairing" a row zero it out.
"""
from ai.response_time import FLAG_TEXT as LATE_RESPONSE_FLAG_TEXT


async def recompute_trend_counts(conn, agent_name: str | None = None, audit_date=None) -> None:
    """Recompute a trend snapshot's issue counters from live conversation_scores.

    This is the mechanism that makes an agent's numbers climb as the auditor
    works: a freshly-saved snapshot records 0 issues, and every Valid click
    re-runs this to fold the newly-confirmed conversation in.

    Every counter is gated on validation_log — a conversation contributes
    nothing until an auditor explicitly confirmed it.

    A validation row is matched through its own `conversation_id`, never
    through `contact_name`. Name matching made one click count for every
    conversation the account ever had with that contact — a returning contact
    on a later day, and (because account ownership moves between texters) often
    a texter the auditor had never reviewed. The join deliberately widens to
    the duplicate rows for the SAME contact and SAME conversation day, since
    re-running an audit appends a second copy of one real conversation and the
    click may have landed on either copy.

    Pass `audit_date` to target the snapshot for that specific day. Without it
    this recomputes every snapshot the agent has (all dates). Pass
    `agent_name=None` to sweep every agent — that is the repair path
    (scripts/repair_trend_counts.py), not something a request should do.

    `trend_snapshots.audit_date` means the conversation's real date (SmarterContact's
    own `convo_date`, MM/DD/YYYY, falling back to the scrape day when blank) —
    NOT the day the audit script happened to run. The department audits a day
    behind (today's run covers yesterday's conversations), so every subquery's
    date match uses the same CASE expression the Detailed Dashboard filters by,
    never a bare `c.audit_date = ts.audit_date`. Matching on the scrape day
    would silently zero out every row once trend_snapshots rows are keyed by
    conversation day instead of scrape day.

    A texter can be assigned a different SmarterContact account each day (and
    in rare cases more than one on the same day), which produces multiple
    trend_snapshots rows for the same (agent_name, audit_date) — one per
    account_email. Every subquery below joins back to `accounts` and matches
    `ts.account_email`, so each row's counters reflect only ITS OWN account's
    conversations. Do not drop that join: without it, conversations from
    every account the texter ever touched that day get merged into each row,
    and — combined with an UPDATE that targets all such rows — every account
    row for that day ends up showing the same inflated, combined-across-
    accounts numbers instead of its own.

    Every subquery also collapses to one row per `contact_id` (the highest
    `conversations.id`, i.e. the most recently scraped copy) before counting.
    Before the dedup cache fix (deep review F7, ai/scorer.py), re-running an
    audit for the same agent+account+day re-inserted a full new `conversations`
    row per contact instead of reusing the existing one, so older dates can
    have several rows for the same contact — e.g. one account's 44 real
    conversations sitting alongside 600 raw rows in the table. Counting raw
    rows on such a day inflates every counter (a wrong-label flag on a
    duplicated contact gets counted once per duplicate); collapsing to the
    latest row per contact first restores the true, one-flag-per-contact count.
    """
    await conn.execute(
        """UPDATE trend_snapshots ts
           SET total_issues = (
               -- Per-flag now: count how many of THIS conversation's individual
               -- red_flags entries have their own matching validation_log row
               -- (or are covered by a legacy pre-flag_text blanket row), rather
               -- than gating the whole array's length on any validation existing.
               SELECT COALESCE(SUM(matched.cnt), 0)
               FROM (
                   SELECT DISTINCT ON (c.contact_id) c.contact_id, c.agent_id, cs.red_flags,
                          c.convo_date, c.audit_date
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
               CROSS JOIN LATERAL (
                   SELECT COUNT(*) AS cnt
                   FROM jsonb_array_elements_text(lc.red_flags::jsonb) ft
                   WHERE EXISTS (SELECT 1 FROM validation_log vl
                                 JOIN conversations vc ON vc.id = vl.conversation_id
                                 WHERE vl.agent_id = lc.agent_id
                                   AND vl.status = 'valid'
                                   AND vc.agent_id   = lc.agent_id
                                   AND vc.contact_id = lc.contact_id
                                   AND (CASE WHEN vc.convo_date <> '' THEN TO_DATE(vc.convo_date, 'MM/DD/YYYY')
                                             ELSE vc.audit_date END)
                                     = (CASE WHEN lc.convo_date <> '' THEN TO_DATE(lc.convo_date, 'MM/DD/YYYY')
                                             ELSE lc.audit_date END)
                                   AND (vl.flag_text IS NULL OR LOWER(vl.flag_text) = LOWER(ft)))
               ) matched
           ),
           late_response_flags = (
               SELECT COUNT(*)
               FROM (
                   SELECT DISTINCT ON (c.contact_id) c.contact_id, c.agent_id, cs.red_flags,
                          c.convo_date, c.audit_date
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
                 -- This specific flag must itself be validated, not just any
                 -- flag on the conversation (or covered by a legacy blanket row).
                 AND EXISTS (SELECT 1 FROM validation_log vl
                             JOIN conversations vc ON vc.id = vl.conversation_id
                             WHERE vl.agent_id = lc.agent_id
                               AND vl.status = 'valid'
                               AND vc.agent_id   = lc.agent_id
                               AND vc.contact_id = lc.contact_id
                               AND (CASE WHEN vc.convo_date <> '' THEN TO_DATE(vc.convo_date, 'MM/DD/YYYY')
                                         ELSE vc.audit_date END)
                                 = (CASE WHEN lc.convo_date <> '' THEN TO_DATE(lc.convo_date, 'MM/DD/YYYY')
                                         ELSE lc.audit_date END)
                               AND (vl.flag_text IS NULL OR LOWER(vl.flag_text) = LOWER($2)))
           ),
           wrong_label_flags = (
               SELECT COUNT(*)
               FROM (
                   SELECT DISTINCT ON (c.contact_id) c.contact_id, c.agent_id, cs.red_flags,
                          c.convo_date, c.audit_date
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
               -- The wrong-label text is dynamic per conversation ("assigned 'X'
               -- but should be 'Y'"), so the validated check has to be nested
               -- against the SAME element, not a separate whole-conversation gate.
               WHERE EXISTS (
                   SELECT 1 FROM jsonb_array_elements_text(lc.red_flags::jsonb) ft
                   WHERE LOWER(ft) LIKE 'wrong label:%'
                     AND EXISTS (SELECT 1 FROM validation_log vl
                                 JOIN conversations vc ON vc.id = vl.conversation_id
                                 WHERE vl.agent_id = lc.agent_id
                                   AND vl.status = 'valid'
                                   AND vc.agent_id   = lc.agent_id
                                   AND vc.contact_id = lc.contact_id
                                   AND (CASE WHEN vc.convo_date <> '' THEN TO_DATE(vc.convo_date, 'MM/DD/YYYY')
                                             ELSE vc.audit_date END)
                                     = (CASE WHEN lc.convo_date <> '' THEN TO_DATE(lc.convo_date, 'MM/DD/YYYY')
                                             ELSE lc.audit_date END)
                                   AND (vl.flag_text IS NULL OR LOWER(vl.flag_text) = LOWER(ft)))
               )
           )
           WHERE ($1::text IS NULL OR LOWER(ts.agent_name) = LOWER($1))
             AND ($3::date IS NULL OR ts.audit_date = $3::date)""",
        agent_name,
        LATE_RESPONSE_FLAG_TEXT,
        audit_date,
    )
