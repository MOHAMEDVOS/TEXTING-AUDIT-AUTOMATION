"""Disposition hygiene — F18/F19, ai/prefilter/label_combination.py.

An account must end on ONE status. SmarterContact seeds every contact with a
campaign tag plus "New Lead", so a texter who sets the real outcome without
clearing "New Lead" leaves the CRM saying two contradictory things:

    New Lead, Wrong Number      -> F18
    Do Not Call, New lead       -> F18
    New Lead  (contact replied) -> F19

The three things that must NOT flag are the whole difficulty of the rule:
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
from ai.prefilter._guards import (
    CONFIDENCE_MAP,
    FLAG_ID_MAP,
    SEVERITY_MAP,
    WHITELIST_FLAG_OUTPUTS,
    normalize_red_flags,
)
from ai.prefilter.label_combination import (
    CONFLICTING_DISPO_FLAG,
    STALE_NEW_LEAD_FLAG,
    check_label_combination,
    status_label_keys,
)

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


def _dispo_flags(labels: list[str], thread: list[dict]) -> list[str]:
    result = analyze_conversation(thread, "Emma", "Sheriff Kristin",
                                  assigned_labels=labels)
    return [f for f in (result.get("red_flags") or []) if f.startswith("Left ")]


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
    """F18 — two statuses that don't belong together."""

    @pytest.mark.parametrize("labels", [
        ["Fishnet list", "New Lead", "Wrong Number"],   # the Sheriff Kristin case
        ["Do Not Call", "New lead"],                    # the John Harrelson case
        ["New Lead", "Do Not Call"],                    # the C Timothy case
        ["Fishnet list", "New Lead", "Sold"],
        ["Wrong Number", "Maybe later"],
    ])
    def test_unrelated_statuses_are_flagged(self, labels):
        assert _dispo_flags(labels, REPLIED) == [CONFLICTING_DISPO_FLAG]

    @pytest.mark.parametrize("labels", [
        ["Fishnet list", "Not interested", "Decision Maker"],  # 94 rows in the snapshot
        ["Verified", "Not Interested"],
        ["Decision Maker", "Not Interested"],
        ["Fishnet list", "FU1", "WL drip"],                    # drip step + drip track
        ["Lead", "Pushed to client"],                          # WF hand raise
        ["Waiting to be pushed", "Lead"],
    ])
    def test_coherent_label_sets_are_not_flagged(self, labels):
        assert _dispo_flags(labels, REPLIED) == []

    @pytest.mark.parametrize("labels", [
        ["Fishnet list", "Wrong Number"],
        ["Fishnet list", "Do Not Call"],
        ["Fishnet list", "Abv MV"],
        ["Fishnet list", "Undefined"],
    ])
    def test_one_status_plus_a_campaign_tag_is_fine(self, labels):
        assert _dispo_flags(labels, REPLIED) == []


class TestStaleNewLead:
    """F19 — 'New Lead' left on after the contact actually engaged."""

    def test_flagged_when_the_contact_replied_in_window(self):
        assert _dispo_flags(["Fishnet list", "New Lead"], REPLIED) == [STALE_NEW_LEAD_FLAG]

    def test_not_flagged_when_the_contact_never_replied(self):
        """A drip firing into silence is still genuinely a New Lead."""
        assert _dispo_flags(["Fishnet list", "New Lead"], SILENT) == []

    def test_not_flagged_when_the_only_reply_predates_the_window(self):
        """The 40-days-ago case: the texter on shift never saw that reply, so
        they cannot be charged for not acting on it. Same reasoning as the
        stale-rescue label guard."""
        assert _dispo_flags(["Fishnet list", "New Lead"], OLD_REPLY) == []

    def test_an_empty_contact_message_does_not_count_as_a_reply(self):
        thread = [
            _msg("Contact", "   ", NOW - timedelta(minutes=5)),
            _msg("Emma", "Checking back in", NOW),
        ]
        assert _dispo_flags(["Fishnet list", "New Lead"], thread) == []

    def test_only_new_lead_triggers_this_one(self):
        """A single real status is a finished disposition, not a stale one."""
        assert _dispo_flags(["Fishnet list", "Wrong Number"], REPLIED) == []


class TestFlagPlumbing:
    """A flag the rest of the pipeline doesn't know about is invisible."""

    @pytest.mark.parametrize("flag", [CONFLICTING_DISPO_FLAG, STALE_NEW_LEAD_FLAG])
    def test_flag_survives_the_whitelist(self, flag):
        """normalize_red_flags drops anything not in WHITELIST_FLAG_OUTPUTS."""
        assert flag in WHITELIST_FLAG_OUTPUTS
        assert normalize_red_flags([flag]) == [flag]

    @pytest.mark.parametrize("flag,fid", [
        (CONFLICTING_DISPO_FLAG, "F18"),
        (STALE_NEW_LEAD_FLAG, "F19"),
    ])
    def test_flag_carries_id_severity_and_confidence(self, flag, fid):
        assert FLAG_ID_MAP[flag] == fid
        assert SEVERITY_MAP[fid] == "medium"
        assert CONFIDENCE_MAP[fid] > 0

    def test_culprit_ref_is_recorded_so_attribution_is_not_legacy(self):
        """Without a ref, database/db.py falls back to the conversation owner
        and stamps attribution='legacy' — wrong for a brand-new flag."""
        from ai.prefilter._guards import canon_flag_text

        result = analyze_conversation(
            REPLIED, "Emma", "Sheriff Kristin",
            assigned_labels=["Fishnet list", "New Lead", "Wrong Number"],
        )
        culprits = result.get("flag_culprits") or {}
        assert canon_flag_text(CONFLICTING_DISPO_FLAG) in culprits

    def test_runs_on_tier1_short_circuits(self):
        """A bare 'wrong number' reply is answered by Tier 1 and never reaches
        the T4 generator. The flag must still appear."""
        result = analyze_conversation(
            REPLIED, "Emma", "Sheriff Kristin",
            assigned_labels=["Fishnet list", "New Lead", "Wrong Number"],
        )
        assert result.get("model_used", "").startswith("prefilter_t1")
        assert CONFLICTING_DISPO_FLAG in (result.get("red_flags") or [])

    def test_no_labels_never_flags(self):
        assert _dispo_flags([], REPLIED) == []
        assert check_label_combination(None, REPLIED) == []


def test_non_string_labels_are_skipped():
    labels = ["Fishnet list", None, 5, "New Lead", "Wrong Number"]
    assert check_label_combination(labels, REPLIED) == [CONFLICTING_DISPO_FLAG]
    assert check_label_combination([None, 7], REPLIED) == []


# A reply no other label rule has an opinion about, so only F18/F19 can mark
# the label wrong.
NEUTRAL = [
    _msg("Emma", "Hi Sheriff, have you thought about selling?", NOW - timedelta(minutes=5)),
    _msg("Contact", "Hi, sorry", NOW),
]


class TestLabelIsMarkedWrong:
    """F18/F19 must not sit next to a 'Correct' label verdict."""

    def _result(self, labels, thread=NEUTRAL):
        return analyze_conversation(thread, "Emma", "Sheriff Kristin",
                                    assigned_labels=labels)

    def test_conflict_marks_label_wrong(self):
        r = self._result(["Fishnet list", "Missed Call", "Not interested", "New Lead"])
        assert r["label_correct"] is False
        assert r["label_should_be"] == "a single status"
        assert r["_dispo_hygiene"] is True

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

    def test_wrong_label_culprit_is_recorded(self):
        r = self._result(["Fishnet list", "New Lead", "Maybe later"])
        assert "__label__" in (r.get("flag_culprits") or {})
