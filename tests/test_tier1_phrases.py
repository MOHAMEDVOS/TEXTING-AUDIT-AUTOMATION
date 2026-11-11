"""Golden-style tests for Tier-1 phrase rules (ai/prefilter/tier1_phrases_v2.py).

Only clear-cut cases are pinned. Each test drives the public `evaluate()` entry
point and asserts on the decision and the label it assigns.
"""
import pytest

from ai.prefilter.tier1_phrases_v2 import evaluate, _WRONG_NUMBER_PATTERNS

OPENER = "Hi John, are you interested in selling your property at 12 Oak St?"


def _run(contact_reply, labels=None, tier="WF"):
    messages = [
        {"sender": "agent", "body": OPENER},
        {"sender": "contact", "body": contact_reply},
    ]
    return evaluate(messages, tier, "Ann", "John", labels)


def _label(result):
    """The label the rules expect (label_assigned just echoes the texter's)."""
    res = result.result
    return res["label_should_be"] or res["label_assigned"]


@pytest.mark.parametrize("reply", [
    "STOP texting me",
    "Do not contact me again",
    "unsubscribe",
    "Please remove me from your list",
])
def test_opt_out_is_dnc(reply):
    r = _run(reply)
    assert r is not None
    assert r.tier_hit == 1
    assert r.decision == "short_circuit"
    assert _label(r) == "DO Not Call"


def test_opt_out_beats_wrong_number():
    r = _run("wrong number, stop texting me", ["Wrong Number"])
    assert _label(r) == "DO Not Call"


@pytest.mark.parametrize("reply", ["wrong number", "You have the wrong person"])
def test_wrong_number(reply):
    r = _run(reply, ["Wrong Number"])
    assert r.decision == "short_circuit"
    assert _label(r) == "Wrong Number"


@pytest.mark.parametrize("reply", [
    "wrong number, I'm 15",
    "I'm 15, you have the wrong number",
])
def test_minor_wrong_number_is_dnc(reply):
    r = _run(reply, ["Wrong Number"])
    assert r.decision == "short_circuit"
    assert _label(r) == "DO Not Call"


def test_sold_reply():
    r = _run("Sold", ["Sold"])
    assert r.decision == "short_circuit"
    assert _label(r) == "Sold"


def test_silent_contact():
    messages = [{"sender": "agent", "body": OPENER}]
    r = evaluate(messages, "WF", "Ann", "John", None)
    assert r.decision == "short_circuit"
    assert "silent" in r.notes
    assert _label(r) == "Stopped Responding"


def test_empty_conversation_defers():
    assert evaluate([], "WF", "Ann", "John", None) is None


def test_engaged_contact_is_not_short_circuited():
    assert _run("Sure, tell me more") is None


@pytest.mark.parametrize("reply", [
    "I'm not interested in selling",
    "I am not interested",
    "I'm not selling right now",
    "I'm not ready to sell",
])
def test_not_interested_is_not_a_wrong_number_name(reply):
    assert not any(p.search(reply) for p in _WRONG_NUMBER_PATTERNS)
    r = _run(reply, ["Not Interested"])
    assert r is None or _label(r) != "Wrong Number"
