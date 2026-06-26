"""
Answer-based pillar detection for the prefilter.

A "pillar" — Condition, Price, Motivation, Timeline — counts as *gathered* only
when the LEAD positively supplies the information: a dollar number, a
property-state description, a stated reason, or a timeframe.

This mirrors the Groq prompt rule "Only counts if the LEAD provides the info"
(see ai/prompts.py). The deterministic tiers historically counted a pillar
whenever its topic *keyword* appeared anywhere in the transcript — including in
an agent's question. So an agent simply asking "do you have a price in mind?"
inflated pillar_count, which produced false "Did not escalate after all 4
pillars gathered." flags. Scanning contact messages only, with content patterns
(not topic words), fixes that at the root.
"""
from __future__ import annotations

import re

_CONTACT_SENDERS = ("contact", "lead")

# price — the lead states an actual number / dollar amount / range.
# A bare mention of the word "price" never matches; a refusal such as
# "not at this point" supplies no number, so it never matches either.
_PRICE_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\s*(?:k|thousand|hundred|million|m))?"   # $120k, $120,000
    r"|\b\d{2,4}\s?(?:k|thousand|million)\b"                   # 120k, 120 thousand
    r"|\b\d{1,3}(?:,\d{3})+\b"                                 # 120,000
    r"|\b(?:want|asking|ask|looking\s+for|take|accept|get)\s+\$?\s?\d{2,}",
    re.I,
)

# condition — the lead describes the property's physical state or repairs.
_CONDITION_RE = re.compile(
    r"\b(?:fix(?:er[\s-]?upper|ing|ed)?|repairs?|damaged?|needs?\s+work|as[\s-]?is"
    r"|rough\s+shape|foundation|roof|mold|flood(?:ed|ing)?|fire\s+damage"
    r"|tear\s?down|renovat\w*|remodel\w*|rehab\w*|gut(?:ted|ting)?"
    r"|move[\s-]?in\s+ready|well[\s-]?maintained|good\s+(?:shape|condition)"
    r"|new\s+(?:roof|water\s+heater|hvac|furnace|ac|wiring))\b"
    r"|\b(?:updat|upgrad|redid|re-?did|replac)\w*\s+(?:the\s+)?"
    r"(?:electric\w*|plumbing|kitchen|bath\w*|roof|hvac|furnace|floor\w*"
    r"|window\w*|wiring|deck|attic|basement)\b",
    re.I,
)

# motivation — the lead gives a reason for (considering) selling.
_MOTIVATION_RE = re.compile(
    r"\b(?:divorce|inherit\w*|estate|probate|relocat\w*|moving|downsiz\w*"
    r"|upsiz\w*|behind\s+on|foreclos\w*|owe|debt|financial|job\s+(?:loss|transfer)"
    r"|retir\w*|illness|widow\w*|death\s+in"
    r"|need(?:ing)?\s+(?:a\s+)?(?:bigger|smaller)"
    r"|smaller\s+(?:place|home|property|house)"
    r"|too\s+(?:big|much)\s+(?:for|to)|empty\s+nest)\b",
    re.I,
)

# timeline — the lead states when they want to sell / move.
_TIMELINE_RE = re.compile(
    r"\b(?:asap|right\s+away|soon|urgent\w*|couple\s+(?:of\s+)?(?:weeks|months)"
    r"|few\s+(?:weeks|months)|end\s+of\s+(?:the\s+)?(?:month|year|summer|spring)"
    r"|next\s+(?:week|month|year|spring|summer|fall|winter)"
    r"|next\s+\d+\s+months?"           # "next 6 months", "next 3 months"
    r"|this\s+(?:week|month|year|spring|summer|fall|winter)"
    r"|within\s+(?:a\s+)?(?:week|month|year|\d)"
    r"|in\s+20\d{2}"                   # "in 2027", "in 2026" — future year
    r"|no\s+rush|whenever|eventually|not\s+in\s+a\s+hurry|down\s+the\s+road"
    r"|moving\s+(?:out|away|soon)|need\s+to\s+(?:move|be\s+out)\s+(?:by|in|next))\b",
    re.I,
)

_PILLAR_PATTERNS = {
    "condition":  _CONDITION_RE,
    "price":      _PRICE_RE,
    "motivation": _MOTIVATION_RE,
    "timeline":   _TIMELINE_RE,
}


def _contact_text(messages: list[dict]) -> str:
    """Join the body of every contact/lead message into one string."""
    parts: list[str] = []
    for m in messages:
        sender = (m.get("sender") or "").strip().lower()
        if sender in _CONTACT_SENDERS:
            parts.append(m.get("message") or m.get("body") or "")
    return " ".join(parts)


def detect_gathered_pillars(messages: list[dict]) -> set[str]:
    """
    Return the set of pillars the LEAD positively answered.

    Only contact/lead messages are scanned, and only content patterns match —
    so an agent asking "do you have a price in mind?" gathers nothing, and a
    lead refusal ("not at this point, no") gathers nothing.
    """
    text = _contact_text(messages)
    if not text.strip():
        return set()
    return {name for name, rx in _PILLAR_PATTERNS.items() if rx.search(text)}
