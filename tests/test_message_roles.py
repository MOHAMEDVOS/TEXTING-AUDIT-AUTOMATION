"""Agent-vs-lead attribution — deep review F10.

The scraper writes the agent's FIRST NAME into messages.sender ("Noah",
"Resva1006") and "Contact" for inbound. Four separate places used to guess at
this differently, and two of them compared against the literal string "agent",
which never occurs — so every message was classified as the lead.
"""
import pytest

from ai.prefilter.embedder import conversation_to_text
from ai.response_time import _is_agent
from database.db import _is_outgoing


class TestIsOutgoing:
    @pytest.mark.parametrize("sender", ["Noah", "Resva1006", "Kev1040", "Agent", "noah mallen"])
    def test_agent_senders_are_outgoing(self, sender):
        assert _is_outgoing(sender) is True

    @pytest.mark.parametrize("sender", ["Contact", "contact", "CONTACT", "lead", "unknown", "", "  "])
    def test_lead_senders_are_not_outgoing(self, sender):
        assert _is_outgoing(sender) is False

    def test_none_is_not_outgoing(self):
        assert _is_outgoing(None) is False


class TestResponseTimeAgreesWithCanonical:
    """_is_agent used to only exclude 'contact', counting lead/unknown as agent."""

    @pytest.mark.parametrize("sender", ["Contact", "lead", "unknown", "", None])
    def test_non_agent_senders_agree(self, sender):
        assert _is_agent(sender) == _is_outgoing(sender) is False

    @pytest.mark.parametrize("sender", ["Noah", "Resva1006"])
    def test_agent_senders_agree(self, sender):
        assert _is_agent(sender) == _is_outgoing(sender) is True


class TestConversationToText:
    """The embedding input must distinguish the two speakers.

    Before F10 every line came out as CONTACT, so the vectors could not encode
    "the agent said X after the lead said Y" — which is the whole audit.
    """

    def test_roles_are_distinguished(self):
        msgs = [
            {"sender": "Noah", "body": "Hi, are you selling?"},
            {"sender": "Contact", "body": "maybe, whats your offer"},
            {"sender": "Resva1006", "body": "What condition is it in?"},
            {"sender": "lead", "body": "needs a roof"},
        ]
        assert conversation_to_text(msgs).splitlines() == [
            "AGENT: Hi, are you selling?",
            "CONTACT: maybe, whats your offer",
            "AGENT: What condition is it in?",
            "CONTACT: needs a roof",
        ]

    def test_not_everything_collapses_to_contact(self):
        """Direct regression guard for the F10 symptom."""
        msgs = [{"sender": "Noah", "body": "hello"}, {"sender": "Contact", "body": "hi"}]
        assert "AGENT:" in conversation_to_text(msgs)

    def test_multi_word_agent_name_is_still_the_agent(self):
        """The index-builder SQL failed on any account name containing a space."""
        assert conversation_to_text([{"sender": "Noah Mallen", "body": "hi"}]) == "AGENT: hi"

    def test_empty_bodies_are_skipped(self):
        msgs = [{"sender": "Noah", "body": "   "}, {"sender": "Contact", "body": "real"}]
        assert conversation_to_text(msgs) == "CONTACT: real"

    def test_accepts_message_alias_from_db(self):
        assert conversation_to_text([{"sender": "Noah", "message": "via alias"}]) == "AGENT: via alias"
