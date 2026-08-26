"""Flag suppression — the mechanism behind deep review F1.

F1: a global, unscoped suppression list was fuzzy substring-matched against every
flag on every conversation of every agent, so one reviewer clicking "Not Valid"
once permanently disabled that flag product-wide. The global path is gone; these
tests pin the behaviour of what remains so it cannot come back.
"""
import pytest

from ai.scorer import _filter_flags, _strip_null_flags

OPT_OUT = "Continued texting after explicit opt-out."


class TestStripNullFlags:
    """Model sentinels must be dropped; real flags must survive."""

    @pytest.mark.parametrize("sentinel", ["none", "N/A", "na", "-", "", "No flags", "no red flags"])
    def test_sentinels_are_stripped(self, sentinel):
        assert _strip_null_flags([sentinel]) == []

    def test_real_flags_survive(self):
        assert _strip_null_flags([OPT_OUT]) == [OPT_OUT]

    def test_sentinels_stripped_alongside_real_flags(self):
        assert _strip_null_flags(["none", OPT_OUT, "-"]) == [OPT_OUT]


class TestFilterFlags:
    def test_exact_match_suppresses(self):
        assert _filter_flags([OPT_OUT], {OPT_OUT.lower()}) == []

    def test_unrelated_flag_is_not_suppressed(self):
        """A rejection of one flag must never touch a different flag."""
        other = "Gave up after first no with zero rebuttal."
        assert _filter_flags([other], {OPT_OUT.lower()}) == [other]

    def test_short_flag_not_swallowed_by_long_pattern(self):
        """Regression for F1's worst case.

        The old matcher tested `flag in pattern` as well, so a long stored
        pattern suppressed any short flag whose text appeared inside it.
        """
        assert _filter_flags(["Rude"], {"rude tone throughout the entire conversation"}) == ["Rude"]

    def test_truncated_pattern_still_matches_full_flag(self):
        """DB entries truncated at write time must still suppress their own flag."""
        assert _filter_flags([OPT_OUT], {"continued texting after explicit"}) == []

    def test_ellipsis_wildcard_matches(self):
        assert _filter_flags([OPT_OUT], {"continued texting...opt-out."}) == []

    def test_ellipsis_wildcard_requires_all_segments(self):
        assert _filter_flags([OPT_OUT], {"continued texting...never happened"}) == [OPT_OUT]

    def test_empty_pattern_set_still_strips_sentinels(self):
        assert _filter_flags(["none", OPT_OUT], set()) == [OPT_OUT]

    def test_matching_is_case_insensitive(self):
        assert _filter_flags([OPT_OUT.upper()], {OPT_OUT.lower()}) == []
