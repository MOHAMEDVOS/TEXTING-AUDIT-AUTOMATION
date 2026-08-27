"""Response-time flag (F17 in the product's own numbering) — ai/response_time.py.

Deep review F28: this was fed the FULL conversation history while the rest of the
audit deliberately applies a 30-day window, so one slow reply from months ago was
re-flagged (and re-penalised) on every single audit, forever.

Shift awareness: elapsed time is counted in staffed shift minutes only
(10:00-19:00 ET, Mon-Fri — ai/shift.py), so overnight and weekend pauses can't
read as unresponsiveness.
"""
from datetime import datetime, timedelta

import pytest

from ai.response_time import check_response_time, _labels_match
from ai.shift import is_on_shift, shift_minutes_between


class TestLabelGating:
    @pytest.mark.parametrize(
        "labels",
        [["Lead"], ["lead"], ["WL Drip"], ["AP Drip"], ["HL Drip"], ["Pushed to client"]],
    )
    def test_in_scope_labels_match(self, labels):
        assert _labels_match(labels) is True

    def test_empty_labels_do_not_match(self):
        assert _labels_match([]) is False

    @pytest.mark.parametrize("label", ["Not Interested", "DNC", "Sold", "Wrong Number", "Stopped Responding"])
    def test_terminal_labels_are_excluded(self, label):
        """A terminal label wins even when an in-scope track label is also present."""
        assert _labels_match([f"FUI, WL Drip, {label}"]) is False

    def test_comma_joined_in_scope_label_still_matches(self):
        assert _labels_match(["FUI, WL Drip"]) is True


class TestShiftMinutes:
    def test_zero_when_reversed_or_equal(self):
        t = datetime(2026, 3, 26, 10, 0)
        assert shift_minutes_between(t, t) == 0.0
        assert shift_minutes_between(t, t - timedelta(hours=1)) == 0.0

    def test_none_inputs_are_zero(self):
        assert shift_minutes_between(None, datetime(2026, 3, 26, 10, 0)) == 0.0
        assert shift_minutes_between(datetime(2026, 3, 26, 10, 0), None) == 0.0

    def test_simple_daytime_gap(self):
        start = datetime(2026, 3, 26, 10, 0)
        assert shift_minutes_between(start, start + timedelta(minutes=12)) == pytest.approx(12, abs=1)

    def test_overnight_pause_is_excluded(self):
        """Thu 7 PM -> Fri 10 AM is the full overnight gap, and counts as zero."""
        evening = datetime(2026, 3, 26, 19, 0)       # Thursday, shift end
        next_morning = datetime(2026, 3, 27, 10, 0)  # Friday, shift start
        assert shift_minutes_between(evening, next_morning) == 0.0

    def test_long_gap_does_not_explode(self):
        """Guards the day-by-day loop against multi-year spans."""
        start = datetime(2024, 1, 1, 10, 0)
        end = datetime(2026, 1, 1, 10, 0)
        assert shift_minutes_between(start, end) > 0


class TestShiftBoundaries:
    """The shift is 10:00-19:00 ET, Mon-Fri. Everything outside it counts as zero."""

    def test_weekend_is_never_on_shift(self):
        assert is_on_shift(datetime(2026, 8, 22, 11, 0)) is False   # Saturday
        assert is_on_shift(datetime(2026, 8, 23, 11, 0)) is False   # Sunday
        assert is_on_shift(datetime(2026, 8, 21, 11, 0)) is True    # Friday

    def test_shift_edges(self):
        day = datetime(2026, 8, 18, 0, 0)                            # Tuesday
        assert is_on_shift(day.replace(hour=9, minute=59)) is False
        assert is_on_shift(day.replace(hour=10, minute=0)) is True
        assert is_on_shift(day.replace(hour=18, minute=59)) is True
        assert is_on_shift(day.replace(hour=19, minute=0)) is False

    def test_whole_weekend_gap_is_zero(self):
        """Sat 11:00 -> Sat 11:40 is 40 wall-clock minutes of nobody's shift."""
        assert shift_minutes_between(datetime(2026, 8, 22, 11, 0),
                                     datetime(2026, 8, 22, 11, 40)) == 0.0

    def test_friday_evening_to_monday_morning_skips_the_weekend(self):
        """Fri 18:50 -> Mon 10:05 counts 10 min Friday + 5 min Monday, not 3 days."""
        assert shift_minutes_between(datetime(2026, 8, 21, 18, 50),
                                     datetime(2026, 8, 24, 10, 5)) == pytest.approx(15, abs=0.1)

    def test_before_open_does_not_count(self):
        """A lead at 8:10 AM isn't waiting on shift time until 10:00."""
        assert shift_minutes_between(datetime(2026, 8, 18, 8, 10),
                                     datetime(2026, 8, 18, 8, 20)) == 0.0

    def test_mid_shift_delay_is_measured_in_full(self):
        """The shift window must not soften a genuine in-hours delay."""
        assert shift_minutes_between(datetime(2026, 8, 18, 11, 0),
                                     datetime(2026, 8, 18, 11, 40)) == pytest.approx(40, abs=0.1)


def _thread(lead_at: datetime, agent_at: datetime) -> list[dict]:
    return [
        {"sender": "Contact", "message": "Are you still interested?", "sent_at": lead_at, "seq": 0},
        {"sender": "Jack", "message": "Yes — what were you thinking?", "sent_at": agent_at, "seq": 1},
    ]


class TestCheckResponseTime:
    """End-to-end through check_response_time, which no test previously covered."""

    def test_lex_siddoway_regression(self):
        """The false positive this rule was rewritten for.

        Lead replies Fri 2026-08-21 10:26 PM, agent replies Sat 2026-08-22 10:03 AM.
        The old 8 AM-8 PM seven-day window scored this 123 min / "critical" and took
        25 points off Script Adherence. Nobody was on shift for any of it.
        """
        result = check_response_time(
            _thread(datetime(2026, 8, 21, 22, 26), datetime(2026, 8, 22, 10, 3)),
            ["Undefined"],
        )
        assert result is None

    def test_same_gap_on_weekdays_is_also_clean(self):
        """Mon 10:26 PM -> Tue 10:03 AM is 3 shift minutes, under the 10-min bar."""
        assert check_response_time(
            _thread(datetime(2026, 8, 17, 22, 26), datetime(2026, 8, 18, 10, 3)),
            ["Lead"],
        ) is None

    def test_yellow_threshold(self):
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 12)),
            ["Lead"],
        )
        assert result is not None
        assert result["threshold_tag"] == "yellow"
        assert result["minutes"] == 12
        assert result["severity"] == "medium"

    def test_red_threshold(self):
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 20)),
            ["Lead"],
        )
        assert result["threshold_tag"] == "red"
        assert result["severity"] == "high"

    def test_critical_threshold(self):
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"],
        )
        assert result["threshold_tag"] == "critical"
        assert result["minutes"] == 40
        assert result["script_penalty"] == 25

    def test_terminal_label_still_wins_over_a_slow_reply(self):
        assert check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["FUI, WL Drip, Not Interested"],
        ) is None


class TestF17Attribution:
    """On a shuffled account, F17 must name the texter who was on duty when the
    lead's message landed — not the conversation's default owner.

    F17 is injected by ai/scorer.py rather than the tier4 generator, so it has to
    record its own culprit ref; without one, database.db.attribute_flag_details
    falls back to the conversation owner and marks it 'legacy'.
    """

    def test_evidence_starts_with_the_lead_message(self):
        """The culprit ref is built from evidence[0], so that must be the lead."""
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"],
        )
        lead_msg, agent_msg = result["evidence"]
        assert lead_msg["sender"] == "Contact"
        assert agent_msg["sender"] == "Jack"
        assert lead_msg["sent_at"] == datetime(2026, 8, 18, 10, 0)

    def test_culprit_ref_uses_the_clock_start(self):
        from ai.prefilter.tier4_flag_generator import _culprit_ref

        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"],
        )
        ref = _culprit_ref(result["evidence"][0], result["evidence"][0].get("seq"), "omission")
        assert ref["basis"] == "omission"
        assert ref["sender"] == "Contact"
        # The instant db.py resolves against assignment_periods.
        assert ref["at"] == datetime(2026, 8, 18, 10, 0).isoformat()

    def test_resolve_texter_picks_the_on_duty_owner(self):
        """A mid-day shuffle: the lead texts at 10:00 while Alice still owns it."""
        from datetime import timezone as _tz
        from database.db import _resolve_texter

        def _utc(h, m):
            return datetime(2026, 8, 18, h, m, tzinfo=_tz.utc)

        periods = [
            {"texter_name": "Alice", "started_at": _utc(8, 0), "ended_at": _utc(10, 30)},
            {"texter_name": "Bob", "started_at": _utc(10, 30), "ended_at": None},
        ]
        assert _resolve_texter(periods, _utc(10, 0), "Carol") == ("Alice", "exact")
        assert _resolve_texter(periods, _utc(11, 0), "Carol") == ("Bob", "exact")
        # No timestamp at all -> falls back to the conversation owner.
        assert _resolve_texter(periods, None, "Carol") == ("Carol", "inferred")
