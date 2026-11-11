"""Stale contact evidence must never produce a wrong-label flag.

The reported conversation: a contact wrote "Please don't contact me about my
house!!!!!" in August 2025. The drip kept running and sent another opener in
November 2026. Every audit since re-read that year-old refusal as if it had
just arrived, forced the label to Do Not Call, and flagged whoever happened to
own the account that week for a "Wrong Label".

Why it reached the scorer at all: ai.analyzer.filter_recent_messages audits a
7-day window. When that window contains no contact reply, it rescues the most
recent one from outside the window (tagged `_stale_rescue`) so a thread with
real-but-old engagement isn't misread as Stopped Responding. Only
ai/response_time.py honoured the tag; the label path treated the rescued
message as a live reply.

The fix is in ai/prefilter/_guards.py: read the old message (it is what keeps
the label right) but never charge a flag for it. An age cutoff was tried first
and rejected — it just moves the damage, because a "check back in the fall"
lead then reads as silent and the texter gets flagged for the correct label.
"""
from datetime import datetime, timedelta

import pytest

from ai.analyzer import analyze_conversation, filter_recent_messages
from ai.prefilter._guards import contact_evidence_is_stale_only

NOW = datetime(2026, 11, 9, 14, 5)


def _msg(sender: str, body: str, when: datetime) -> dict:
    """A message carrying both fields real transcripts have: the display 'date'
    string filter_recent_messages windows on, and the 'sent_at' timestamp."""
    return {
        "sender": sender,
        "body": body,
        "date": when.strftime("%A, %B %d, %Y"),
        "sent_at": when.isoformat(),
        "time": when.strftime("%I:%M %p"),
    }


def _audit(thread: list[dict], labels: list[str]) -> dict:
    return analyze_conversation(thread, "Jack", "Kyle Vanculin", assigned_labels=labels)


class TestContactEvidenceIsStaleOnly:
    """The predicate the guard is built on."""

    def test_true_when_the_only_contact_reply_is_rescued(self):
        thread = [
            {**_msg("Contact", "don't contact me", NOW - timedelta(days=400)),
             "_stale_rescue": True},
            _msg("Jack", "Thinking of selling?", NOW),
        ]
        assert contact_evidence_is_stale_only(thread) is True

    def test_false_when_a_fresh_reply_is_present(self):
        thread = [
            {**_msg("Contact", "don't contact me", NOW - timedelta(days=400)),
             "_stale_rescue": True},
            _msg("Contact", "actually, what's your offer?", NOW - timedelta(days=1)),
            _msg("Jack", "Thinking of selling?", NOW),
        ]
        assert contact_evidence_is_stale_only(thread) is False

    def test_false_when_the_contact_never_replied(self):
        """A genuinely silent thread is not 'stale evidence' — there is no
        evidence at all, and Stopped Responding handling must still apply."""
        thread = [_msg("Jack", "Thinking of selling?", NOW)]
        assert contact_evidence_is_stale_only(thread) is False


class TestStaleEvidenceDoesNotFlagTheLabel:
    """End-to-end, through the real window + prefilter + guard chain."""

    def test_year_old_opt_out_does_not_flag_this_weeks_texter(self):
        """The reported bug. The refusal predates the texter's shift by 15
        months — their 'Not Interested' must stand."""
        thread = [
            _msg("Contact",
                 "Please don't contact me about my house !!!!! "
                 "I don't like to be bugged on what's mine!!!",
                 datetime(2025, 8, 5, 13, 37)),
            _msg("Jack", "Hi Kyle, have you thought about selling? - Jack", NOW),
        ]
        result = _audit(thread, ["Not Interested"])
        assert result["label_correct"] is not False
        assert result["label_should_be"] != "DO Not Call"
        assert not any("wrong label" in str(f).lower()
                       for f in (result.get("red_flags") or []))

    def test_rescued_message_is_still_present_for_context(self):
        """Read the old file: the guard suppresses the flag, it does not delete
        the message. Dropping it outright is the rejected age-cutoff approach."""
        thread = [
            _msg("Contact", "Please don't contact me about my house",
                 datetime(2025, 8, 5, 13, 37)),
            _msg("Jack", "Hi Kyle, have you thought about selling?", NOW),
        ]
        windowed = filter_recent_messages(thread, window_days=7)
        assert any(m.get("_stale_rescue") for m in windowed)

    def test_maybe_later_lead_keeps_its_label_at_any_age(self):
        """The case an age cutoff breaks: 'check back in the fall' is a
        forward-looking statement that stays true for months. The texter
        labelled it correctly and must not be flagged."""
        thread = [
            _msg("Contact", "Not right now, check back with me in the fall",
                 NOW - timedelta(days=40)),
            _msg("Jack", "Sounds good, I'll follow up then.", NOW),
        ]
        result = _audit(thread, ["Maybe Later"])
        assert result["label_correct"] is not False

    @pytest.mark.parametrize("age_days", [40, 120, 400])
    def test_suppression_holds_at_every_age_past_the_window(self, age_days):
        """No cliff: the guard keys on 'outside the audit window', not on how
        far outside, so there is no age at which the flag reappears."""
        thread = [
            _msg("Contact", "Please don't contact me about my house",
                 NOW - timedelta(days=age_days)),
            _msg("Jack", "Hi Kyle, have you thought about selling?", NOW),
        ]
        assert _audit(thread, ["Not Interested"])["label_correct"] is not False


class TestFreshEvidenceStillFlagsNormally:
    """The guard must not blunt a live, in-window mislabel."""

    def test_current_opt_out_still_forces_do_not_call(self):
        thread = [
            _msg("Contact", "Please don't contact me about my house",
                 NOW - timedelta(days=2)),
            _msg("Jack", "Understood, thanks.", NOW),
        ]
        result = _audit(thread, ["Not Interested"])
        assert result["label_correct"] is False
        assert result["label_should_be"] == "DO Not Call"

    def test_current_wrong_number_still_flagged(self):
        thread = [
            _msg("Contact", "You have the wrong number, I'm not Kyle",
                 NOW - timedelta(days=1)),
            _msg("Jack", "Sorry about that!", NOW),
        ]
        result = _audit(thread, ["Not Interested"])
        assert result["label_correct"] is False
        assert result["label_should_be"] == "Wrong Number"

    def test_a_fresh_reply_alongside_a_stale_one_is_still_judged(self):
        """The stale rescue only happens when the window has no contact reply,
        but guard against a transcript that carries both: the fresh message is
        real evidence and must be acted on."""
        thread = [
            {**_msg("Contact", "whatever", NOW - timedelta(days=400)),
             "_stale_rescue": True},
            _msg("Contact", "You have the wrong number, I'm not Kyle",
                 NOW - timedelta(days=1)),
            _msg("Jack", "Sorry about that!", NOW),
        ]
        result = _audit(thread, ["Not Interested"])
        assert result["label_correct"] is False
        assert result["label_should_be"] == "Wrong Number"
