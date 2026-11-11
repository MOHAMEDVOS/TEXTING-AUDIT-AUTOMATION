"""Disposition hygiene, ai/prefilter/label_combination.py.

An account must end on ONE status. SmarterContact seeds every contact with a
campaign tag plus "New Lead", so a texter who sets the real outcome without
clearing "New Lead" leaves the CRM saying two contradictory things:

    New Lead, Wrong Number      -> wrong label
    Do Not Call, New lead       -> wrong label
    New Lead  (contact replied) -> wrong label

It is reported as an ordinary wrong label (label_correct False plus the scorer's
"Wrong label" flag), so Trends counts it under Wrong Label and there is no
separate flag.

The things that must NOT be marked wrong are the whole difficulty of the rule:
campaign/list tags (every row has one), team synonyms that mean the same
status ("Decision Maker, Not interested"), and genuine two-word dispositions
("Lead, Pushed to client", "FU1, WL drip").

The label sets used here are the real ones from the dev snapshot, not invented
shapes — 'Fishnet list' is on 289 of its 292 conversations.

These run through analyze_conversation rather than the T4 generator on purpose:
Tier 1 short-circuits on trivial conversations (a bare "wrong number" reply
never reaches T4), and the first version of this feature silently did nothing
in production for exactly that reason.
"""
from datetime import datetime, timedelta

import pytest

from ai.analyzer import analyze_conversation
from ai.prefilter.label_combination import check_label_combination, status_label_keys

NOW = datetime(2026, 11, 10, 11, 16)


def _msg(sender: str, body: str, when: datetime) -> dict:
    return {
        "sender": sender,
        "body": body,
        "date": when.strftime("%A, %B %d, %Y"),
        "sent_at": when.isoformat(),
        "time": when.strftime("%I:%M %p"),
    }


# Contact answered inside the 7-day audit window.
REPLIED = [
    _msg("Emma", "Hi Sheriff, may I ask about 615 S 1850 E?", NOW - timedelta(minutes=5)),
    _msg("Contact", "Wrong number", NOW),
]
# Drip going out into silence — never any reply.
SILENT = [
    _msg("Emma", "Hi, have you thought about selling?", NOW - timedelta(days=14)),
    _msg("Emma", "Checking back in", NOW),
]
# Only reply is far outside the window (filter_recent_messages rescues it).
OLD_REPLY = [
    _msg("Contact", "Wrong number", NOW - timedelta(days=200)),
    _msg("Emma", "Checking back in", NOW),
]


# A reply no other label rule has an opinion about, so only this rule can mark
# the label wrong.
NEUTRAL = [
    _msg("Emma", "Hi Sheriff, have you thought about selling?", NOW - timedelta(minutes=5)),
    _msg("Contact", "Hi, sorry", NOW),
]


def _flagged(labels: list[str], thread: list[dict]) -> bool:
    """True when the hygiene rule marked the label wrong."""
    result = analyze_conversation(thread, "Emma", "Sheriff Kristin",
                                  assigned_labels=labels)
    return bool(result.get("_dispo_hygiene"))


class TestStatusLabelKeys:
    """Campaign tags out, synonyms collapsed."""

    def test_campaign_tags_are_not_statuses(self):
        assert status_label_keys(["Fishnet list", "Wrong Number"]) == {"wrong number"}
        assert status_label_keys(["Denza_Sniper_FINAL", "Do Not Call"]) == {"do not call"}

    def test_not_interested_synonyms_collapse_to_one(self):
        assert status_label_keys(
            ["Fishnet list", "Not interested", "Decision Maker"]
        ) == {"not interested"}
        assert status_label_keys(["Verified", "Not Interested"]) == {"not interested"}

    def test_casing_and_spacing_are_ignored(self):
        assert status_label_keys(["New lead"]) == status_label_keys(["  NEW LEAD "])

    def test_no_labels_is_empty(self):
        assert status_label_keys(None) == set()
        assert status_label_keys([]) == set()


class TestConflictingDispositions:
    """Two statuses that don't belong together."""

    @pytest.mark.parametrize("labels", [
        ["Fishnet list", "New Lead", "Wrong Number"],   # the Sheriff Kristin case
        ["Do Not Call", "New lead"],                    # the John Harrelson case
        ["New Lead", "Do Not Call"],                    # the C Timothy case
        ["Fishnet list", "New Lead", "Sold"],
        ["Wrong Number", "Maybe later"],
    ])
    def test_unrelated_statuses_are_flagged(self, labels):
        assert _flagged(labels, NEUTRAL)

    @pytest.mark.parametrize("labels", [
        ["Fishnet list", "Not interested", "Decision Maker"],  # 94 rows in the snapshot
        ["Verified", "Not Interested"],
        ["Decision Maker", "Not Interested"],
        ["Fishnet list", "FU1", "WL drip"],                    # drip step + drip track
        ["Lead", "Pushed to client"],                          # WF hand raise
        ["Waiting to be pushed", "Lead"],
    ])
    def test_coherent_label_sets_are_not_flagged(self, labels):
        assert not _flagged(labels, NEUTRAL)

    @pytest.mark.parametrize("labels", [
        ["Fishnet list", "Wrong Number"],
        ["Fishnet list", "Do Not Call"],
        ["Fishnet list", "Abv MV"],
        ["Fishnet list", "Undefined"],
    ])
    def test_one_status_plus_a_campaign_tag_is_fine(self, labels):
        assert not _flagged(labels, NEUTRAL)


class TestStaleNewLead:
    """'New Lead' left on after the contact actually engaged."""

    def test_flagged_when_the_contact_replied_in_window(self):
        assert _flagged(["Fishnet list", "New Lead"], NEUTRAL)

    def test_not_flagged_when_the_contact_never_replied(self):
        """A drip firing into silence is still genuinely a New Lead."""
        assert not _flagged(["Fishnet list", "New Lead"], SILENT)

    def test_not_flagged_when_the_only_reply_predates_the_window(self):
        """The 40-days-ago case: the texter on shift never saw that reply, so
        they cannot be charged for not acting on it. Same reasoning as the
        stale-rescue label guard."""
        assert not _flagged(["Fishnet list", "New Lead"], OLD_REPLY)

    def test_an_empty_contact_message_does_not_count_as_a_reply(self):
        thread = [
            _msg("Contact", "   ", NOW - timedelta(minutes=5)),
            _msg("Emma", "Checking back in", NOW),
        ]
        assert not _flagged(["Fishnet list", "New Lead"], thread)

    def test_a_single_real_status_is_a_finished_disposition(self):
        assert not _flagged(["Fishnet list", "Wrong Number"], NEUTRAL)


def test_no_labels_never_flags():
    assert not _flagged([], REPLIED)
    assert check_label_combination(None, REPLIED) is None


def test_non_string_labels_are_skipped():
    assert check_label_combination(["Fishnet list", None, 5, "New Lead", "Wrong Number"], REPLIED)
    assert check_label_combination([None, 7], REPLIED) is None


class TestItIsOneWrongLabelFlag:
    """The mixed disposition is the wrong-label flag, not a second flag."""

    def _result(self, labels, thread=NEUTRAL):
        return analyze_conversation(thread, "Emma", "Sheriff Kristin",
                                    assigned_labels=labels)

    def test_conflict_marks_label_wrong(self):
        r = self._result(["Fishnet list", "Missed Call", "Not interested", "New Lead"])
        assert r["label_correct"] is False
        assert r["label_should_be"] == "a single status"

    def test_new_lead_plus_one_status_names_that_status(self):
        r = self._result(["Fishnet list", "New Lead", "Maybe later"])
        assert r["label_correct"] is False
        assert r["label_should_be"] == "Maybe Later"

    def test_stale_new_lead_marks_label_wrong(self):
        r = self._result(["Fishnet list", "New Lead"])
        assert r["label_correct"] is False
        assert "New Lead" in r["label_should_be"]

    def test_valid_pair_stays_correct(self):
        r = self._result(["Fishnet list", "Not interested", "Decision Maker"])
        assert r["label_correct"] is not False
        assert "_dispo_hygiene" not in r

    def test_no_separate_red_flag_is_added(self):
        r = self._result(["Fishnet list", "New Lead", "Maybe later"])
        assert not any("disposition" in f.lower() or "new lead" in f.lower()
                       for f in (r.get("red_flags") or []))

    def test_wrong_label_culprit_is_recorded(self):
        """Without a ref, database/db.py falls back to the conversation owner
        and stamps attribution='legacy'."""
        r = self._result(["Fishnet list", "New Lead", "Maybe later"])
        assert "__label__" in (r.get("flag_culprits") or {})

    def test_runs_on_tier1_short_circuits(self):
        """A bare 'wrong number' reply is answered by Tier 1 and never reaches
        the T4 generator. The label must still be marked wrong."""
        r = self._result(["Fishnet list", "New Lead", "Wrong Number"], REPLIED)
        assert r.get("model_used", "").startswith("prefilter_t1")
        assert r["label_correct"] is False


class TestScorerKeepsTheWrongLabel:
    """ai/scorer.py re-checks every label_correct=False result. For 'New Lead,
    Wrong Number' it once judged the label equal to 'Wrong Number', reset it to
    Correct and added no flag."""

    def _scored(self, labels, thread=REPLIED):
        from ai.scorer import _inject_wrong_label_flags

        r = analyze_conversation(thread, "Emma", "Sheriff Kristin", assigned_labels=labels)
        _inject_wrong_label_flags([r])
        return r

    @pytest.mark.parametrize("labels", [
        ["Fishnet list", "New Lead", "Wrong Number"],
        ["Do Not Call", "New lead"],
        ["New Lead", "Do Not Call"],
        ["Fishnet list", "New Lead"],
    ])
    def test_conflict_gets_exactly_one_wrong_label_flag(self, labels):
        r = self._scored(labels)
        assert r["label_correct"] is False
        assert [f for f in r["red_flags"] if f.startswith("Wrong label:")]
        assert len(r["red_flags"]) == len([f for f in r["red_flags"] if f.startswith("Wrong label:")])

    def test_equivalent_labels_are_still_reset_to_correct(self):
        """The scorer's original check must keep working for ordinary results."""
        from ai.scorer import _inject_wrong_label_flags

        r = {"label_correct": False, "label_assigned": "Verified",
             "label_should_be": "Not Interested", "red_flags": []}
        _inject_wrong_label_flags([r])
        assert r["label_correct"] is True
        assert r["red_flags"] == []
