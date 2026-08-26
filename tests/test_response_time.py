"""Response-time flag (F17 in the product's own numbering) — ai/response_time.py.

Deep review F28: this was fed the FULL conversation history while the rest of the
audit deliberately applies a 30-day window, so one slow reply from months ago was
re-flagged (and re-penalised) on every single audit, forever.
"""
from datetime import datetime, timedelta

import pytest

from ai.response_time import _business_minutes_between, _labels_match


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


class TestBusinessMinutes:
    def test_zero_when_reversed_or_equal(self):
        t = datetime(2026, 3, 26, 10, 0)
        assert _business_minutes_between(t, t) == 0.0
        assert _business_minutes_between(t, t - timedelta(hours=1)) == 0.0

    def test_none_inputs_are_zero(self):
        assert _business_minutes_between(None, datetime(2026, 3, 26, 10, 0)) == 0.0
        assert _business_minutes_between(datetime(2026, 3, 26, 10, 0), None) == 0.0

    def test_simple_daytime_gap(self):
        start = datetime(2026, 3, 26, 10, 0)
        assert _business_minutes_between(start, start + timedelta(minutes=12)) == pytest.approx(12, abs=1)

    def test_overnight_pause_is_excluded(self):
        """8 PM -> 8 AM must not count as ~12 hours of unresponsiveness."""
        evening = datetime(2026, 3, 26, 20, 0)
        next_morning = datetime(2026, 3, 27, 8, 0)
        assert _business_minutes_between(evening, next_morning) < 60

    def test_long_gap_does_not_explode(self):
        """Guards the day-by-day loop against multi-year spans."""
        start = datetime(2024, 1, 1, 10, 0)
        end = datetime(2026, 1, 1, 10, 0)
        assert _business_minutes_between(start, end) > 0
