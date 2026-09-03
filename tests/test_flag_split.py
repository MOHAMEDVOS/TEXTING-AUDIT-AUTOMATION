"""F17 blame splitting — database/db.py attribution over an INTERVAL.

Every other flag names one deciding message, so it has one author. F17 is the
only flag with a real interval: a wait has two ends, and when an account changes
hands during it the delay belongs to both texters in proportion.

The split is additive — `texter_name` stays a scalar (now the largest
shareholder) and `texter_split` only appears when more than one texter held the
wait — so every existing reader keeps working.

Pure: no DB. `attribute_flag_details` is driven through a stub connection.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ai.prefilter._guards import canon_flag_text
from ai.prefilter.tier4_flag_generator import _culprit_ref
from ai.response_time import FLAG_TEXT as RESPONSE_TIME_FLAG
from database.db import (
    _resolve_texter,
    _resolve_texters_in_range,
    attribute_flag_details,
)


def _et(h: int, m: int, day: int = 18) -> datetime:
    """A shift-local (Eastern) instant, held in UTC — ET is UTC-4 in August.

    Production timestamps are always tz-aware (timestamptz / UTC-stamped
    scrapes), so the tests use aware datetimes too. Hours past midnight roll
    the date rather than overflowing.
    """
    return (datetime(2026, 8, day, 0, 0, tzinfo=timezone.utc)
            + timedelta(hours=h + 4, minutes=m))


def _period(name: str, start: datetime, end: datetime | None) -> dict:
    return {"texter_name": name, "started_at": start, "ended_at": end}


# Alice hands the account to Bob at 10:30 ET.
HANDOVER = [
    _period("Alice", _et(8, 0), _et(10, 30)),
    _period("Bob", _et(10, 30), None),
]
ALICE_ONLY = [_period("Alice", _et(8, 0), None)]


class _StubConn:
    """Stands in for an asyncpg connection: one conversation row, no periods."""

    def __init__(self, account_email="acct@example.com", texter_name="Carol"):
        self.row = {"account_email": account_email, "texter_name": texter_name}
        self.fetch_calls = 0

    async def fetchrow(self, *_a, **_k):
        return self.row

    async def fetch(self, *_a, **_k):
        self.fetch_calls += 1
        return []


def _attribute(details, culprits, periods):
    conn = _StubConn()
    asyncio.run(attribute_flag_details(conn, 1, details, culprits, periods=periods))
    return conn


def _f17_detail_and_culprits(start: datetime, end: datetime | None):
    """A flag_details entry plus the culprit ref ai/scorer.py records for F17."""
    detail = {"flag_id": "F17", "flag_text": RESPONSE_TIME_FLAG}
    ref = _culprit_ref({"timestamp": start, "sender": "Contact"}, None,
                       "omission", until=end)
    return [detail], {canon_flag_text(RESPONSE_TIME_FLAG): ref}


class TestResolveTextersInRange:
    def test_split_is_largest_share_first(self):
        split = _resolve_texters_in_range(HANDOVER, _et(10, 0), _et(10, 40), "Carol")
        assert split == [
            {"texter_name": "Alice", "minutes": 30.0, "attribution": "exact"},
            {"texter_name": "Bob", "minutes": 10.0, "attribution": "exact"},
        ]

    def test_off_shift_interval_splits_nothing(self):
        """An overnight handover divides a wait nobody was on shift for."""
        assert _resolve_texters_in_range(
            [_period("Alice", _et(0, 0, 21), _et(23, 0, 21)),
             _period("Bob", _et(23, 0, 21), None)],
            _et(22, 26, 21), _et(10, 3, 22), "Carol",
        ) == []

    def test_no_periods_gives_no_split(self):
        assert _resolve_texters_in_range([], _et(10, 0), _et(10, 40), "Carol") == []

    def test_missing_end_gives_no_split(self):
        assert _resolve_texters_in_range(HANDOVER, _et(10, 0), None, "Carol") == []

    def test_missing_start_mirrors_resolve_texter(self):
        """No usable timestamp — the conversation owner, honestly labelled."""
        assert _resolve_texters_in_range(HANDOVER, None, _et(10, 40), "Carol") == [
            {"texter_name": "Carol", "minutes": 0.0, "attribution": "inferred"}
        ]


class TestAttributeFlagDetailsSplits:
    def test_handover_produces_a_split(self):
        details, culprits = _f17_detail_and_culprits(_et(10, 0), _et(10, 40))
        _attribute(details, culprits, HANDOVER)
        d = details[0]
        # The scalar is the LARGEST shareholder — a deliberate change from
        # "whoever was on duty when the clock started".
        assert d["texter_name"] == "Alice"
        assert d["attribution"] == "exact"
        assert d["texter_split"] == [
            {"texter_name": "Alice", "minutes": 30.0, "attribution": "exact"},
            {"texter_name": "Bob", "minutes": 10.0, "attribution": "exact"},
        ]
        assert d["culprit_at"] == _et(10, 0).isoformat()
        assert d["culprit_until"] == _et(10, 40).isoformat()

    def test_single_owner_has_no_split_key(self):
        details, culprits = _f17_detail_and_culprits(_et(10, 0), _et(10, 40))
        _attribute(details, culprits, ALICE_ONLY)
        d = details[0]
        assert d["texter_name"] == "Alice"
        assert d["attribution"] == "exact"
        assert "texter_split" not in d

    def test_no_periods_falls_back_to_the_clock_start_owner(self):
        details, culprits = _f17_detail_and_culprits(_et(10, 0), _et(10, 40))
        _attribute(details, culprits, [])
        d = details[0]
        # Identical to what the single-instant path has always produced.
        assert (d["texter_name"], d["attribution"]) == \
               _resolve_texter([], _et(10, 0), "Carol")
        assert "texter_split" not in d

    def test_uncovered_interval_falls_back_to_the_clock_start_owner(self):
        """Fri 10:26 PM → Sat 10:03 AM: nobody was on shift, so nothing splits,
        and the flag lands on whoever owned the account when the clock started."""
        periods = [_period("Alice", _et(0, 0, 21), None)]
        details, culprits = _f17_detail_and_culprits(_et(22, 26, 21), _et(10, 3, 22))
        _attribute(details, culprits, periods)
        d = details[0]
        assert d["texter_name"] == "Alice"
        assert d["attribution"] == "exact"
        assert "texter_split" not in d

    def test_a_stale_split_is_cleared_when_the_timeline_changes(self):
        """Re-attribution must not leave a split behind once it no longer holds."""
        details, culprits = _f17_detail_and_culprits(_et(10, 0), _et(10, 40))
        details[0]["texter_split"] = [{"texter_name": "Ghost", "minutes": 99.0,
                                       "attribution": "exact"}]
        _attribute(details, culprits, ALICE_ONLY)
        assert "texter_split" not in details[0]

    def test_supplied_periods_are_not_refetched(self):
        """The scorer fetches the timeline once per run; the pool is max_size=2
        while conversations score 15-wide, so a refetch here would serialise."""
        details, culprits = _f17_detail_and_culprits(_et(10, 0), _et(10, 40))
        conn = _attribute(details, culprits, HANDOVER)
        assert conn.fetch_calls == 0


class TestOtherFlagsAreUnchanged:
    def test_an_instant_culprit_keeps_the_single_owner_path(self):
        """Every non-F17 flag names one message, so its ref has no `until` and
        its detail must come out exactly as it does today."""
        ref = _culprit_ref({"timestamp": _et(11, 0), "sender": "Jack"}, 3, "message")
        assert "until" not in ref
        details = [{"flag_id": "F5", "flag_text": "Agent was condescending."}]
        _attribute(details, {canon_flag_text("Agent was condescending."): ref},
                   HANDOVER)
        d = details[0]
        assert d["texter_name"] == "Bob"          # on duty at 11:00
        assert d["attribution"] == "exact"
        assert d["attribution_basis"] == "message"
        assert "texter_split" not in d
        assert "culprit_until" not in d

    def test_resolve_texter_still_resolves_a_single_instant(self):
        """Directly unit-tested elsewhere too — the signature must not move."""
        assert _resolve_texter(HANDOVER, _et(10, 0), "Carol") == ("Alice", "exact")
        assert _resolve_texter(HANDOVER, _et(11, 0), "Carol") == ("Bob", "exact")
        assert _resolve_texter(HANDOVER, None, "Carol") == ("Carol", "inferred")


class TestCulpritRefInterval:
    def test_until_is_only_present_when_given(self):
        assert "until" not in _culprit_ref({"timestamp": _et(10, 0)}, None, "omission")

    def test_until_is_serialised_like_at(self):
        ref = _culprit_ref({"timestamp": _et(10, 0)}, None, "omission",
                           until=_et(10, 40))
        assert ref["at"] == _et(10, 0).isoformat()
        assert ref["until"] == _et(10, 40).isoformat()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])


# ── validation_flag_key ──────────────────────────────────────────────────────
# Mirrors database/schema.sql::validation_flag_key and
# dashboard/views/index.html::validationFlagKey. A re-audit rewording a flag
# used to orphan the auditor's click on it.
def test_validation_flag_key_ignores_trailing_annotation():
    from ai.prefilter._guards import validation_flag_key, DEFENSIBLE_ALTERNATIVE_SUFFIX

    stem = "Wrong label: assigned 'Not Interested' but should be 'DO Not Call'"
    assert validation_flag_key(stem + DEFENSIBLE_ALTERNATIVE_SUFFIX) == validation_flag_key(stem)
    assert validation_flag_key(stem + " (contact said: 'six million')") == validation_flag_key(stem)


def test_validation_flag_key_normalizes_case_space_and_period():
    from ai.prefilter._guards import validation_flag_key

    assert validation_flag_key("  Slow  response TIME to an engaged lead. ") == \
        validation_flag_key("Slow response time to an engaged lead")


def test_validation_flag_key_keeps_distinct_flags_distinct():
    from ai.prefilter._guards import validation_flag_key

    assert validation_flag_key("Wrong label: assigned 'A' but should be 'B'") != \
        validation_flag_key("Wrong label: assigned 'A' but should be 'C'")


def test_validation_flag_key_passes_legacy_sentinel_through():
    from ai.prefilter._guards import validation_flag_key

    assert validation_flag_key("*") == "*"
