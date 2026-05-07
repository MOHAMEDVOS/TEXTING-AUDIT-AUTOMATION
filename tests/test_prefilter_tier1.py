"""Tests for Tier 1 phrase-matching prefilter."""
import pytest
from ai.prefilter.tier1_phrases import check_tier1
from ai.prefilter.types import TierHit


def _msgs(*pairs):
    """Build a messages list from (sender, body) pairs."""
    return [{"sender": s, "body": b, "sent_at": None} for s, b in pairs]


def test_explicit_optout_after_agent_continued_returns_flag_escalation():
    """Lead opts out, agent then sends another message → MUST escalate to Groq."""
    msgs = _msgs(
        ("agent",   "Hi, are you the owner of 123 Main St?"),
        ("contact", "stop texting me"),
        ("agent",   "Just one more question — is it for sale?"),
    )
    result = check_tier1(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.tier_hit == TierHit.T1_PHRASE
    assert result.short_circuited is False    # MUST go to Groq
    assert "opt-out" in result.reason.lower()


def test_clean_short_conversation_returns_none():
    """No suspicious phrases → no Tier 1 decision (let other tiers handle)."""
    msgs = _msgs(
        ("agent",   "Hi, this is Sarah. Are you the owner of 123 Main?"),
        ("contact", "Yes, who's asking?"),
        ("agent",   "I'm a local investor. Would you consider selling?"),
        ("contact", "Maybe, what would you offer?"),
    )
    assert check_tier1(msgs, "Agent", "Bob") is None


def test_optout_with_no_subsequent_agent_message_short_circuits_clean():
    """Lead opts out, agent stops correctly → safe to short-circuit as clean."""
    msgs = _msgs(
        ("agent",   "Hi, are you the owner?"),
        ("contact", "remove me from your list"),
    )
    result = check_tier1(msgs, "Agent", "Bob")
    assert result is not None
    assert result.short_circuited is True
    assert result.predicted["compliance_score"] == 100


def test_empty_messages_returns_none():
    assert check_tier1([], "Agent", "Bob") is None


def test_only_agent_messages_returns_none():
    """No contact reply at all — let downstream tiers decide."""
    msgs = _msgs(("agent", "Hi"), ("agent", "Are you there?"))
    assert check_tier1(msgs, "Agent", "Bob") is None


# ── Tests for evaluate() — the ACTUAL pipeline entry point ───────────────────

from ai.prefilter.tier1_phrases import evaluate


def test_evaluate_empty_messages_returns_none():
    """evaluate() must return None on empty input."""
    assert evaluate([], agent_name="Agent", contact_name="Bob") is None


def test_evaluate_opt_out_in_contact_text_escalates():
    """Contact says 'stop texting me' → escalate decision."""
    msgs = _msgs(
        ("agent",   "Hi, are you the owner of 123 Main?"),
        ("contact", "stop texting me"),
    )
    result = evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.decision == "escalate"
    assert result.tier_hit == 1


def test_evaluate_suspicious_aggressive_language_escalates():
    """Aggressive phrase in conversation → escalate."""
    msgs = _msgs(
        ("agent",   "Hi, would you consider selling?"),
        ("contact", "fuck you stop calling me"),
    )
    result = evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.decision == "escalate"
    assert result.tier_hit == 1


def test_evaluate_specific_dollar_offer_escalates():
    """Agent makes a specific dollar offer → compliance risk, escalate."""
    msgs = _msgs(
        ("agent",   "I'll offer you $150000 for the property."),
        ("contact", "Hmm, let me think."),
    )
    result = evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.decision == "escalate"
    assert result.tier_hit == 1


def test_evaluate_contact_silent_agent_one_message_short_circuits():
    """Contact never replied, agent sent 1 message → short_circuit."""
    msgs = _msgs(
        ("agent", "Hi, are you the owner of 123 Main?"),
    )
    result = evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.decision == "short_circuit"
    assert result.tier_hit == 1
    assert result.predicted_scores["compliance_score"] == 100


def test_evaluate_contact_silent_agent_two_messages_short_circuits():
    """Contact never replied, agent sent 2 messages → short_circuit."""
    msgs = _msgs(
        ("agent", "Hi there!"),
        ("agent", "Just following up."),
    )
    result = evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.decision == "short_circuit"


def test_evaluate_normal_conversation_returns_none():
    """Active back-and-forth with no flags → None (pass to tier 2)."""
    msgs = _msgs(
        ("agent",   "Hi, are you the owner of 123 Main?"),
        ("contact", "Yes, what do you want?"),
        ("agent",   "I'm an investor — would you consider selling?"),
        ("contact", "Not really, thanks."),
    )
    result = evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is None


def test_evaluate_wrong_property_pattern_escalates():
    """Contact says 'not my house' → suspicious pattern, escalate."""
    msgs = _msgs(
        ("agent",   "Hi, are you interested in selling your home?"),
        ("contact", "This isn't my house, you have the wrong number."),
    )
    result = evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.decision == "escalate"
