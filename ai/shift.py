"""
The team's work shift, and the arithmetic every time-based audit rule shares.

The texting team is staffed 10:00 AM - 7:00 PM Eastern, Monday-Friday
(config.settings.SHIFT_START_HOUR / SHIFT_END_HOUR / SHIFT_DAYS). Elapsed-time
rules must measure *shift minutes*, never wall-clock minutes: a lead who texts
at 10:26 PM on a Friday and gets an answer at 10:03 AM on Saturday waited zero
minutes of anyone's shift, and flagging that as a slow reply penalises an agent
for being off duty.

This module is the single definition of that window. Before it existed the
project carried three disagreeing ones (8-20 in the response-time flag, 9-17 in
the scorer, and a UI-only 10-19 in the assignments editor).

Pure / deterministic: no DB, no I/O, no clock reads.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from config.settings import (
    SHIFT_DAYS,
    SHIFT_END_HOUR,
    SHIFT_START_HOUR,
    TIMEZONE,
)

# A response gap is only ever measured inside the audit window (30 days), but
# a stray timestamp shouldn't be able to spin the day loop for years.
_MAX_SPAN_DAYS = 400


def _to_shift_tz(dt: datetime) -> datetime:
    """Naive datetimes are read as local shift time; aware ones are converted."""
    if dt.tzinfo is None:
        return TIMEZONE.localize(dt)
    return dt.astimezone(TIMEZONE)


def shift_window(day) -> tuple[datetime, datetime]:
    """The (start, end) instants of the shift on a given local date."""
    start = TIMEZONE.localize(
        datetime(day.year, day.month, day.day, SHIFT_START_HOUR, 0, 0)
    )
    end = TIMEZONE.localize(
        datetime(day.year, day.month, day.day, SHIFT_END_HOUR, 0, 0)
    )
    return start, end


def is_on_shift(dt: datetime | None) -> bool:
    """True when dt falls on a worked weekday, inside the shift hours."""
    if not dt:
        return False
    dt = _to_shift_tz(dt)
    if dt.weekday() not in SHIFT_DAYS:
        return False
    start, end = shift_window(dt.date())
    return start <= dt < end


def shift_minutes_between(dt1: datetime | None, dt2: datetime | None) -> float:
    """
    Minutes of staffed shift time between dt1 and dt2.

    Off-shift time - overnight, before open, after close, and whole non-worked
    days - contributes nothing, so an overnight or weekend pause never reads as
    unresponsiveness. A delay that genuinely sits inside the shift is measured
    in full.
    """
    if not dt1 or not dt2 or dt2 <= dt1:
        return 0.0

    dt1 = _to_shift_tz(dt1)
    dt2 = _to_shift_tz(dt2)

    total_seconds = 0.0
    curr_date = dt1.date()
    end_date = dt2.date()
    if (end_date - curr_date).days > _MAX_SPAN_DAYS:
        end_date = curr_date + timedelta(days=_MAX_SPAN_DAYS)

    while curr_date <= end_date:
        if curr_date.weekday() not in SHIFT_DAYS:
            curr_date += timedelta(days=1)
            continue

        b_start, b_end = shift_window(curr_date)
        t0 = max(dt1, b_start) if curr_date == dt1.date() else b_start
        t1 = min(dt2, b_end) if curr_date == end_date else b_end

        if t1 > t0:
            total_seconds += (t1 - t0).total_seconds()

        curr_date += timedelta(days=1)

    return total_seconds / 60.0


def shift_window_label() -> str:
    """Human-readable window for manager-facing explanations, e.g. '10a-7p ET, Mon-Fri'."""
    def _h(h: int) -> str:
        suffix = "a" if h < 12 else "p"
        hour = h if 1 <= h <= 12 else abs(h - 12) or 12
        return f"{hour}{suffix}"

    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    worked = sorted(SHIFT_DAYS)
    if worked == list(range(7)):
        days = "daily"
    elif worked and worked == list(range(worked[0], worked[-1] + 1)):
        days = f"{names[worked[0]]}-{names[worked[-1]]}"
    else:
        days = ", ".join(names[d] for d in worked) or "no days"

    return f"{_h(SHIFT_START_HOUR)}-{_h(SHIFT_END_HOUR)} ET, {days}"
