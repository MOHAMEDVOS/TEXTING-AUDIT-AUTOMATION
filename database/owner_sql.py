"""SQL fragments that decide WHO a conversation belongs to.

One definition, shared by the dashboard views and the trend counters. They used
to disagree: the views resolved the owner from assignment periods / day
assignments, while the trend counters matched the `conversations.texter_name`
column stamped at scrape time. A conversation scraped the day after it happened
(the department audits a day behind) got stamped with the NEW day's texter, so
a flag validated on the page under texter A was charged to texter B in the
trend table.

All fragments need `c` (conversations) in scope.
"""

# Inbound messages are nobody's work product, so they never carry a texter.
OUTGOING_EXCLUDED_SQL = "('contact', 'lead', 'unknown')"

# Time-ranged ownership resolved at the conversation's last outgoing message.
# A day with a single owner produces the same name the day-grained
# account_assignments row would, so this only changes anything where a shuffle
# was actually recorded — which is the point.
#
# Callers COALESCE it ahead of the day-grained value: ap → aa → the frozen
# c.texter_name (see OWNER_EXPR).
PERIOD_TEXTER_LATERAL = f"""
    LEFT JOIN LATERAL (
        SELECT p.texter_name
          FROM messages m
          JOIN assignment_periods p
            ON LOWER(p.account_email) =
               LOWER((SELECT email FROM accounts WHERE id = c.agent_id))
           AND p.period @> m.sent_at
         WHERE m.conversation_id = c.id
           AND m.sent_at IS NOT NULL
           AND LOWER(m.sender) NOT IN {OUTGOING_EXCLUDED_SQL}
         ORDER BY m.seq DESC, m.id DESC
         LIMIT 1
    ) ap ON TRUE
"""

# Day-grained owner, resolved against the conversation's own date (convo_date),
# not the scrape date.
DAY_ASSIGNMENT_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT agent_name FROM account_assignments
        WHERE account_email = (SELECT email FROM accounts WHERE id = c.agent_id)
          AND assigned_date <= CASE
                WHEN c.convo_date <> ''
                THEN TO_DATE(c.convo_date, 'MM/DD/YYYY')
                ELSE c.audit_date
              END
        ORDER BY assigned_date DESC
        LIMIT 1
    ) aa ON TRUE
"""

# The resolved owner, valid once both laterals above are joined.
OWNER_EXPR = "COALESCE(ap.texter_name, aa.agent_name, c.texter_name)"
