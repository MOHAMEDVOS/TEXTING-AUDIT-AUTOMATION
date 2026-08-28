"""Response-time flag (F17 in the product's own numbering) — ai/response_time.py.

Deep review F28: this was fed the FULL conversation history while the rest of the
audit deliberately applies a rolling window (7 days), so one slow reply from
months ago was re-flagged (and re-penalised) on every single audit, forever.

Shift awareness: elapsed time is counted in staffed shift minutes only
(10:00-19:00 ET, Mon-Fri — ai/shift.py), so overnight and weekend pauses can't
read as unresponsiveness.
"""
from datetime import datetime, timedelta

import pytest

from ai.analyzer import filter_recent_messages
from ai.response_time import check_response_time, _labels_match
from ai.shift import (
    is_on_shift,
    shift_minutes_between,
    shift_minutes_by_texter,
    shift_minutes_with_periods,
)


def _dated_msg(sender: str, body: str, when: datetime, seq: int) -> dict:
    """A message carrying BOTH fields real transcripts have: the display
    'date' string filter_recent_messages windows on, and the 'sent_at'
    timestamp check_response_time/_parse_msg_datetime reads directly."""
    return {
        "sender": sender,
        "message": body,
        "date": when.strftime("%A, %B %d, %Y"),
        "sent_at": when,
        "seq": seq,
    }


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


# A period covering every date this file's fixtures touch (oldest is the
# stale-rescue tests' ~2025-11 message). Used so tests of the threshold,
# label, and stale-rescue logic exercise a CONFIRMED assignment window rather
# than the removed no-periods-falls-back-to-global-shift path — that path no
# longer exists, so without this, every one of these tests would just assert
# "no flag" for the wrong reason (see TestNoPeriodsSuppressesTheFlag below).
_CONFIRMED = [{"texter_name": "Jack", "started_at": datetime(2020, 1, 1), "ended_at": None}]


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
            ["Undefined"], periods=_CONFIRMED,
        )
        assert result is None

    def test_same_gap_on_weekdays_is_also_clean(self):
        """Mon 10:26 PM -> Tue 10:03 AM is 3 shift minutes, under the 10-min bar."""
        assert check_response_time(
            _thread(datetime(2026, 8, 17, 22, 26), datetime(2026, 8, 18, 10, 3)),
            ["Lead"], periods=_CONFIRMED,
        ) is None

    def test_yellow_threshold(self):
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 12)),
            ["Lead"], periods=_CONFIRMED,
        )
        assert result is not None
        assert result["threshold_tag"] == "yellow"
        assert result["minutes"] == 12
        assert result["severity"] == "medium"

    def test_red_threshold(self):
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 20)),
            ["Lead"], periods=_CONFIRMED,
        )
        assert result["threshold_tag"] == "red"
        assert result["severity"] == "high"

    def test_critical_threshold(self):
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"], periods=_CONFIRMED,
        )
        assert result["threshold_tag"] == "critical"
        assert result["minutes"] == 40
        assert result["script_penalty"] == 25

    def test_terminal_label_still_wins_over_a_slow_reply(self):
        assert check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["FUI, WL Drip, Not Interested"], periods=_CONFIRMED,
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
            ["Lead"], periods=_CONFIRMED,
        )
        lead_msg, agent_msg = result["evidence"]
        assert lead_msg["sender"] == "Contact"
        assert agent_msg["sender"] == "Jack"
        assert lead_msg["sent_at"] == datetime(2026, 8, 18, 10, 0)

    def test_culprit_ref_uses_the_clock_start(self):
        from ai.prefilter.tier4_flag_generator import _culprit_ref

        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"], periods=_CONFIRMED,
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


# ── Per-texter hours ─────────────────────────────────────────────────────────
# assignment_periods records WHO OWNS an account, not what hours someone works
# (the server defaults a segment to 00:00 → end of day), so periods may only
# NARROW the global shift, never widen it.

def _period(name: str, start: datetime, end: datetime | None) -> dict:
    return {"texter_name": name, "started_at": start, "ended_at": end}


class TestPeriodsNarrowTheWindow:
    def test_all_day_period_cannot_revive_an_overnight_gap(self):
        """Regression guard for the bug the shift window fixed.

        Alice owns the account Fri 00:00 → Sun 00:00 (an all-day period, the
        server default). The lead writes Fri 10:26 PM, the reply lands Sat
        10:03 AM. Measuring against the period alone would count that in full
        again; the global shift still has to clip it to nothing.
        """
        periods = [_period("Alice", datetime(2026, 8, 21, 0, 0),
                           datetime(2026, 8, 23, 0, 0))]
        assert shift_minutes_with_periods(datetime(2026, 8, 21, 22, 26),
                                          datetime(2026, 8, 22, 10, 3),
                                          periods) == 0.0
        assert check_response_time(
            _thread(datetime(2026, 8, 21, 22, 26), datetime(2026, 8, 22, 10, 3)),
            ["Lead"], periods=periods,
        ) is None

    def test_narrow_period_clips_an_evening_message(self):
        """Alice worked 10a–2p Tuesday. A 5:30 PM lead message is not her wait."""
        periods = [_period("Alice", datetime(2026, 8, 18, 10, 0),
                           datetime(2026, 8, 18, 14, 0))]
        assert shift_minutes_with_periods(datetime(2026, 8, 18, 17, 30),
                                          datetime(2026, 8, 19, 10, 40),
                                          periods) == 0.0
        assert check_response_time(
            _thread(datetime(2026, 8, 18, 17, 30), datetime(2026, 8, 19, 10, 40)),
            ["Lead"], periods=periods,
        ) is None

    def test_uncovered_tail_counts_zero_but_the_covered_part_still_counts(self):
        """Alice held it until 10:30 and nobody after; only her 30 min count."""
        periods = [_period("Alice", datetime(2026, 8, 18, 9, 0),
                           datetime(2026, 8, 18, 10, 30))]
        assert shift_minutes_with_periods(datetime(2026, 8, 18, 10, 0),
                                          datetime(2026, 8, 18, 10, 40),
                                          periods) == pytest.approx(30, abs=0.1)
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"], periods=periods,
        )
        assert result["minutes"] == 30
        assert result["by_texter"] == {"Alice": pytest.approx(30, abs=0.1)}

    def test_handover_splits_the_wait(self):
        periods = [
            _period("Alice", datetime(2026, 8, 18, 8, 0), datetime(2026, 8, 18, 10, 30)),
            _period("Bob", datetime(2026, 8, 18, 10, 30), None),
        ]
        shares = shift_minutes_by_texter(datetime(2026, 8, 18, 10, 0),
                                         datetime(2026, 8, 18, 10, 40), periods)
        assert shares["Alice"] == pytest.approx(30, abs=0.1)
        assert shares["Bob"] == pytest.approx(10, abs=0.1)
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"], periods=periods,
        )
        assert result["minutes"] == 40
        assert result["started_at"] == datetime(2026, 8, 18, 10, 0)
        assert result["ended_at"] == datetime(2026, 8, 18, 10, 40)


class TestNoPeriodsSuppressesTheFlag:
    """Without any assignment_periods for the account, we can't confirm anyone
    was on duty during the gap, so F17 can't hold anyone accountable for it —
    it must not fire. An account in this state needs
    scripts/backfill_assignment_periods.py run (or a fresh dashboard
    assignment) before F17 can catch a slow reply on it."""

    @pytest.mark.parametrize("periods", [None, [], ()])
    def test_empty_periods_never_flags(self, periods):
        result = check_response_time(
            _thread(datetime(2026, 8, 18, 10, 0), datetime(2026, 8, 18, 10, 40)),
            ["Lead"], periods=periods,
        )
        assert result is None

    @pytest.mark.parametrize("periods", [None, [], ()])
    def test_empty_periods_yield_zero_confirmed_minutes(self, periods):
        assert shift_minutes_with_periods(datetime(2026, 8, 18, 10, 0),
                                          datetime(2026, 8, 18, 10, 40), periods) == 0.0


class TestStaleRescueDoesNotOpenAResponseClock:
    """filter_recent_messages() rescues a contact's last reply from outside the
    window so a stale-but-real thread isn't mislabeled Stopped Responding. But
    that rescued message is a stitched-in historical artifact, not a fresh
    reply waiting on the agent — pairing it with today's agent reply would
    report a multi-month silence as one continuous unanswered wait. This class
    reproduces that exact shape and confirms the fix (the _stale_rescue tag).
    """

    def test_stale_rescue_tag_alone_never_opens_a_burst(self):
        """Unit-level: a message tagged _stale_rescue must not start a clock,
        even in isolation, regardless of where filter_recent_messages ran."""
        old = datetime(2025, 11, 10, 15, 46)
        reply = datetime(2026, 8, 26, 15, 11)
        messages = [
            {**_dated_msg("Contact", "It's not my house", old, 0), "_stale_rescue": True},
            _dated_msg("Jack", "Any updates?", reply, 1),
        ]
        assert check_response_time(messages, ["Lead"], periods=_CONFIRMED) is None

    def test_rescued_only_case_end_to_end(self):
        """The reported bug shape: a contact message ~9.5 months old is the ONLY
        contact-side activity in the whole thread. The 7-day window drops it,
        rescues it back in (tagged), and check_response_time must not flag a
        multi-month gap against the next agent message."""
        old_contact = datetime(2025, 11, 10, 15, 46)
        agent_attempts = [
            datetime(2026, 8, 20, 15, 0),
            datetime(2026, 8, 26, 15, 11),
        ]
        raw = [_dated_msg("Contact", "Hello?", old_contact, 0)] + [
            _dated_msg("Jack", "Hi, checking back in", t, i + 1)
            for i, t in enumerate(agent_attempts)
        ]
        windowed = filter_recent_messages(raw, window_days=7)
        # Sanity: the rescue actually fired and the tag is present.
        assert any(m.get("_stale_rescue") for m in windowed)
        assert check_response_time(windowed, ["Lead"], periods=_CONFIRMED) is None

    def test_rescued_plus_fresh_reply_measures_only_the_fresh_gap(self):
        """A stale rescue AND a genuine in-window reply both present. The gap
        reported must come from the fresh pair, not the resurrected one."""
        old_contact = datetime(2025, 11, 10, 15, 46)
        fresh_contact = datetime(2026, 8, 26, 10, 0)
        fresh_agent = datetime(2026, 8, 26, 10, 20)   # 20 shift-minutes later
        raw = [
            _dated_msg("Contact", "Hello?", old_contact, 0),
            _dated_msg("Contact", "Are you still there?", fresh_contact, 1),
            _dated_msg("Jack", "Yes, sorry for the delay!", fresh_agent, 2),
        ]
        windowed = filter_recent_messages(raw, window_days=7)
        result = check_response_time(windowed, ["Lead"], periods=_CONFIRMED)
        assert result is not None
        assert result["minutes"] == 20
        assert result["threshold_tag"] == "red"
        # Evidence must be the fresh pair, never the ~9.5-month-old message.
        assert result["evidence"][0]["message"] == "Are you still there?"

    def test_without_the_fix_this_shape_would_explode(self):
        """Documents what the bug looked like: feeding check_response_time an
        UNTAGGED rescue (the old behavior) reproduces a six-figure-minute flag.
        Guards against a future refactor silently dropping the tag."""
        old = datetime(2025, 11, 10, 15, 46)
        reply = datetime(2026, 8, 26, 15, 11)
        untagged_rescue = [
            _dated_msg("Contact", "It's not my house", old, 0),
            _dated_msg("Jack", "Any updates?", reply, 1),
        ]
        result = check_response_time(untagged_rescue, ["Lead"], periods=_CONFIRMED)
        assert result is not None
        assert result["minutes"] > 100_000
