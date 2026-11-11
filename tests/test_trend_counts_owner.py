"""Trend counters must follow the resolved owner, not the scrape-time stamp.

No database here (the suite never opens one): these pin the shape of the SQL so
it can't drift back to `LOWER(c.texter_name) = LOWER(ts.agent_name)`, which
charged a validated flag to whoever held the account on the SCRAPE day instead
of the conversation's day.
"""
import re

from database import trend_counts as tc
from database.owner_sql import OWNER_EXPR


def test_counters_match_on_resolved_owner_not_stamp():
    assert OWNER_EXPR in tc._UPDATE_COUNTS_SQL
    assert not re.search(r"c\.texter_name\)\s*=\s*LOWER\(ts\.agent_name", tc._UPDATE_COUNTS_SQL)
    assert "LOWER(lc.owner) = LOWER(ts.agent_name)" in tc._UPDATE_COUNTS_SQL


def test_placeholders_line_up_with_the_arguments_passed():
    params = lambda sql: set(re.findall(r"\$(\d+)", sql))
    assert params(tc._ENSURE_ROWS_SQL) == {"1"}          # execute(sql, audit_date)
    assert params(tc._UPDATE_COUNTS_SQL) == {"1", "2", "3"}  # (agent, late text, date)


def test_missing_owner_rows_are_created_with_zero_conversations():
    assert "INSERT INTO trend_snapshots" in tc._ENSURE_ROWS_SQL
    assert "NOT EXISTS" in tc._ENSURE_ROWS_SQL
