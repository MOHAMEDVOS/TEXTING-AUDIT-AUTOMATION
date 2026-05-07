"""Regression tests for UI-noise filtering in transcript parser."""

from ai.transcript_parser import parse_transcript


def test_parser_removes_ui_chrome_lines():
    raw = """
    Messenger Contacts Campaigns Workflows New Dialer Calendar Skiptace
    Newest labels Date:
    Hi Eddie, have you thought about selling your place?
    -Jack
    04:28 PM
    👍 to "Hi Eddie, have you thought about selling your place?"
    04:29 PM
    """

    msgs = parse_transcript(raw, agent_name="Noah")
    text = " ".join(m["message"] for m in msgs)
    assert "Messenger Contacts Campaigns" not in text
    assert "Newest labels Date" not in text
    assert any("Hi Eddie" in m["message"] for m in msgs)

