"""Message timestamp and texter-attribution parsing (database/db.py).

Pure functions with real branching and no I/O — the highest value-per-line tests
in the project, and previously untested.
"""
from datetime import datetime, timedelta

import pytest

from database.db import _parse_msg_datetime


class TestParseMsgDatetime:
    def test_full_date_and_time(self):
        dt = _parse_msg_datetime({"date": "Thursday, March 26, 2026", "time": "05:59 PM"})
        assert dt is not None
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 3, 26, 17, 59)

    def test_midnight_and_noon_am_pm(self):
        midnight = _parse_msg_datetime({"date": "Thursday, March 26, 2026", "time": "12:00 AM"})
        noon = _parse_msg_datetime({"date": "Thursday, March 26, 2026", "time": "12:00 PM"})
        assert midnight.hour == 0
        assert noon.hour == 12

    def test_time_without_date_returns_none(self):
        """Must NOT silently assume today — that would fabricate a timestamp."""
        assert _parse_msg_datetime({"time": "05:59 PM"}) is None

    def test_iso_timestamp_is_accepted(self):
        dt = _parse_msg_datetime({"timestamp": "2026-03-26T17:59:00Z"})
        assert dt is not None
        assert dt.tzinfo is not None, "ISO timestamps must stay timezone-aware"

    def test_empty_input_returns_none(self):
        assert _parse_msg_datetime({}) is None

    def test_garbage_returns_none_rather_than_raising(self):
        assert _parse_msg_datetime({"date": "not a date", "time": "not a time"}) is None


class TestTimezoneAwareness:
    """Deep review F24: naive datetimes bound to TIMESTAMPTZ are read as UTC."""

    def test_get_now_is_aware(self):
        from config.settings import get_now
        assert get_now().tzinfo is not None

    def test_get_now_is_not_utc_offset_zero_in_eastern(self):
        from config.settings import get_now, TIMEZONE
        now = get_now()
        assert now.utcoffset() == TIMEZONE.utcoffset(now.replace(tzinfo=None))
