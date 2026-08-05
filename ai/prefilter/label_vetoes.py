"""Structural (non-vocabulary) label vetoes.

Every detector in ``label_validator.py`` and ``tier1_phrases_v2.py`` is a
keyword/regex match — and a keyword match can always be wrong in a context
nobody wrote a pattern for yet. These vetoes ask a different kind of
question: not "does this phrase match a list", but "is this verdict
*structurally* possible given the rest of the conversation".

Added 2026-08-05 after an 18-conversation reviewer rejection review showed
the same handful of regexes firing on context they were never meant to catch
(e.g. `\\bsold\\b` matching "I sold off 20 units" — a career anecdote, not the
subject property). Patching each regex closes the specific case; these
vetoes catch the *next* phrasing of the same mistake, because they don't
depend on vocabulary at all.

Used by ``label_validator._expected_label()`` and the Tier-1 short-circuit
checks in ``tier1_phrases_v2.py``.
"""
from __future__ import annotations

import re


def _sender(m: dict) -> str:
    return (m.get("sender") or "").strip().lower()


def _body(m: dict) -> str:
    return (m.get("message") or m.get("body") or "").strip()


# ── Address extraction ───────────────────────────────────────────────────────
# Openers are templated: "...selling your property at 707 Pritz Ave?" /
# "...selling 123 N Virgil St lately?" — a street number followed by 1-4
# capitalized-or-lowercase words before the next punctuation/stopword.
_ADDRESS_RE = re.compile(
    r"\b(\d{2,6})\s+([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3})\b"
)

# A contact discussing price, condition, or ownership has — by definition —
# engaged with THIS property. Wrong Number cannot be true if this fires.
_ENGAGEMENT_RE = re.compile(
    r"\b(bedroom|bathroom|bath|kitchen|garage|pool|basement|attic|roof|foundation"
    r"|sqft|sq\s*ft|square\s+feet|acre|fixer|renovat|remodel|move.?in\s+ready"
    r"|asking\s+price|how\s+much|make\s+(me\s+)?an?\s+offer|the\s+price|that\s+price"
    r"|worth|way\s+off\s+base|too\s+low|lowball"
    r"|i\s+own|my\s+(house|home|property)|not\s+selling|not\s+for\s+sale)\b",
    re.I,
)

# Portfolio / plural sale objects — "sold off 20 units", "sold a few condos" —
# are never the singular subject property under discussion.
_PORTFOLIO_OBJECT_RE = re.compile(
    r"\b\d+\s+(units?|condos?|houses?|homes?|properties|doors)\b"
    r"|\b(a\s+few|several|many|dozens|multiple)\s+(units?|condos?|houses?|homes?|properties)\b",
    re.I,
)


def _extract_subject_address(messages: list[dict]) -> tuple[str, str] | None:
    """Pull (street_number, first_street_word) from the AGENT's first message
    (the templated opener names the subject property). Returns None if no
    address pattern is found."""
    for m in messages or []:
        if _sender(m) in ("contact", "lead"):
            continue
        match = _ADDRESS_RE.search(_body(m))
        if match:
            street_word = match.group(2).split()[0].lower()
            return match.group(1), street_word
    return None


def contact_confirmed_address(messages: list[dict]) -> bool:
    """True if a CONTACT message repeats the subject property's street number
    and street name back. A contact who can read back their own address has
    proven — independent of anything else they said — that they are the
    right person. (#17 Rachelle Bohannon: agent opener named "707 Pritz Ave",
    contact replied "...707 Pritz Avenue Dayton Ohio" and was still flagged
    Wrong Number by a keyword match on an unrelated phrase.)
    """
    subject = _extract_subject_address(messages)
    if not subject:
        return False
    number, street_word = subject
    for m in messages or []:
        if _sender(m) not in ("contact", "lead"):
            continue
        body = _body(m).lower()
        if number in body and street_word in body:
            return True
    return False


def contact_engaged_on_property(messages: list[dict]) -> bool:
    """True if any CONTACT message discusses price, condition, or ownership
    of the property — not just a passing identity question. A contact
    arguing about price ("way off base with that price", #13 Max Kielcz) or
    describing their house has, by definition, confirmed they are the right
    person; Wrong Number is not a possible reading of that message.
    """
    for m in messages or []:
        if _sender(m) not in ("contact", "lead"):
            continue
        if _ENGAGEMENT_RE.search(_body(m)):
            return True
    return False


def sold_refers_to_subject_property(messages: list[dict]) -> bool:
    """False if EVERY contact mention of "sold" is a portfolio/plural object
    (20 units, a few condos, several houses) — i.e. all of them describe a
    property OTHER than the one under discussion. True (the default) if
    there's no "sold" mention, or at least one mention that isn't portfolio
    language. (#15 Joseph Ortenzi: "I sold off 20 units" while describing a
    18-year rehab career — not the subject property, which he separately
    said he loves and isn't selling.)
    """
    sold_lines = [
        _body(m) for m in (messages or [])
        if _sender(m) in ("contact", "lead") and re.search(r"\bsold\b", _body(m), re.I)
    ]
    if not sold_lines:
        return True
    return not all(_PORTFOLIO_OBJECT_RE.search(line) for line in sold_lines)
