"""Single definition of the trend_snapshots counter recompute.

Both the dashboard (dashboard/app.py, on every Mark Valid click) and the
repair pass (scripts/repair_trend_counts.py) run this exact query, so the
two can never drift. They did drift once: scripts/backfill_trend_counts.py
kept the pre-migration-009 contact-name validation match and the scrape-day
date join long after app.py had moved to conversation_id and the
convo_date-preferring effective date, which made "repairing" a row zero it out.
"""
from ai.response_time import FLAG_TEXT as LATE_RESPONSE_FLAG_TEXT
from database.owner_sql import DAY_ASSIGNMENT_LATERAL, OWNER_EXPR, PERIOD_TEXTER_LATERAL

_EFFECTIVE_DATE = (
    "(CASE WHEN c.convo_date <> '' THEN TO_DATE(c.convo_date, 'MM/DD/YYYY') "
    "ELSE c.audit_date END)"
)

# Latest copy of every (account, contact, conversation day), tagged with the
# resolved owner. Built once per call and shared by every counter below, so the
# owner lookup (which scans messages) runs once, not once per counter per row.
# The day filter (date_param) is applied inside so a single-day recompute only resolves
# that day's conversations.
def _latest_convos(date_param: str) -> str:
    return f"""
    latest AS (
        SELECT DISTINCT ON (c.agent_id, c.contact_id, {_EFFECTIVE_DATE})
               c.contact_id, c.agent_id, cs.red_flags, c.convo_date, c.audit_date,
               {_EFFECTIVE_DATE} AS eff_date,
               {OWNER_EXPR} AS owner,
               a.email AS account_email
          FROM conversations c
          JOIN accounts a ON a.id = c.agent_id
          JOIN LATERAL (
              SELECT red_flags FROM conversation_scores cs2
               WHERE cs2.conversation_id = c.id
               ORDER BY cs2.id DESC LIMIT 1
          ) cs ON TRUE
          {PERIOD_TEXTER_LATERAL}
          {DAY_ASSIGNMENT_LATERAL}
         WHERE ({date_param}::date IS NULL OR {_EFFECTIVE_DATE} = {date_param}::date)
           -- Only a conversation with a Valid click can add to any counter, so
           -- skip the rest instead of resolving an owner for the whole day's
           -- thousands. The latest-copy pick below still sees every duplicate
           -- of a validated conversation, because the test is per contact+day.
           AND EXISTS (SELECT 1 FROM validation_log vl
                         JOIN conversations vc ON vc.id = vl.conversation_id
                        WHERE vl.agent_id = c.agent_id
                          AND vl.status = 'valid'
                          AND vc.agent_id   = c.agent_id
                          AND vc.contact_id = c.contact_id
                          AND (CASE WHEN vc.convo_date <> '' THEN TO_DATE(vc.convo_date, 'MM/DD/YYYY')
                                    ELSE vc.audit_date END) = {_EFFECTIVE_DATE})
         ORDER BY c.agent_id, c.contact_id, {_EFFECTIVE_DATE}, c.id DESC
    )
"""

# `lc` belongs to the trend row `ts` being filled in: same owner, same account,
# same conversation day.
_BELONGS_TO_ROW = """
    LOWER(lc.owner) = LOWER(ts.agent_name)
    AND lc.eff_date = ts.audit_date
    AND LOWER(lc.account_email) = LOWER(ts.account_email)
"""


def _validated(flag_expr: str | None) -> str:
    """EXISTS(...) — `lc` has a Valid click on its own conversation (or a same-day
    duplicate of it), optionally for one specific flag text."""
    flag_clause = (
        f"AND (vl.flag_text IS NULL OR validation_flag_key(vl.flag_text) = validation_flag_key({flag_expr}))"
        if flag_expr else ""
    )
    return f"""EXISTS (SELECT 1 FROM validation_log vl
                 JOIN conversations vc ON vc.id = vl.conversation_id
                 WHERE vl.agent_id = lc.agent_id
                   AND vl.status = 'valid'
                   AND vc.agent_id   = lc.agent_id
                   AND vc.contact_id = lc.contact_id
                   AND (CASE WHEN vc.convo_date <> '' THEN TO_DATE(vc.convo_date, 'MM/DD/YYYY')
                             ELSE vc.audit_date END) = lc.eff_date
                   {flag_clause})"""


# A validated conversation whose owner has no trend row for that account+day
# (the owner was resolved to someone other than whoever the audit stamped) would
# have nowhere to be counted. Create the row; counts are filled in right after.
# conversations_analyzed starts at 0 on purpose: it is the audit run's own tally,
# recorded under the stamped texter's row, and copying it here would count the
# same conversations twice in "Convos audited".
_ENSURE_ROWS_SQL = f"""
    WITH {_latest_convos('$1')}
    INSERT INTO trend_snapshots
        (agent_name, audit_date, audit_timestamp, account_email,
         total_issues, conversations_analyzed)
    SELECT g.owner, g.eff_date, NOW(), g.account_email, 0, 0
      FROM (
          SELECT lc.owner, lc.eff_date, lc.account_email,
                 BOOL_OR({_validated(None)}) AS any_validated
            FROM latest lc
           WHERE COALESCE(lc.owner, '') <> ''
           GROUP BY lc.owner, lc.eff_date, lc.account_email
      ) g
     WHERE g.any_validated
       AND NOT EXISTS (SELECT 1 FROM trend_snapshots t
                        WHERE LOWER(t.agent_name) = LOWER(g.owner)
                          AND t.audit_date = g.eff_date
                          AND LOWER(t.account_email) = LOWER(g.account_email))
    ON CONFLICT DO NOTHING
"""

_UPDATE_COUNTS_SQL = f"""
    WITH {_latest_convos('$3')}
    UPDATE trend_snapshots ts
       SET total_issues = (
               -- One per CONVERSATION that has at least one confirmed flag. A
               -- conversation with two confirmed flags is still one issue, so
               -- this equals the number of cards the Detailed Dashboard shows.
               -- (The Late Response / Wrong Label columns are per-flag-type and
               -- can overlap, so they may add up to more than this.)
               SELECT COUNT(*)
                 FROM latest lc
                WHERE {_BELONGS_TO_ROW}
                  AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(lc.red_flags::jsonb) ft
                               WHERE {_validated("ft")})
           ),
           late_response_flags = (
               SELECT COUNT(*)
                 FROM latest lc
                WHERE {_BELONGS_TO_ROW}
                  AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(lc.red_flags::jsonb) ft
                               WHERE LOWER(ft) = LOWER($2))
                  -- This specific flag must itself be validated, not just any
                  -- flag on the conversation (or covered by a legacy blanket row).
                  AND {_validated("$2")}
           ),
           wrong_label_flags = (
               SELECT COUNT(*)
                 FROM latest lc
                WHERE {_BELONGS_TO_ROW}
                  -- The wrong-label text is dynamic per conversation ("assigned 'X'
                  -- but should be 'Y'"), so the validated check is nested against
                  -- the SAME element, not a separate whole-conversation gate.
                  AND EXISTS (
                      SELECT 1 FROM jsonb_array_elements_text(lc.red_flags::jsonb) ft
                       WHERE LOWER(ft) LIKE 'wrong label:%'
                         AND {_validated("ft")}
                  )
           )
     WHERE ($1::text IS NULL OR LOWER(ts.agent_name) = LOWER($1))
       AND ($3::date IS NULL OR ts.audit_date = $3::date)
"""




async def recompute_trend_counts(conn, agent_name: str | None = None, audit_date=None) -> None:
    """Recompute a trend snapshot's issue counters from live conversation_scores.

    This is the mechanism that makes an agent's numbers climb as the auditor
    works: a freshly-saved snapshot records 0 issues, and every Valid click
    re-runs this to fold the newly-confirmed conversation in.

    Every counter is gated on validation_log — a conversation contributes
    nothing until an auditor explicitly confirmed it.

    WHO a conversation counts for is the RESOLVED owner (assignment period ->
    day assignment -> stamped texter_name; database/owner_sql.py), the same one
    the Detailed Dashboard shows and filters by. It is not the stamped
    `conversations.texter_name` alone: the department audits a day behind, so a
    conversation is scraped under the NEXT day's texter and the stamp can name
    someone who never held the account that day. Counting by the stamp put a
    validated flag on a different person's trend row than the one the auditor
    clicked it under. Because the owner is resolved per conversation, a call for
    one agent still scans the whole day; pass `agent_name` only to narrow which
    rows get written. Mark Valid therefore passes None (every agent, that day)
    so the row the flag LEAVES is refreshed along with the row it lands on.

    If the resolved owner has no trend row for that account and day, one is
    created (see _ENSURE_ROWS_SQL) — an UPDATE alone would silently drop the
    count.

    A validation row is matched through its own `conversation_id`, never
    through `contact_name`. Name matching made one click count for every
    conversation the account ever had with that contact — a returning contact
    on a later day, and (because account ownership moves between texters) often
    a texter the auditor had never reviewed. The match deliberately widens to
    the duplicate rows for the SAME contact and SAME conversation day, since
    re-running an audit appends a second copy of one real conversation and the
    click may have landed on either copy.

    Pass `audit_date` to target the snapshot for that specific day. Without it
    this recomputes every snapshot (all dates) — that is the repair path
    (scripts/repair_trend_counts.py) and is heavy.

    `trend_snapshots.audit_date` means the conversation's real date (SmarterContact's
    own `convo_date`, MM/DD/YYYY, falling back to the scrape day when blank) —
    NOT the day the audit script happened to run, so every date match uses the
    same CASE expression the Detailed Dashboard filters by.

    A texter can be assigned a different SmarterContact account each day (and
    in rare cases more than one on the same day), which produces multiple
    trend_snapshots rows for the same (agent_name, audit_date) — one per
    account_email. Each row's counters reflect only ITS OWN account's
    conversations; do not drop the account match in _BELONGS_TO_ROW.

    Every counter reads one row per (account, contact, conversation day) — the
    highest `conversations.id`, i.e. the most recently scraped copy. Before the
    dedup cache fix (deep review F7, ai/scorer.py), re-running an audit for the
    same agent+account+day re-inserted a full new `conversations` row per
    contact, so counting raw rows inflated every counter; collapsing to the
    latest copy restores the true, one-flag-per-contact count.
    """
    await conn.execute(_ENSURE_ROWS_SQL, audit_date)
    await conn.execute(_UPDATE_COUNTS_SQL, agent_name, LATE_RESPONSE_FLAG_TEXT, audit_date)
