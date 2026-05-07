"""
Pre-flight scan: detect any text that COULD trigger a red flag.

Broader than tier1_phrases — it does not try to score or short-circuit.
It only answers: "Should this conversation skip ML and go straight to Groq?"

If ANY trigger pattern matches in the contact's messages OR the agent's messages,
returns (True, pattern_name). The pipeline then bypasses all ML tiers.

Categories:
  1. Explicit opt-out phrases (superset of tier1_phrases._OPT_OUT_PATTERNS)
  2. Aggressive / threatening language
  3. Dollar amounts in agent messages (firm offer risk)
  4. Wrong-number / wrong-person signals
  5. Harassment / legal threat language
"""
from __future__ import annotations

import re
from typing import Optional


# ── Pattern groups ────────────────────────────────────────────────────────────
# Each entry: (pattern_name, compiled_regex, apply_to)
#   apply_to: "contact" | "agent" | "all"

_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # 1. Opt-out phrases — contact only
    ("opt-out:stop_texting",   re.compile(r"\bstop\s+texting\b", re.I), "contact"),
    ("opt-out:stop_messaging", re.compile(r"\bstop\s+messaging\b", re.I), "contact"),
    ("opt-out:stop_contacting",re.compile(r"\bstop\s+contact(ing)?\s+me\b", re.I), "contact"),
    ("opt-out:remove_me",      re.compile(r"\bremove\s+me\b", re.I), "contact"),
    ("opt-out:remove_name",    re.compile(r"\bremove\s+my\s+(name|number)\b", re.I), "contact"),
    ("opt-out:unsubscribe",    re.compile(r"\bunsubscribe\b", re.I), "contact"),
    ("opt-out:leave_alone",    re.compile(r"\bleave\s+me\s+alone\b", re.I), "contact"),
    ("opt-out:dont_contact",   re.compile(r"\bdon'?t\s+(contact|message|text)\s+me\b", re.I), "contact"),
    ("opt-out:stop_bothering", re.compile(r"\bstop\s+bothering\s+me\b", re.I), "contact"),
    ("opt-out:do_not_contact", re.compile(r"\bdo\s+not\s+contact\b", re.I), "contact"),
    ("opt-out:take_off_list",  re.compile(r"\btake\s+(me\s+)?off\s+(your\s+)?(list|registry)\b", re.I), "contact"),
    ("opt-out:no_more",        re.compile(r"\bno\s+more\s+(texts?|messages?|calls?)\b", re.I), "contact"),

    # 2. Aggressive / threatening language — all parties
    ("aggression:profanity_threat", re.compile(r"\b(fuck\s+you|piss\s+off|go\s+to\s+hell|shut\s+up)\b", re.I), "all"),
    ("aggression:legal_threat",     re.compile(r"\b(sue\s+you|suing\s+you|report\s+you|harassment|harassing)\b", re.I), "all"),
    ("aggression:do_not_call",      re.compile(r"\bdo\s+not\s+call\s+(list|registry)\b", re.I), "all"),

    # 3. Dollar / firm offer amounts in AGENT messages — compliance risk
    # NOTE: standard pitch templates like "a cash range like 105k-140k" are
    # normal script behavior and NOT compliance violations. Only flag
    # firm/specific offers where the agent commits to a number.
    ("offer:i_offer", re.compile(
        r"\b(my\s+offer\s+is|i('|')?ll\s+offer\s+you|offering\s+you|"
        r"we('|')?ll\s+pay\s+you|i\s+can\s+do)\s*\$?\s*\d",
        re.I,
    ), "agent"),

    # 4. Wrong number / wrong person
    # NOTE: Removed from flag triggers — Tier 1 (Check 4 + Check 9) handles
    # wrong-number and wrong-identity with proper agent-behavior verification.
    # Only keep "not mine" for property ownership disputes (different from wrong number).

    # 5. Sensitive topics that need Groq judgment
    ("sensitive:legal_threat_contact", re.compile(r"\b(attorney|lawyer|police|authorities)\b", re.I), "contact"),
    ("sensitive:deceased",             re.compile(r"\b(passed\s+away|deceased|died|death)\b", re.I), "all"),
]

# Build fast lookup caches: text split by sender role
def _split_text(messages: list[dict], agent_name: str) -> tuple[str, str]:
    """Return (contact_text, agent_text) as single strings."""
    contact_parts: list[str] = []
    agent_parts: list[str] = []
    for m in messages:
        sender = (m.get("sender") or "").lower()
        body = m.get("body") or ""
        if sender == "agent":
            agent_parts.append(body)
        else:
            contact_parts.append(body)
    return " \n ".join(contact_parts), " \n ".join(agent_parts)


def has_flag_trigger(
    messages: list[dict],
    agent_name: str,
) -> tuple[bool, Optional[str]]:
    """
    Returns (True, pattern_name) if any trigger fires, else (False, None).

    pattern_name is logged so the prefilter_decisions table records which
    trigger caused the bypass. Useful for auditing over-aggressive patterns.
    """
    if not messages:
        return False, None

    contact_text, agent_text = _split_text(messages, agent_name)
    all_text = contact_text + " \n " + agent_text

    for name, pat, scope in _PATTERNS:
        target = {
            "contact": contact_text,
            "agent":   agent_text,
            "all":     all_text,
        }[scope]
        if pat.search(target):
            return True, name

    return False, None
