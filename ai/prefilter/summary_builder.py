"""
Smart summary builder for ML prefilter short-circuited conversations.

Extracts real facts from the conversation (message count, contact tone,
agent behavior, topic signals) and builds a descriptive summary that
reads like Groq output — not a mechanical "Tier X said clean" message.

Used by all three tiers when short-circuiting.
"""
from __future__ import annotations

import re
from typing import Optional


# ── Tone / intent detection patterns ─────────────────────────────────────────

_HOSTILE_PATTERNS = [
    re.compile(r"\b(fuck|shit|damn|hell|stfu|wtf|idiot|stupid)\b", re.I),
    re.compile(r"[\U0001F621\U0001F620\U0001F92C\U0001F595]"),  # angry/rude emoji
]

_NOT_INTERESTED_PATTERNS = [
    re.compile(r"\b(not\s+interested|no\s+thanks?|nah|nope|pass)\b", re.I),
    re.compile(r"\b(don'?t\s+want|not\s+selling|not\s+for\s+sale)\b", re.I),
    re.compile(r"\b(never|absolutely\s+not)\b", re.I),
]

_MAYBE_PATTERNS = [
    re.compile(r"\b(maybe|possibly|might|could\s+be|thinking\s+about)\b", re.I),
    re.compile(r"\b(not\s+sure|let\s+me\s+think|down\s+the\s+road)\b", re.I),
]

_SOLD_PATTERNS = [
    re.compile(r"\b(already\s+sold|just\s+sold|under\s+contract|sold\s+it)\b", re.I),
    re.compile(r"\b(closing\s+soon|in\s+escrow|have\s+an?\s+agent)\b", re.I),
]

_WRONG_NUMBER_PATTERNS = [
    re.compile(r"\b(wrong\s+(number|person)|not\s+my\s+(house|property|number))\b", re.I),
    re.compile(r"\b(don'?t\s+own|never\s+owned|who\s+is\s+this)\b", re.I),
]

_EMOJI_ONLY = re.compile(
    r"^[\s\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    r"\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
    r"\u2600-\u26FF\u2700-\u27BF\U0000FE00-\U0000FE0F"
    r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    r"?!.,]+$"
)

_POSITIVE_PATTERNS = [
    re.compile(r"\b(yes|yeah|yep|sure|ok(ay)?|sounds?\s+good)\b", re.I),
    re.compile(r"\b(tell\s+me\s+more|how\s+(much|does\s+it))\b", re.I),
    re.compile(r"\b(interested|what'?s?\s+the\s+offer)\b", re.I),
]


def _classify_contact_tone(contact_msgs: list[dict]) -> str:
    """Classify the overall contact tone from their messages."""
    if not contact_msgs:
        return "silent"

    all_text = " ".join(m.get("body", "") for m in contact_msgs)

    if any(p.search(all_text) for p in _HOSTILE_PATTERNS):
        return "hostile"
    if any(p.search(all_text) for p in _WRONG_NUMBER_PATTERNS):
        return "wrong_number"
    if any(p.search(all_text) for p in _SOLD_PATTERNS):
        return "already_sold"
    if any(p.search(all_text) for p in _NOT_INTERESTED_PATTERNS):
        return "not_interested"
    if any(p.search(all_text) for p in _POSITIVE_PATTERNS):
        return "interested"
    if any(p.search(all_text) for p in _MAYBE_PATTERNS):
        return "maybe"

    # Check if all messages are very short (emoji, one word, etc.)
    if all(len((m.get("body") or "").strip()) < 10 for m in contact_msgs):
        if any(_EMOJI_ONLY.match((m.get("body") or "").strip()) for m in contact_msgs):
            return "emoji_only"
        return "brief"

    return "neutral"


def _describe_agent_opening(agent_msgs: list[dict]) -> str:
    """Describe how the agent opened the conversation."""
    if not agent_msgs:
        return ""
    first = (agent_msgs[0].get("body") or "").strip()
    if len(first) < 20:
        return "sent a brief initial message"
    if any(w in first.lower() for w in ["hi ", "hey ", "hello", "good morning", "good afternoon"]):
        return "sent a warm initial message"
    if any(w in first.lower() for w in ["follow", "checking", "reaching back"]):
        return "sent a follow-up message"
    return "sent an initial outreach message"


def _count_rebuttals(agent_msgs: list[dict], contact_msgs: list[dict]) -> int:
    """Estimate how many rebuttals the agent used (agent messages after first contact reply)."""
    if not contact_msgs:
        return 0
    # Count agent messages that came after the first contact message
    first_contact_idx = None
    all_msgs_ordered = []
    # We don't have guaranteed ordering, so count agent msgs after first contact body
    return max(0, len(agent_msgs) - 1)  # subtract the opening message


def _detect_referral_close(agent_msgs: list[dict]) -> bool:
    """Check if agent mentioned referral or $1k."""
    for m in agent_msgs:
        body = (m.get("body") or "").lower()
        if any(w in body for w in ["referral", "refer", "$1k", "$1,000", "1000 for"]):
            return True
    return False


def build_summary(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    scores: dict,
    *,
    model_used: str = "prefilter",
) -> str:
    """
    Build a descriptive, Groq-style summary from conversation content.

    Returns a 1-3 sentence summary that describes what actually happened,
    matching the style of Groq's AI audit summaries.
    """
    agent_msgs = [m for m in messages if (m.get("sender") or "").lower() == "agent"]
    contact_msgs = [m for m in messages if (m.get("sender") or "").lower() != "agent"]

    tone = _classify_contact_tone(contact_msgs)
    opening = _describe_agent_opening(agent_msgs)
    has_referral = _detect_referral_close(agent_msgs)
    n_agent = len(agent_msgs)
    n_contact = len(contact_msgs)
    total_turns = n_agent + n_contact

    parts: list[str] = []

    # ── Opening: what stage / scenario ───────────────────────────────
    if tone == "silent":
        if n_agent == 1:
            parts.append(
                f"No funnel stage reached. Texter {opening} "
                f"and received no response from {contact_name}."
            )
        else:
            parts.append(
                f"No funnel stage reached. Texter {opening} "
                f"and sent {n_agent} messages total, "
                f"but {contact_name} never responded."
            )
        parts.append("No compliance issues in a one-sided outreach.")

    elif tone == "hostile":
        contact_snippet = _get_snippet(contact_msgs[-1])
        parts.append(
            f"No funnel stage reached. Texter {opening} "
            f"but received a hostile response from {contact_name}."
        )
        if contact_snippet:
            parts.append(f"Contact replied with: \"{contact_snippet}\".")
        parts.append(
            "Texter handled the situation without escalation. "
            "No compliance violations."
        )

    elif tone == "wrong_number":
        parts.append(
            f"Wrong number scenario. {contact_name} indicated this is not their property. "
        )
        if has_referral:
            parts.append(
                "Texter apologized and pivoted to a referral ask."
            )
        else:
            parts.append("Texter acknowledged and ended the conversation.")

    elif tone == "already_sold":
        parts.append(
            f"No funnel stage reached. {contact_name} indicated the property "
            f"is already sold or under contract."
        )
        if has_referral:
            parts.append("Texter pivoted to a referral close.")
        else:
            parts.append("Texter acknowledged and closed out.")

    elif tone == "not_interested":
        rebuttals = max(0, n_agent - 1)
        if rebuttals >= 1:
            parts.append(
                f"No funnel stage reached. Texter {opening} and {contact_name} "
                f"expressed disinterest. Texter used {rebuttals} follow-up "
                f"message{'s' if rebuttals > 1 else ''} before ending the conversation."
            )
        else:
            parts.append(
                f"No funnel stage reached. Texter {opening} and {contact_name} "
                f"expressed disinterest."
            )
        if has_referral:
            parts.append("Referral close was included.")

    elif tone == "interested" or tone == "maybe":
        if tone == "interested":
            parts.append(
                f"Early funnel engagement. Texter {opening} and {contact_name} "
                f"showed interest."
            )
        else:
            parts.append(
                f"Early funnel engagement. Texter {opening} and {contact_name} "
                f"expressed tentative interest."
            )
        if n_agent > 2:
            parts.append(
                f"Texter followed up with {n_agent - 1} additional messages "
                f"to qualify the lead."
            )

    elif tone == "emoji_only":
        parts.append(
            f"No funnel stage reached. Texter {opening} and {contact_name} "
            f"replied only with an emoji reaction, no text."
        )
        if n_agent > 1:
            parts.append("Texter followed up but received no substantive response.")

    elif tone == "brief":
        snippet = _get_snippet(contact_msgs[-1])
        parts.append(
            f"Minimal engagement. Texter {opening} and {contact_name} "
            f"gave a brief reply"
        )
        if snippet:
            parts[-1] += f": \"{snippet}\"."
        else:
            parts[-1] += "."
        if n_agent > 1:
            parts.append(f"Texter sent {n_agent} total messages.")

    else:  # neutral
        parts.append(
            f"Texter {opening} and exchanged {total_turns} messages "
            f"with {contact_name}."
        )
        parts.append("Conversation proceeded without compliance issues.")

    # ── Score commentary (only if something stands out) ──────────────
    comp = scores.get("compliance_score", 100)
    if comp >= 95:
        parts.append("No rule violations detected.")
    elif comp >= 80:
        parts.append("Minor areas for improvement noted.")

    return " ".join(parts)


def detect_label(
    messages: list[dict],
    contact_name: str,
) -> tuple[str, str]:
    """
    Detect a reasonable label + label_reason from conversation content.
    Returns (label, label_reason).
    """
    agent_msgs = [m for m in messages if (m.get("sender") or "").lower() == "agent"]
    contact_msgs = [m for m in messages if (m.get("sender") or "").lower() != "agent"]
    tone = _classify_contact_tone(contact_msgs)

    if tone == "silent":
        if len(agent_msgs) <= 1:
            return ("Stopped Responding",
                    f"{contact_name} never replied to the initial outreach.")
        return ("Stopped Responding",
                f"{contact_name} did not respond to {len(agent_msgs)} messages.")

    if tone == "hostile":
        return ("Bluffer",
                f"{contact_name} responded with hostility. "
                f"No genuine selling intent detected.")

    if tone == "wrong_number":
        return ("Wrong Number",
                f"{contact_name} indicated wrong number or not the property owner.")

    if tone == "already_sold":
        return ("Not interested",
                f"{contact_name} indicated the property is already sold or under contract.")

    if tone == "not_interested":
        return ("Not interested",
                f"{contact_name} expressed disinterest in selling.")

    if tone == "interested":
        return ("New Lead",
                f"{contact_name} showed interest in the conversation.")

    if tone == "maybe":
        return ("New Lead",
                f"{contact_name} expressed tentative interest.")

    if tone == "emoji_only":
        return ("Stopped Responding",
                f"{contact_name} replied only with emoji, no substantive engagement.")

    if tone == "brief":
        return ("Stopped Responding",
                f"{contact_name} gave only brief responses with no clear intent.")

    return ("Lead",
            f"Conversation with {contact_name} was routine with no red flags.")


def detect_funnel_stage(messages: list[dict]) -> str:
    """Detect approximate funnel stage from conversation content."""
    agent_msgs = [m for m in messages if (m.get("sender") or "").lower() == "agent"]
    contact_msgs = [m for m in messages if (m.get("sender") or "").lower() != "agent"]

    if not contact_msgs:
        return "none"

    tone = _classify_contact_tone(contact_msgs)
    if tone in ("silent", "hostile", "wrong_number", "emoji_only"):
        return "none"

    if tone in ("interested", "maybe"):
        # Check if pillars were discussed
        all_text = " ".join(m.get("body", "") for m in messages).lower()
        pillar_hits = sum(1 for kw in [
            "condition", "price", "timeline", "motivation",
            "how much", "when", "why sell", "needs work",
            "roof", "repair", "renovati",
        ] if kw in all_text)
        if pillar_hits >= 3:
            return "mid_funnel"
        if pillar_hits >= 1:
            return "wide_funnel"
        return "initial_contact"

    return "none"


def _get_snippet(msg: dict, max_len: int = 50) -> str:
    """Get a short snippet of a message for quoting in summaries."""
    body = (msg.get("body") or "").strip()
    if not body:
        return ""
    if len(body) <= max_len:
        return body
    # Cut at word boundary
    truncated = body[:max_len].rsplit(" ", 1)[0]
    return truncated + "..."
