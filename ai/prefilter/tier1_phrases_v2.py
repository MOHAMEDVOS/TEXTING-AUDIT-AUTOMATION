# -*- coding: utf-8 -*-
"""
Tier 1 v2 — Funnel-aware exact phrase / regex matching.

Adds funnel_tier (WF / MF / NF) to every short-circuit decision so that
downstream ML tiers can calibrate expectations correctly.

Conservative rules:
  ESCALATE   → risky signal detected; Groq must do the full audit.
  SHORT-CIRCUIT → trivially clean for this funnel tier; skip Groq.
  None       → pass through to Tier 2.

False-positive budget: ZERO.  If there is any doubt, return None.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ._pipeline_types import PipelineResult as PrefilterResult

logger = logging.getLogger(__name__)


# ── Pillar keywords ───────────────────────────────────────────────────────────
#
# Used to detect how many pillars the contact has revealed.  Funnel thresholds:
#   WF = 0 pillars needed   (raised-hand = Lead immediately)
#   MF = 2 pillars needed   (2-of-4)
#   NF = 3 pillars needed   (3-of-4)

_PILLAR_PATTERNS = {
    "condition": re.compile(
        r"\b(fix(er.?upper|ing|ed)?|repair|damaged|needs\s+work|as.?is|rough\s+shape"
        r"|foundation|roof|mold|flood|fire\s+damage|teardown|renovate|update)"
        r"\b",
        re.I,
    ),
    "price": re.compile(
        r"(\$\s?\d[\d,\.]*\s*k?\b|\d[\d,\.]*\s*k\s+(?:cash|asking|listing|price)"
        r"|\b(asking|listing|offer|price|worth|value|market)\s+.{0,20}\$?\d)"
        r"|\$\s?\d[\d,\.]+\s*(million|M)\b",
        re.I,
    ),
    "motivation": re.compile(
        r"\b(divorce|inherit|estate|probate|relocat|moving|downsize|upgrade|upsize"
        r"|behind\s+on|foreclos|owe|debt|financial|job\s+(loss|transfer)|retire"
        r"|health|illness|widow|death\s+in|selling\s+because|need\s+to\s+sell"
        r"|want\s+to\s+sell|ready\s+to\s+sell)\b",
        re.I,
    ),
    "timeline": re.compile(
        r"\b(asap|right\s+away|soon|urgently|couple\s+(of\s+)?(weeks|months)"
        r"|few\s+(weeks|months)|end\s+of\s+(the\s+)?(month|year|summer|spring)"
        r"|next\s+(week|month|year|spring|summer|fall|winter)"
        r"|no\s+rush|whenever|eventually|not\s+in\s+a\s+hurry"
        r"|moving\s+(out|away|soon)|need\s+to\s+(move|be\s+out)\s+(by|in|next))\b",
        re.I,
    ),
}

_PILLAR_THRESHOLD = {"WF": 0, "MF": 2, "NF": 3}


# ── Opt-out vocabulary ────────────────────────────────────────────────────────

_OPT_OUT_PATTERNS = [
    re.compile(r"\bstop\s+texting\b", re.I),
    re.compile(r"\bstop\s+messaging\b", re.I),
    re.compile(r"\bstop\s+(contact(ing)?|calling)\s+me\b", re.I),
    re.compile(r"\bremove\s+(me|us)\b", re.I),
    re.compile(r"\bremove\s+my\s+(name|number)\b", re.I),
    re.compile(r"\bremove\s+name\s+from\s+list\b", re.I),
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"\bleave\s+me\s+alone\b", re.I),
    re.compile(r"\bdon'?t\s+(contact|message|text)\s+me\b", re.I),
    re.compile(r"\bstop\s+bothering\s+me\b", re.I),
    re.compile(r"\bdo\s+not\s+(call|contact)\b", re.I),
    re.compile(r"\btake\s+(me|us)\s+off\b", re.I),
    re.compile(r"\btake\s+off\s+(the|your)?\s*list\b", re.I),
    re.compile(r"\bi\s+said\s+no\b", re.I),
    re.compile(r"\bplease\s+don'?t\s+(ask|contact|text|call)\s+(again|me)\b", re.I),
    re.compile(r"\bno\s+please\s+don'?t\s+(ask|contact|text|call)\b", re.I),
    # Note: "nothing about this interests me. Give me a number..." is NOT opt-out
    # Only match when it's a hard stop (followed by end of message or hard punctuation)
    re.compile(r"\bif\s+you\s+could\s+stop\b", re.I),
    re.compile(r"^stop[.!]*$", re.I | re.MULTILINE),
    re.compile(r"\bnothing\s+about\s+this.{0,20}interests\s+me[.!]\s*$", re.I | re.MULTILINE),
]

# ── Profanity / clear hostility → DNC short-circuit (no Groq needed) ─────────
# These are unambiguous: contact is hostile, conversation is done.

_HARASSMENT_DNC_PATTERNS = [
    re.compile(r"\balone\s+time\b", re.I),
    re.compile(r"\b(meeting|seeing)\s+you\b.{0,80}\b(fun|nice|good)\b.{0,80}\b(alone|private)\b", re.I),
    re.compile(r"\bgo\s+somewhere\b.{0,80}\b(alone|private)\b", re.I),
    re.compile(r"\bjust\s+want\s+to\s+(meet|see)\s+you\b", re.I),
    re.compile(r"\bnot\s+(about|for)\s+(the\s+)?(house|property)\b.{0,80}\b(you|meet|date)\b", re.I),
]

_PROFANITY_DNC_PATTERNS = [
    re.compile(r"\b(fuck|shit|bitch|asshole|bastard|piss\s+off|go\s+to\s+hell)\b", re.I),
    re.compile(r"\b(f\*\*\*|b\*\*\*\*|s\*\*\*|a\*\*hole)\b", re.I),
    re.compile(r"\bson\s+of\s+a\s+(bitch|b\*\*\*\*|b|whore)\b", re.I),
    re.compile(r"\byou\s+suck\b", re.I),
    re.compile(r"\bhow\s+(fucking\s+)?rude\b", re.I),
]

_HARD_OPTOUT_DNC_PATTERNS = [
    re.compile(r"\b(scammer|scam|fraud|harassing\s+me|harassment)\b", re.I),
    re.compile(r"\bstop\s+(texting|contacting|calling|messaging)\s+me\b", re.I),
    re.compile(r"\bstop\s+asking\b", re.I),
    re.compile(r"\b(do\s+not\s+contact|do\s+not\s+(ever\s+)?text)\b", re.I),
    re.compile(r"\bleave\s+me\s+alone\b", re.I),
    re.compile(r"\bobviously\s+not\s+for\s+sale\b", re.I),
    re.compile(r"\bnever\s+(sale|sell)\b", re.I),
    re.compile(r"\bstop[.!]*(?:\s|$)", re.I),  # terminal opt-out command
]

# ── Suspicious / risky patterns → escalate to Groq for full audit ────────────

_SUSPICIOUS_PATTERNS = [
    re.compile(r"\b(my\s+offer\s+is|i('|'|')?ll\s+offer\s+you|i\s+will\s+offer\s+you|offering\s+you)\s*\$?\d{3,}", re.I),
]

# ── Agent price-affirmation patterns ─────────────────────────────────────────
# Contact states asking price → agent replies with affirmation ("Great!", "Perfect!")
# This implies agent accepted the price without negotiation — script violation.
# Detection: agent message starts with or contains a strong positive affirmation
# within a short distance of the contact's price statement.

_AGENT_PRICE_AFFIRMATION_RE = re.compile(
    r"^\s*(great|perfect|awesome|wonderful|fantastic|excellent|sounds\s+good|that.{0,10}works"
    r"|that.{0,10}great|that.{0,10}perfect|that.{0,10}amazing|love\s+that"
    r"|good\s+to\s+know|good\s+number|fair\s+price|fair\s+enough|fair\s+enough)"
    r"[!.,]?\s",
    re.I | re.MULTILINE,
)

# Price pattern to detect when contact has stated a price
_CONTACT_PRICE_RE = re.compile(
    r"(\$\s?\d[\d,\.]*\s*k?\b"
    r"|\d[\d,\.]*\s*k\b"
    r"|\b\d{3,}[,]\d{3}\b"
    r"|\b(asking|listing|want|price|worth)\s+.{0,15}\$?\d)"
    r"|\$\s?\d[\d,\.]+\s*(million|M)\b",
    re.I,
)

# ── Agent asked for info contact already gave ────────────────────────────────
# Detects when agent asks for price/motivation/timeline/condition that the
# contact already stated earlier in the conversation.

_AGENT_ASKS_CONDITION_RE = re.compile(
    r"\b(tell\s+me\s+(a\s+bit\s+)?about\s+the\s+propert\w*s?\s+condition"
    r"|what.{0,20}condition.{0,20}(propert|home|house|it\s+in)"
    r"|how.{0,20}(condition|shape)\s+(is|are).{0,15}(propert|home|house)"
    r"|any\s+(upgrades?|repairs?|updates?|improvements?|renovation|work\s+done)"
    r"|done\s+any\s+(upgrades?|repairs?|updates?|work|renovation)"
    r"|(upgrades?|repairs?|updates?|renovation).{0,30}(recently|lately|done|made)"
    r"|what\s+(work|repairs?|upgrades?).{0,20}(done|made|completed))\b",
    re.I,
)

# Contact gave condition when they describe physical property details:
# rooms, features, materials, dimensions, fencing, appliances, etc.
# Threshold: message must be at least 30 chars with structural/material words.
_CONTACT_CONDITION_RE = re.compile(
    r"\b(bedroom|bathroom|bath|kitchen|living\s+room|garage|carport|porch|deck"
    r"|roof|foundation|floor(ing)?|carpet|tile|hardwood|laminate"
    r"|fenc(e|ing)|gate|driveway|yard|pool|basement|attic"
    r"|window|door|siding|brick|stucco|paint(ed)?"
    r"|appliance|hvac|ac|heat|plumb|electric|water\s+heater"
    r"|sqft|sq\s*ft|square\s+feet|stories|story|acre"
    r"|updated|renovated|remodel|new\s+(roof|floor|kitchen|bath|hvac|ac|fence|paint)"
    r"|great\s+condition|good\s+condition|excellent\s+condition|needs?\s+(work|repair|update)"
    r"|as.?is|fixer|move.?in\s+ready)\b",
    re.I,
)

_AGENT_ASKS_PRICE_RE = re.compile(
    r"\b(price\s+(in\s+mind|you('|')?re\s+looking)|have\s+a\s+(ballpark|price|number)"
    r"|what.{0,20}(asking|price|looking\s+to\s+get|want\s+for\s+it)"
    r"|how\s+much.{0,20}(looking|want|asking|hoping)"
    r"|what.{0,15}(number|figure).{0,15}(mind|thinking)"
    r"|do\s+you\s+(have\s+a\s+price|know\s+what\s+you.{0,10}want))\b",
    re.I,
)

_AGENT_ASKS_MOTIVATION_RE = re.compile(
    r"\b(reason\s+for\s+(selling|the\s+sale)"
    r"|what.{0,20}(motivat|reason|making\s+you\s+consider|bringing\s+you)"
    r"|why.{0,20}(sell|move|leaving)"
    r"|mind\s+(me\s+)?asking.{0,30}(reason|why|situation|selling)"
    r"|share.{0,20}(reason|situation|why)"
    r"|what\s+sparked.{0,20}(interest|decision|reason)"
    r"|what.{0,20}(prompted|led|drove|brought)\s+you"
    r"|curious.{0,20}what.{0,20}(making|motivat|consider|deal)"
    r"|what.{0,20}(is\s+it\s+that.{0,10}making|got\s+you\s+thinking)"
    r"|behind\s+the\s+(decision|sale|move|reason))\b",
    re.I,
)

_AGENT_ASKS_TIMELINE_RE = re.compile(
    r"\b(timeline|time\s+frame|when.{0,20}(looking|hoping|plan)"
    r"|how\s+(soon|quickly).{0,20}(sell|move|close)"
    r"|within\s+the\s+next|by\s+when|what.{0,15}timeline)\b",
    re.I,
)

_CONTACT_MOTIVATION_RE = re.compile(
    r"\b(divorce|inherit|estate|probate|relocat|moving|downsize|upgrade|upsize"
    r"|behind\s+on|foreclos|owe|debt|financial|job\s+(loss|transfer|relocation)"
    r"|retir(e|ed|ing|ement)?|health|illness|widow|death\s+in|selling\s+because|need\s+to\s+sell"
    r"|want\s+to\s+sell|ready\s+to\s+sell|rental|have\s+another\s+place"
    r"|already\s+have|bought\s+another|moving\s+to|going\s+to"
    # Real-world phrases from audited conversations
    r"|bought\s+(a\s+)?(new\s+)?(land|house|home|property|place)"
    r"|had\s+another\s+(house|home|place)\s+built"
    r"|have\s+another\s+(house|home|place|property)"
    r"|looking\s+to\s+(minimize|invest|downsize|upsize|upgrade)"
    r"|invest\s+in\s+(more|other|new)\s+(condo|propert|home|house)"
    r"|bigger\s+(house|home|space|place|land|property)"
    r"|too\s+(big|small|much\s+space|large)\s+(for\s+us|for\s+me)?"
    r"|kids?\s+(left|grew|grown|moved\s+out)"
    r"|empty\s+nest"
    r"|upsiz(e|ing)(\s+(for\s+)?(family|kids?|children))?"
    r"|out\s+of\s+(state|the\s+city|town|country|the\s+area)"
    r"|minimiz(e|ing)"
    r"|consolidat(e|ing))\b",
    re.I,
)

_CONTACT_TIMELINE_RE = re.compile(
    r"\b(asap|right\s+away|soon|urgently|couple\s+(of\s+)?weeks"
    r"|few\s+(weeks|months)|end\s+of\s+(the\s+)?(month|year|summer|spring)"
    r"|next\s+(week|month|year|spring|summer|fall|winter)"
    r"|by\s+summer|this\s+summer|summers?\s+end|gradually"
    r"|no\s+rush|whenever|eventually|not\s+in\s+a\s+hurry"
    r"|moving\s+(out|away|soon)|need\s+to\s+(move|be\s+out)\s+(by|in|next))\b",
    re.I,
)

# ── Robotic duplicate message ─────────────────────────────────────────────────
# Two agent messages are "identical" if their similarity ratio >= 90%.
# Uses SequenceMatcher (stdlib) — no extra deps, fast enough for short SMS.

def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()

def _opener(body: str) -> str:
    """Return the first sentence of a message (up to 80 chars), normalised."""
    body = re.sub(r"\s+", " ", body.strip().lower())
    m = re.search(r"^(.{20,80}?)[.!?]", body)
    return m.group(1).strip() if m else body[:80]

def _msg_date(m: dict) -> str:
    """
    Return a stable date string for a scraped message so two messages can be
    compared to see if they happened on the same calendar day.

    Scraped messages carry two separate fields:
      "date" — e.g. "Thursday, March 26, 2026"  (empty "" for today's messages)
      "time" — e.g. "05:59 PM"                  (clock only, no date part)
      "sent_at" — ISO datetime if stored in DB   (e.g. "2026-05-08T17:59:00")

    Priority:
      1. DB ISO datetime in "sent_at" → extract YYYY-MM-DD
      2. Scraped "date" field (non-empty) → use as-is (stable identifier)
      3. Scraped "date" field is "" (today's messages) → return "__today__"
         All same-session today messages share this sentinel, so same-day
         comparison still works correctly.
    """
    # DB path: ISO datetime from sent_at
    sent_at = m.get("sent_at") or m.get("timestamp")
    if sent_at:
        s = str(sent_at)
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
        try:
            from datetime import datetime
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
        except Exception:
            pass

    # Scraper path: use the "date" divider string.
    # date_field is None  → key absent entirely → no date info, skip
    # date_field == ""    → key present but empty → today's messages (no divider shown)
    # date_field == "None"→ str(None) bug from old code → treat as absent
    date_field = m.get("date")
    if date_field is None or date_field == "None":
        return ""          # no date info — can't enforce same-day rule
    if date_field == "":
        return "__today__" # today's messages share this sentinel
    return date_field      # e.g. "Thursday, March 26, 2026"


# Contact responses that count as "answered" for pillar duplicate detection.
# Covers: rejections (no/never/not interested), openness (yes/maybe/sure/ok),
# soft acceptance ("that would be fine"), and brief number/timeframe answers.
_CONTACT_ANSWERED_PATTERNS = [
    re.compile(r"\bno\b", re.I),
    re.compile(r"\bnope\b", re.I),
    re.compile(r"\bnot\s+(interested|now|really|at\s+all|right\s+now)\b", re.I),
    re.compile(r"\b(never|stop|don'?t)\b", re.I),
    re.compile(r"\btry\s+(back|again)\b", re.I),
    re.compile(r"\byes\b", re.I),
    re.compile(r"\byeah\b", re.I),
    re.compile(r"\byep\b", re.I),
    re.compile(r"\bsure\b", re.I),
    re.compile(r"\bok(ay)?\b", re.I),
    re.compile(r"\bmaybe\b", re.I),
    re.compile(r"\bsoon\b", re.I),
    re.compile(r"\b(would|that'?d|that\s+would)\s+be\s+(fine|good|great|ok)\b", re.I),
    re.compile(r"\bopen\s+to\b", re.I),
    re.compile(r"\b\d+\s*(months?|years?|weeks?|days?)\b", re.I),
    re.compile(r"\$\s*\d", re.I),
    re.compile(r"\b\d{2,3}\s*(k|thousand|million|mil)\b", re.I),
]


def _contact_answered_pillar(body: str) -> bool:
    """True if contact's message reads as a real answer (yes/no/maybe/number/etc)."""
    if len(body.strip()) < 2:
        return False
    return any(p.search(body) for p in _CONTACT_ANSWERED_PATTERNS)


def _classify_pillar_question(body: str) -> str | None:
    """Return which pillar an agent message asks about, or None."""
    if _AGENT_ASKS_PRICE_RE.search(body):
        return "asking price"
    if _AGENT_ASKS_TIMELINE_RE.search(body):
        return "closing timeline"
    if _AGENT_ASKS_MOTIVATION_RE.search(body):
        return "reason for selling"
    if _AGENT_ASKS_CONDITION_RE.search(body):
        return "condition"
    return None


def _parse_date_obj(date_str: str):
    """
    Convert a _msg_date() string into a datetime.date object for arithmetic.
    Returns None when the date can't be parsed (flag will be skipped).

    Handles:
      - ISO format:   "2026-05-08"
      - Scraper full: "Thursday, May 8, 2026"  /  "May 8, 2026"
      - Sentinel:     "__today__"  → today's date
    """
    from datetime import date, datetime
    if not date_str:
        return None
    if date_str == "__today__":
        return date.today()
    # ISO date
    if len(date_str) == 10 and date_str[4] == "-":
        try:
            return date.fromisoformat(date_str)
        except ValueError:
            pass
    # Full scraper string e.g. "Thursday, May 8, 2026" or "May 8, 2026"
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def _agent_sent_duplicate(messages: list[dict]) -> tuple[bool, str]:
    """
    Flag when the agent re-asked the SAME pillar question too soon after the
    contact already answered.

    Window: same calendar day OR the very next calendar day (≤ 1 day gap).

    Trigger requires ALL THREE:
      1. Agent asked pillar X on Day N
      2. Contact gave a real answer AFTER that ask (yes / no / number / etc.)
      3. Agent asked pillar X again on Day N or Day N+1

    Examples:
      Ask May 8 → contact "No" → ask again May 8      → FLAGGED  (same day)
      Ask May 8 → contact "No" → ask again May 9      → FLAGGED  (next day, too soon)
      Ask May 8 → contact "No" → ask again May 10+    → NOT flagged (2+ days = fresh follow-up)
      Ask May 8 → ask again May 8 (no reply between)  → NOT flagged (rapid double-send)

    Pillars checked: asking price, closing timeline, reason for selling, condition.

    Returns (flag, pillar_name). pillar_name empty when flag is False.
    """
    from datetime import timedelta

    # Per pillar: track the date of the first ask (as a date object) and whether
    # the contact has answered since that ask.
    # {pillar: {"first_ask_date": date | None, "contact_answered": bool}}
    state: dict[str, dict] = {}

    for m in messages:
        sender = _sender(m)
        body   = _body(m)
        if not body:
            continue

        if sender == "contact":
            # Contact replied — mark ALL pillars currently being tracked as answered
            if _contact_answered_pillar(body):
                for pillar_state in state.values():
                    if not pillar_state["contact_answered"]:
                        pillar_state["contact_answered"] = True
            continue

        if not _is_agent(m):
            continue

        pillar = _classify_pillar_question(body)
        if not pillar:
            continue

        date_str = _msg_date(m)
        if not date_str:
            continue  # no date info — can't enforce the window rule

        ask_date = _parse_date_obj(date_str)
        if ask_date is None:
            continue

        if pillar not in state:
            # First time asking this pillar — start tracking
            state[pillar] = {"first_ask_date": ask_date, "contact_answered": False}
        else:
            ps = state[pillar]
            first_date = ps["first_ask_date"]
            gap = (ask_date - first_date).days  # always >= 0 (messages are chronological)

            if ps["contact_answered"] and gap <= 1:
                # Contact already answered AND agent is re-asking within 0–1 days → FLAG
                return True, pillar
            elif gap >= 2:
                # 2+ days later → treat as a fresh legitimate follow-up, reset tracking
                state[pillar] = {"first_ask_date": ask_date, "contact_answered": False}
            # else gap==0 or gap==1 but contact hasn't answered yet → rapid double-send, skip

    return False, ""




# ── Bluffer / paranoid / nonsense reply detection ─────────────────────────────
# Contacts who reply with absurd, threatening, or paranoid statements that don't
# engage with the property question — should be labeled "Bluffer" not Lead/NI.
#
# Also covers the price-bluffer case: contact quotes $1M+ as a brush-off
# (e.g. "1 million dollars", "$2 million") — a classic bluffing move on a typical
# 2-3 bed property to make the conversation go away.
_BLUFFER_PATTERNS = [
    re.compile(r"\b(fbi|cia|nsa|police|cops?|government|monitor(ing|ed)?\s+(this|my)\s+phone)\b", re.I),
    re.compile(r"\bmeth\s+lab\b", re.I),
    re.compile(r"\b(illegals?|illegal\s+immigrants?)\s+(to\s+)?(help|patch|work)", re.I),
    re.compile(r"\bdrug(s|\s+den|\s+house)?\b.{0,40}\b(in|at)\s+(the\s+)?(house|property|home)\b", re.I),
    re.compile(r"\bhaunted\b", re.I),
    re.compile(r"\bbur(ned|nt)\s+(down|in)\b.{0,40}\b(explosion|fire|meth)\b", re.I),
    re.compile(r"\b(buried|body|bodies|corpse)\s+(in|under|behind)\b", re.I),
    # ── Price bluffer: contact quotes $1M+ as a brush-off ──────────────────────
    # Word-form: "a million", "one million", "two million dollars", etc.
    re.compile(
        r"\b((a|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
        r"(million|mil)\b",
        re.I,
    ),
    # Numeric with unit: "$2M", "$1.5 million", "2,000,000"
    re.compile(r"(?:\$\s*)?(\d{1,4})(?:[.,](\d{3}))*\s*(million|mil|m\b)", re.I),
    # Bare dollar amount >= $1,000,000 e.g. "$1,000,000" or "$2000000"
    re.compile(r"\$\s*[\d,]{8,}", re.I),
    # "7-figure" / "seven figure" — implies $1M+ asking
    re.compile(r"\b(7|seven)\s*[-\s]?\s*figure\b", re.I),
]


def _has_bluffer_indicator(messages: list[dict]) -> tuple[bool, str]:
    """Return (True, matched_text) if contact made a clearly bluff/paranoid statement."""
    for m in messages:
        if _sender(m) != "contact":
            continue
        body = _body(m)
        for pat in _BLUFFER_PATTERNS:
            match = pat.search(body)
            if match:
                return True, match.group(0)
    return False, ""


# ── Above Market Value detection ──────────────────────────────────────────────
# Contact responds with a clearly inflated price (>= $1M for a property with
# no luxury indicators in the conversation). Stripped: $/k/M/comma.
_PRICE_RE = re.compile(
    r"(?:\$\s*)?(\d{1,4})(?:[\.,](\d{3}))*\s*(million|mil|m\b|k\b|thousand)?",
    re.I,
)

# Word-form prices: "million dollars", "a million", "two million", etc.
_WORD_PRICE_RE = re.compile(
    r"\b((a|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
    r"(million|mil)\s+(dollars?|bucks?)?",
    re.I,
)

_WORD_NUMS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# "7 figure offers are welcome" / "seven figure offer" → implies $1M+ asking price
_SEVEN_FIGURE_RE = re.compile(
    r"\b(7|seven)\s*[-\s]?\s*figure\b",
    re.I,
)


def _contact_stated_inflated_price(messages: list[dict]) -> tuple[bool, float]:
    """Return (True, dollars) if contact stated a price >= $1M."""
    for m in messages:
        if _sender(m) != "contact":
            continue
        body = _body(m)

        # "7 figure" / "seven figure" → implies $1M+ asking price
        if _SEVEN_FIGURE_RE.search(body):
            return True, 1_000_000.0

        body = body.lower()

        # Word-form: "million dollars", "two million", etc.
        word_match = _WORD_PRICE_RE.search(body)
        if word_match:
            multiplier_word = (word_match.group(2) or "one").lower()
            mult = _WORD_NUMS.get(multiplier_word, 1)
            dollars = mult * 1_000_000
            if dollars >= 1_000_000:
                return True, float(dollars)

        # Numeric form
        if not re.search(r"\d", body):
            continue

        for match in _PRICE_RE.finditer(body):
            num_str = match.group(1)
            decimals = match.group(2) or ""
            unit = (match.group(3) or "").lower()

            try:
                num = float(num_str + (f".{decimals}" if decimals else ""))
            except ValueError:
                continue

            if unit in ("million", "mil", "m"):
                dollars = num * 1_000_000
            elif unit in ("k", "thousand"):
                dollars = num * 1_000
            elif "," in body and num >= 100:
                full_match = match.group(0).replace("$", "").replace(",", "").strip()
                try:
                    dollars = float(full_match)
                except ValueError:
                    continue
            else:
                dollars = num

            if dollars >= 1_000_000:
                return True, dollars
    return False, 0.0


# ── Multiple template openers without reply (auto-rotation gone wild) ─────────
def _excessive_unanswered_openers(messages: list[dict]) -> bool:
    """
    Detect when agent sent 4+ different first-contact templates before contact replied.
    Indicates broken template auto-rotation (sender is spamming variants).
    """
    contact_first_reply_idx = next(
        (i for i, m in enumerate(messages) if _sender(m) == "contact" and _body(m)),
        None,
    )
    if contact_first_reply_idx is None:
        agent_msgs_before_reply = [m for m in messages if _is_agent(m)]
    else:
        agent_msgs_before_reply = [
            m for m in messages[:contact_first_reply_idx] if _is_agent(m)
        ]

    if len(agent_msgs_before_reply) < 4:
        return False

    # Check: are they all "first contact" style (mention the property address)?
    opener_count = 0
    for m in agent_msgs_before_reply:
        body = _body(m).lower()
        if re.search(r"\b(have you\s+(thought|considered|given)|consider(ing)?\s+selling|"
                     r"thought\s+about\s+selling|may\s+i\s+ask|hope\s+you'?re\s+doing|"
                     r"reaching\s+out|noticed\s+(your|the)\s+(property|home)|"
                     r"saw\s+your\s+(property|place|home))\b", body):
            opener_count += 1

    return opener_count >= 4

# ── Wrong Number ──────────────────────────────────────────────────────────────

_WRONG_NUMBER_PATTERNS = [
    re.compile(r"\bwrong\s+(number|person|address|house|property)\b", re.I),
    re.compile(r"\bnot\s+my\s+(number|property|house|address)\b", re.I),
    re.compile(r"\bi\s+don'?t\s+own\b", re.I),
    re.compile(r"\byou\s+have\s+the\s+wrong\b", re.I),
    re.compile(r"\bthis\s+must\s+(be|have)\s+(a\s+)?wrong\b", re.I),
    re.compile(r"\b(mis-?print|wrong\s+person)\b", re.I),
    re.compile(r"\bi\s+am\s+not\s+the\s+owner\b", re.I),
    re.compile(r"\bhave\s+not\s+owned.{0,30}(year|month)", re.I),
    re.compile(r"\bhaven'?t\s+owned\b", re.I),
    re.compile(r"\bi\s+don'?t\s+know\s+(the\s+)?address\b", re.I),
    re.compile(r"\[Name\].*\[Mobile\]", re.I),
]

_NOT_THIS_PERSON_PATTERNS = [
    re.compile(r"\b[Tt]his\s+is\s+not\s+[A-Z]\w+\b"),
    re.compile(r"\b[Ii](?:'|'|')?m[^\S\n]+not[^\S\n]+[A-Z]\w+\b"),
    re.compile(r"\bthat(?:'|'|')?s?\s+not\s+(me|my\s+name)\b", re.I),
    re.compile(r"\byou(?:'|'|')?(?:ve|'?re)\s+(got|texting\s+the)\s+wrong\b", re.I),
    re.compile(r"\bi(?:'|'|')?m\s+not\s+(?:that|the|this)\s+person\b", re.I),
    # "my name is X not Y" — contact correcting the agent's name usage
    re.compile(r"\bmy\s+name\s+is\s+\w+\s+not\s+\w+\b", re.I),
    re.compile(r"\b(not\s+)?(?:called|named)\s+[A-Z]\w+\b"),
    # "Not [Name]" — must be a proper noun (capital letter), not a common word
    # Exclude: "Not selling", "Not interested", "Not ready", "Not now", "Not yet", "Not sure"
    re.compile(
        r"^\s*[Nn]ot\s+(?!selling|interested|ready|now|yet|sure|available|going|doing|planning"
        r"|considering|looking|thinking|really|for|at|in|on|with|a|an|the|this|that|these|those)"
        r"[A-Z]\w{2,}\s*$",
        re.MULTILINE,
    ),
]

_AGENT_PITCH_AFTER_WN = [
    re.compile(r"\b(have\s+you\s+considered|would\s+you\s+be\s+open|thinking\s+about\s+selling|cash\s+offer)\b", re.I),
    re.compile(r"\b(your\s+property\s+at|selling\s+your\s+(property|home|house))\b", re.I),
    re.compile(r"\b(for\s+sale|could\s+it\s+be\s+sold|be\s+available|listing\s+it)\b", re.I),
    # Re-pitching the same property after wrong-number acknowledgment
    re.compile(r"\bcurious\s+if\s+you.{0,30}(thought|think|consider)\b", re.I),
    re.compile(r"\b(ever\s+thought|given\s+any\s+thought|thought\s+about)\s+(about\s+)?(selling|opportunities)\b", re.I),
    re.compile(r"\b(is\s+.{0,30}something\s+we\s+could\s+discuss|love\s+to\s+chat\s+whenever)\b", re.I),
]

# ── Not Interested (soft refusal) ─────────────────────────────────────────────

_NOT_INTERESTED_PATTERNS = [
    re.compile(r"\bnot\s+(at\s+this\s+time|interested|for\s+sale|looking|selling|ready|yet)\b", re.I),
    re.compile(r"\bnot\s+at\s+all(\s+likely)?\b", re.I),
    re.compile(r"\bno\s+for\s+sale\b", re.I),
    re.compile(r"\bno\s+thank(s|\s+you|\.?)?\b", re.I),
    re.compile(r"^\s*no[.,!]?\s*$", re.I | re.MULTILINE),
    re.compile(r"^\s*nope[.,!]?\s*$", re.I | re.MULTILINE),
    re.compile(r"\bi('|')?m\s+(not\s+interested|okay|good|fine)\b", re.I),
    re.compile(r"\bplease\s+stop\b", re.I),
    re.compile(r"^Disliked\s+", re.MULTILINE),
    re.compile(r"\bwe\s+don'?t\s+have\s+(a\s+)?plan\b", re.I),
    re.compile(r"\bnot\s+selling\b", re.I),          # "Not selling"
    re.compile(r"\bnot\s+ready\b", re.I),             # "Not ready to sell"
]

# Phrases that CANCEL a not-interested match (e.g. "absolutely not" in context)
_NOT_INTERESTED_CANCEL = re.compile(
    r"\b(absolutely\s+not|certainly\s+not|definitely\s+not)\b", re.I
)

# ── Sold property ─────────────────────────────────────────────────────────────

_SOLD_PATTERNS = [
    re.compile(r"\b(already|been|was|is)\s+sold\b", re.I),
    re.compile(r"\bjust\s+sold\b", re.I),
    re.compile(r"\bsold\s+(it|the\s+(house|property|home))\b", re.I),
    re.compile(r"\bproperty\s+(is|was|has\s+been)\s+sold\b", re.I),
    re.compile(r"\bsold\s+(last|a\s+few|this)\s+(month|year|week)\b", re.I),
]

# ── AbvMV guard — contacts with high prices MUST go to Groq ──────────────────
#   $300k+, $1.2M, 1 million, 1,500,000 etc.

_ABV_MV_RE = re.compile(
    r"(\$\s?\d{3,}\s*k"
    r"|\$?\s*[23456789]\d{2},\d{3}"       # $300,000 – $999,000
    r"|\$?\s*[1-9]\d{6,}"                 # $1,000,000+
    r"|\$?\s*[1-9][\d,.]+\s*(million|M)\b"  # 1.2M / 1 million
    r")",
    re.I,
)

# ── Raised hand / interested signals (used for WF leads) ─────────────────────

_RAISED_HAND_RE = re.compile(
    r"\b(yes|yeah|sure|interested|tell\s+me\s+more|how\s+much|what.{0,10}offer"
    r"|when\s+can|let.{0,5}(know|talk)|sounds\s+good|that\s+works|i'?m\s+open"
    r"|give\s+me.{0,10}(call|number|info)|can\s+you\s+(call|tell|send)"
    r"|what\s+(would|is).{0,15}(offer|price|value))\b",
    re.I,
)

# Strong positive engagement — used to suppress false duplicate flags on hot leads
_POSITIVE_ENGAGEMENT_PATTERNS = [
    re.compile(r"\b(yes|yeah|sure|absolutely|definitely)\b.{0,40}\b(sell|interested|open|talk|chat)\b", re.I),
    re.compile(r"\byes\s+(please|i\s+(am|do|would|want|can))\b", re.I),
    re.compile(r"\b(interested\s+in|open\s+to|want\s+to)\s+(sell|talk|chat|discuss|hear)\b", re.I),
    re.compile(r"\b(call|text|reach)\s+(me|us)\b", re.I),
    re.compile(r"\bhow\s+(does|do)\s+your\s+process\s+work\b", re.I),
    re.compile(r"\bwhat\s+(company|is\s+your\s+process|are\s+you\s+offering)\b", re.I),
    re.compile(r"\binterested\s+in\s+(two|2|three|3|multiple|several)\s+propert", re.I),
    re.compile(r"\bwe\s+can\s+chat\b", re.I),
    re.compile(r"\b(my\s+)?number\s+is\b", re.I),
]

# Weaker "flip" signal — contact showed engagement after initial NI
_POST_NI_FLIP_RE = re.compile(
    r"\b(ok|okay|sure|yes|yeah|neighbor|next\s+door|down\s+the\s+street|vacant|referral"
    r"|know\s+someone|someone\s+who|call\s+me|interested\s+now|changed\s+my\s+mind)\b",
    re.I,
)

_STRONG_NI_RE = re.compile(
    r"\b(absolutely\s+not|not\s+interested.{0,10}ever|never\s+selling|stop\s+texting"
    r"|do\s+not\s+(call|contact)|leave\s+(me|us)\s+alone|remove\s+(me|us)"
    r"|take\s+(me|us)\s+off|unsubscribe|scam|fraud|illegal|police|lawsuit)\b",
    re.I,
)

# ── Maybe Later ───────────────────────────────────────────────────────────────

_MAYBE_LATER_PATTERNS = [
    re.compile(r"\bmaybe\s+(later|in\s+a\s+few|in\s+the\s+future|next\s+year|sometime)\b", re.I),
    re.compile(r"\bnot\s+(yet|right\s+now)\b", re.I),
    re.compile(r"\bcheck\s+back\b", re.I),
    re.compile(r"\bpossibly\s+(soon|later|in\s+the)\b", re.I),
    re.compile(r"\bin\s+(a\s+)?couple\s+(of\s+)?months\b", re.I),
    re.compile(r"\bdown\s+the\s+road\b", re.I),
    re.compile(r"\bnear\s+future\b", re.I),
]

_SIX_MONTH_TIMELINE_RE = re.compile(
    r"\b("
    r"(close|closing|sell|sale|sold|move|moving|be\s+out|for\s+sale).{0,35}"
    r"(within|in|next|inside|over).{0,12}(6|six)\s*(month|mo)s?"
    r"|"
    r"(6|six)\s*(month|mo)s?.{0,35}"
    r"(close|closing|sell|sale|sold|move|moving|be\s+out|for\s+sale)"
    r")\b",
    re.I,
)

_SHORTER_TIMELINE_RE = re.compile(
    r"\b("
    r"30\s*(days?|d)|60\s*(days?|d)|90\s*(days?|d)"
    r"|[1-5]\s*(months?|mos?|mo)"
    r"|one\s+month|two\s+months?|three\s+months?|four\s+months?|five\s+months?"
    r"|3\s*[-/]\s*4\s*months?|3\s+to\s+4\s*months?|three\s+(to|-)\s+four\s*months?"
    r"|few\s+months?|couple\s+of\s+months?|soon|asap|right\s+away"
    r")\b",
    re.I,
)

_INTERESTED_OWNER_RE = re.compile(
    r"\b("
    r"yes|yeah|yep|sure|ok|okay|possibly|maybe|interested|open\s+to|consider"
    r"|tell\s+me\s+more|how[\s.,!?]+much|what.{0,12}offer|send.{0,12}offer"
    r"|moving|relocat|downsiz|upsiz|personal|job|retir|divorce|inherited"
    r"|need\s+to\s+sell|want\s+to\s+sell|ready\s+to\s+sell"
    r"|price|worth|value|condition|repairs?"
    r")\b",
    re.I,
)

# ── Post-NI price flip: contact says No → then asks about agent's offer ──────
# Handles punctuation between words ("how. Much do you want to pay").
_POST_NI_PRICE_FLIP_RE = re.compile(
    r"\b("
    r"how[\s.,!?]*much[\s.,!?]*(do|would|will|can|are)\s+you\s+(want|pay|offer|give|thinking)"
    r"|much[\s.,!?]*(do|would|will|can)\s+you\s+(want|pay|offer|give)"
    r"|what[\s.,!?]*(would|do|will|can)\s+you\s+(pay|offer|give)"
    r"|what.{0,20}\b(offer|buying\s+for|purchase\s+price)"
    r"|what'?s?\s+(your|the)\s+offer"
    r"|make\s+(me\s+)?an?\s+offer"
    r"|what\s+are\s+you\s+(willing|able|offering)\s+(to\s+pay)?"
    r")\b",
    re.I,
)

# ── Already-sold short-circuit patterns ─────────────────────────────────────
# These fire BEFORE T3 so the ML never misclassifies 'Sold' as 'Do Not Call'.
_SOLD_SC_PATTERNS = [
    # Bare "Sold" as the entire reply — most common case (Abrahan Preciado scenario)
    re.compile(r"^\s*sold[.!?]?\s*$", re.I | re.MULTILINE),
    # Compound sold phrases
    re.compile(r"\b(already\s+sold|just\s+sold|under\s+contract|sold\s+it)\b", re.I),
    re.compile(r"\b(it'?s?\s+sold|was\s+sold|is\s+sold|property\s+sold|place\s+sold)\b", re.I),
    re.compile(r"\bno\s+longer\s+(available|for\s+sale|on\s+the\s+market)\b", re.I),
    re.compile(r"\b(closing\s+soon|in\s+escrow|sale\s+pending)\b", re.I),
    # NOTE: "pending sale" removed from above — too generic.
    # It fires on "I dont know of any house pending sale in area" (contact is NOT confirming their own sale).
    # Instead, only match "pending sale" when clearly about the contact's own property:
    re.compile(r"\bmy\s+(home|house|property|place).{0,25}pending\s+sale\b", re.I),
    re.compile(r"\bpending\s+sale\b.{0,25}\b(my|our|the)\s+(home|house|property|place)\b", re.I),
    re.compile(r"\bit'?s?\s+pending\b", re.I),
]
# Neighbour-context / negation guard:
# Fires when contact is talking about SOMEONE ELSE's property, or saying they DON'T know of any.
_SOLD_NEIGHBOR_SC = re.compile(
    r"\b("
    r"next\s+door|neighbor|nearby|down\s+the\s+street|across\s+the\s+street|adjacent"
    r"|don'?t\s+know|do\s+not\s+know|no\s+idea"
    r"|any\s+(house|home|property|place)"
    r"|in\s+(the\s+)?area|in\s+(the\s+)?neighborhood"
    r")\b",
    re.I,
)


_QUESTION_RE = re.compile(
    r"\?|\b(what|where|when|who|which|how|why)\b",
    re.I,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _body(m: dict) -> str:
    return (m.get("body") or m.get("message") or "").strip()


def _sender(m: dict) -> str:
    return (m.get("sender") or "").lower()


def _is_agent(m: dict) -> bool:
    """True if this message was sent by the agent (not contact, not system)."""
    s = _sender(m)
    return s not in ("contact", "system", "")


def _detect_pillars(contact_msgs: list[dict]) -> list[str]:
    contact_text = " ".join(_body(m) for m in contact_msgs)
    return [pillar for pillar, pat in _PILLAR_PATTERNS.items() if pat.search(contact_text)]


def _future_rebuttal_sequence_violation(messages: list[dict]) -> tuple[bool, str]:
    """
    Check if agent jumped to 6-month window incorrectly.
    Returns (violated, flag_text).

    Two sub-cases:
    1. Owner already stated a SHORT timeline (soon/asap/few months) →
       agent ignored it and asked about 6 months anyway.
    2. Owner showed interest but agent skipped shorter timeline attempts
       and went straight to 6 months.
    """
    lead_showed_interest   = False
    owner_gave_short_tl    = False  # owner explicitly said soon/asap/few months
    saw_shorter_timeline   = False  # agent tried a shorter window first

    for message in messages:
        sender = _sender(message)
        body   = _body(message)

        if sender == "contact":
            is_disinterest = (
                _STRONG_NI_RE.search(body)
                or any(p.search(body) for p in _NOT_INTERESTED_PATTERNS)
            )
            if not is_disinterest and _INTERESTED_OWNER_RE.search(body):
                lead_showed_interest = True
            if _SHORTER_TIMELINE_RE.search(body):
                owner_gave_short_tl = True
            continue

        if sender != "agent":
            continue

        if _SHORTER_TIMELINE_RE.search(body):
            saw_shorter_timeline = True

        if lead_showed_interest and not saw_shorter_timeline and _SIX_MONTH_TIMELINE_RE.search(body):
            if owner_gave_short_tl:
                return True, (
                    "Owner already stated a short timeline; "
                    "agent ignored it and asked about 6 months instead."
                )
            return True, (
                "Started future rebuttal with 6-month window before shorter timeline."
            )

    return False, ""


def _clean_result(
    contact_name: str,
    *,
    summary: str,
    scores: dict,
    funnel_tier: str,
    funnel_stage: str = "none",
    label_assigned: str = "Stopped Responding",
    label_reason: str = "No engagement, no rule violations.",
    pillars: list | None = None,
    actual_label: str | None = None,
) -> dict:
    # Use the label the texter actually set as label_assigned
    real_label = actual_label.strip() if actual_label else label_assigned
    expected_label = label_assigned.strip() if label_assigned else ""
    if actual_label:
        label_correct = _label_key(real_label) == _label_key(expected_label)
        label_should_be = expected_label
        if label_correct:
            label_reason = "ML rule matched the assigned label."
        else:
            label_reason = f"ML rule matched {expected_label}."
    else:
        label_correct = None
        label_should_be = None

    return {
        "compliance_score": scores["compliance_score"],
        "sentiment_score": scores["sentiment_score"],
        "professionalism_score": scores["professionalism_score"],
        "script_adherence_score": scores["script_adherence_score"],
        "funnel_tier": funnel_tier,
        "funnel_stage_reached": funnel_stage,
        "pillars_gathered": pillars or [],
        "rebuttals_used": [],
        "label_assigned": real_label,
        "label_correct": label_correct,
        "label_should_be": label_should_be,
        "label_reason": label_reason if label_correct is not None else "",
        "red_flags": [],
        "actions_triggered": [],
        "summary": summary,
        "model_used": "prefilter_t1_v2",
        "contact_name": contact_name,
    }


def _label_key(label: str | None) -> str:
    normalized = re.sub(r"\s+", " ", (label or "").strip()).lower()
    if normalized in {"dnc", "do not call"}:
        return "do not call"
    return normalized


# ── Main entry point ──────────────────────────────────────────────────────────

def evaluate(
    messages: list[dict],
    funnel_tier: str,
    agent_name: str,
    contact_name: str,
    assigned_labels: list[str] | None = None,
) -> Optional[PrefilterResult]:
    """
    Evaluate a conversation against Tier-1 rules with funnel awareness.

    Args:
        messages:     List of message dicts with keys: sender, body/message.
        funnel_tier:  "WF" | "MF" | "NF"  (Wide / Middle / Narrow Funnel).
        agent_name:   Agent's display name (for logging).
        contact_name: Contact's name (for logging and result dict).

    Returns:
        PrefilterResult(decision="escalate")      — risky, send to Groq.
        PrefilterResult(decision="short_circuit") — trivially clean, skip Groq.
        None                                      — defer to Tier 2.
    """
    if not messages:
        return None

    funnel_tier = (funnel_tier or "NF").upper().strip()
    if funnel_tier not in _PILLAR_THRESHOLD:
        funnel_tier = "NF"

    # Real label the texter set — passed to every _clean_result call
    _actual_label = (assigned_labels or [""])[0].strip() if assigned_labels else None

    contact_msgs = [m for m in messages if _sender(m) == "contact"]
    contact_text = " \n ".join(_body(m) for m in contact_msgs)

    # ── Already sold: contact replied "Sold" / "under contract" / etc. ──────────
    # Must be checked BEFORE T3 runs — T3 ML misclassifies these as 'Do Not Call'
    # because they are very short messages with no strong feature signal.
    _sold_match = any(p.search(contact_text) for p in _SOLD_SC_PATTERNS)
    _sold_neighbor = _SOLD_NEIGHBOR_SC.search(contact_text)
    if _sold_match and not _sold_neighbor:
        from . import summary_builder as _sb
        _sold_scores = {
            "compliance_score": 100, "sentiment_score": 80,
            "professionalism_score": 90, "script_adherence_score": 100,
        }
        _sold_summary = _sb.build_summary(
            messages, agent_name, contact_name, _sold_scores,
            model_used="prefilter_t1_v2",
        )
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.97,
            notes=f"[{funnel_tier}] property already sold — contact reply: '{contact_text[:40]}'",
            predicted_scores=_sold_scores,
            result=_clean_result(
                contact_name,
                summary=_sold_summary,
                scores=_sold_scores,
                funnel_tier=funnel_tier,
                funnel_stage="none",
                label_assigned="Sold",
                label_reason="ML detected sold-property language — property is already sold or under contract.",
                actual_label=_actual_label,
            ),
        )

    for pat in _HARASSMENT_DNC_PATTERNS:
        if pat.search(contact_text):
            scores = {"compliance_score": 100, "sentiment_score": 55,
                      "professionalism_score": 95, "script_adherence_score": 90}
            return PrefilterResult(
                tier_hit=1, decision="short_circuit", confidence=0.98,
                notes=f"sexual/private-meeting harassment: /{pat.pattern}/",
                result=_clean_result(
                    contact_name,
                    summary=(
                        f"{contact_name} used sexual/private-meeting language and did not "
                        "continue as a serious property owner. Texter remained professional; "
                        "conversation should be treated as DNC/unserious lead."
                    ),
                    scores=scores,
                    funnel_tier=funnel_tier,
                    funnel_stage="none",
                    label_assigned="DO Not Call",
                    label_reason="Contact used sexual/private-meeting harassment language.",
                    actual_label=_actual_label,
                ),
            )

    # ── FIRST (pre-check): opt-out ignored takes priority over profanity DNC ──
    # If contact used opt-out language AND agent continued (≥2 msgs after),
    # F1 is the primary violation — score it before profanity DNC fires.
    _all_msgs_early = messages  # messages not yet normalised at this point
    for _pat_e1 in _OPT_OUT_PATTERNS:
        if _pat_e1.search(contact_text):
            _optout_idx_early = next(
                (i for i, m in enumerate(_all_msgs_early)
                 if _sender(m) == "contact" and _pat_e1.search(_body(m))),
                None,
            )
            if _optout_idx_early is not None:
                _agent_after_early = [
                    m for m in _all_msgs_early[_optout_idx_early + 1:]
                    if _is_agent(m)
                ]
                if len(_agent_after_early) >= 2:
                    from . import summary_builder
                    _e1_scores = {
                        "compliance_score": 0,
                        "sentiment_score": 60,
                        "professionalism_score": 70,
                        "script_adherence_score": 60,
                    }
                    _e1_summary = summary_builder.build_summary(
                        _all_msgs_early, agent_name, contact_name, _e1_scores,
                        model_used="prefilter_t1_v2",
                    )
                    return PrefilterResult(
                        tier_hit=1, decision="short_circuit", confidence=1.0,
                        notes=f"F1: opt-out ignored — agent sent {len(_agent_after_early)} msgs after contact opted out",
                        predicted_scores=_e1_scores,
                        result={
                            "compliance_score": _e1_scores["compliance_score"],
                            "sentiment_score": _e1_scores["sentiment_score"],
                            "professionalism_score": _e1_scores["professionalism_score"],
                            "script_adherence_score": _e1_scores["script_adherence_score"],
                            "funnel_tier": funnel_tier,
                            "funnel_stage_reached": "none",
                            "pillars_gathered": [],
                            "rebuttals_used": [],
                            "label_assigned": _actual_label or "DO Not Call",
                            "label_correct": None,
                            "label_should_be": None,
                            "label_reason": "",
                            "red_flags": ["Continued texting after explicit opt-out."],
                            "actions_triggered": ["Not Following Lead Flow"],
                            "summary": _e1_summary,
                            "model_used": "prefilter_t1_v2",
                            "contact_name": contact_name,
                        },
                    )
            break

    # ── FIRST: profanity / clear hostility → DNC short-circuit ──────────────
    # No Groq needed — hostile contact is an unambiguous DO Not Call.
    for pat in _PROFANITY_DNC_PATTERNS:
        if pat.search(contact_text):
            matched = pat.pattern
            from . import summary_builder
            scores = {"compliance_score": 100, "sentiment_score": 60,
                      "professionalism_score": 95, "script_adherence_score": 80}
            summary = summary_builder.build_summary(messages, agent_name, contact_name, scores, model_used="prefilter_t1_v2")
            return PrefilterResult(
                tier_hit=1, decision="short_circuit", confidence=0.97,
                notes=f"profanity/hostility: /{matched}/",
                result=_clean_result(
                    contact_name,
                    summary=summary,
                    scores=scores,
                    funnel_tier=funnel_tier,
                    funnel_stage="none",
                    label_assigned="DO Not Call",
                    label_reason="Contact used hostile or profane language.",
                    actual_label=_actual_label,
                ),
            )

    # ── HARD OPT-OUTS → DNC short-circuit ──────────────
    # Unambiguous opt-outs ("stop texting me", "harassing me") must be DNC.
    for pat in _HARD_OPTOUT_DNC_PATTERNS:
        if pat.search(contact_text):
            matched = pat.pattern
            from . import summary_builder
            scores = {"compliance_score": 100, "sentiment_score": 80,
                      "professionalism_score": 95, "script_adherence_score": 90}
            summary = summary_builder.build_summary(messages, agent_name, contact_name, scores, model_used="prefilter_t1_v2")
            return PrefilterResult(
                tier_hit=1, decision="short_circuit", confidence=0.97,
                notes=f"explicit opt-out: /{matched}/",
                result=_clean_result(
                    contact_name,
                    summary=summary,
                    scores=scores,
                    funnel_tier=funnel_tier,
                    funnel_stage="none",
                    label_assigned="DO Not Call",
                    label_reason="Contact explicitly requested to stop communication.",
                    actual_label=_actual_label,
                ),
            )

    pillar_threshold = _PILLAR_THRESHOLD[funnel_tier]

    # Normalize message fields once
    messages = [
        {**m, "body": _body(m), "sender": _sender(m)}
        for m in messages
    ]

    contact_msgs = [m for m in messages if _sender(m) == "contact"]
    agent_msgs   = [m for m in messages if _is_agent(m)]
    contact_text = " \n ".join(_body(m) for m in contact_msgs)
    all_text     = " \n ".join(_body(m) for m in messages)

    _f7_violated, _f7_flag_text = _future_rebuttal_sequence_violation(messages)

    # ── GUARD: Contact asked a question, agent never replied ──────────────────
    if contact_msgs and agent_msgs:
        last_q_idx = None
        for i, m in enumerate(messages):
            if not _is_agent(m) and _QUESTION_RE.search(_body(m)):
                last_q_idx = i
        if last_q_idx is not None:
            agent_replied = any(_is_agent(m) for m in messages[last_q_idx + 1:])
            if not agent_replied:
                return PrefilterResult(
                    tier_hit=1, decision="escalate", confidence=1.0,
                    notes="contact asked question; agent did not reply — potential abandonment",
                )

    # ── Check 1: explicit opt-out — clean case handled in Check 6 ───────────
    # F1 (agent continued after opt-out) is handled in the pre-check above.
    # This break ensures the clean opt-out path falls through to Check 6.
    for pat in _OPT_OUT_PATTERNS:
        if pat.search(contact_text):
            break  # clean opt-out → handled in Check 6

    # ── Checks 2 / 2b / 2b2 / 2b3 / 2c / 2d — collect ALL flags ────────────────
    # Each check appends to collected_flags / collected_actions instead of
    # returning immediately. A single PrefilterResult is built at the end so
    # that a conversation with e.g. a wrong label AND a duplicate pillar question
    # surfaces both flags rather than only the first one encountered.
    from . import summary_builder as _sb

    collected_flags:   list[str] = []
    collected_actions: list[str] = []

    # Scores are degraded by the worst single violation found; we track the
    # minimum value seen for each dimension and use that for the final result.
    _flag_scores = {
        "compliance_score": 100,
        "sentiment_score": 100,
        "professionalism_score": 100,
        "script_adherence_score": 100,
    }

    def _degrade(new: dict) -> None:
        for k in _flag_scores:
            if new.get(k, 100) < _flag_scores[k]:
                _flag_scores[k] = new[k]

    # ── Check F7: jumped to 6-month window before trying shorter timeline ───────
    if _f7_violated:
        collected_flags.append(_f7_flag_text)
        collected_actions.append("Wrong Message")
        _degrade({"compliance_score": 100, "sentiment_score": 85,
                  "professionalism_score": 95, "script_adherence_score": 80})

    # ── Check 2: agent stated specific dollar offer — F3 ─────────────────────
    for pat in _SUSPICIOUS_PATTERNS:
        if pat.search(all_text):
            collected_flags.append("Stated a specific dollar offer.")
            collected_actions.append("Not Following Lead Flow")
            _degrade({"compliance_score": 80, "sentiment_score": 75,
                      "professionalism_score": 80, "script_adherence_score": 60})
            break

    # ── Check 2b: agent affirmed contact's asking price (F13) ────────────────
    if contact_msgs and agent_msgs:
        for i, m in enumerate(messages):
            if _sender(m) == "contact" and _CONTACT_PRICE_RE.search(_body(m)):
                next_agent = [n for n in messages[i + 1:] if _sender(n) == "agent"]
                if next_agent and _AGENT_PRICE_AFFIRMATION_RE.search(_body(next_agent[0])):
                    collected_flags.append("Affirmed lead's asking price without negotiation.")
                    collected_actions.append("Not Following Lead Flow")
                    _degrade({"compliance_score": 100, "sentiment_score": 85,
                              "professionalism_score": 95, "script_adherence_score": 80})
                break  # only check first price statement

    # ── Check 2b2: bluffer / paranoid contact → wrong label ──────────────────
    _bluff, _bluff_text = _has_bluffer_indicator(messages)
    _is_bluffer_label = False  # default; set below if bluff detected
    if _bluff:
        _label_lower_b = (_actual_label or "").lower()
        # Accept Bluffer, DNC, or Abv MV for bluffer/inflated price scenarios
        _is_accepted_bluffer_label = (
            "bluffer" in _label_lower_b
            or "do not call" in _label_lower_b
            or _label_lower_b == "dnc"
            or "abv mv" in _label_lower_b
            or "above market" in _label_lower_b
        )
        # Only raise the wrong-label flag when the label is NOT in the accepted group.
        if not _is_accepted_bluffer_label:
            collected_flags.append(
                f"Wrong label: assigned '{_actual_label}' but should be 'Bluffer' "
                f"(contact said: '{_bluff_text[:60]}')"
            )
            collected_actions.append("Wrong Label")
            _degrade({"compliance_score": 100, "sentiment_score": 80,
                      "professionalism_score": 95, "script_adherence_score": 100})

    # ── Check 2b3: contact stated inflated price → AbvMV label check ─────────
    _inflated, _price = _contact_stated_inflated_price(messages)
    _is_abv_label = False  # default; set below if inflated detected
    if _inflated:
        _label_lower_a = (_actual_label or "").lower()
        _is_abv_label = (
            "abv mv" in _label_lower_a
            or "above market" in _label_lower_a
            or "bluffer" in _label_lower_a   # Bluffer IS an accepted label for $1M+ deflection
            or "do not call" in _label_lower_a
            or _label_lower_a == "dnc"
        )
        if not _is_abv_label:
            collected_flags.append(
                f"Contact stated inflated price (${_price:,.0f}) — likely Above Market Value, "
                f"label '{_actual_label}' may be wrong"
            )
            _degrade({"compliance_score": 100, "sentiment_score": 85,
                      "professionalism_score": 95, "script_adherence_score": 100})

    # ── Check 2c: agent re-asked same pillar same day after contact answered ──
    _dup_flag, _dup_pillar = _agent_sent_duplicate(messages)
    if _dup_flag:
        collected_flags.append(f"Re-asked {_dup_pillar} same day after contact answered.")
        collected_actions.append("Robotic Conversation")
        _degrade({"compliance_score": 100, "sentiment_score": 75,
                  "professionalism_score": 65, "script_adherence_score": 80})

    # ── Check 2d: agent asked for pillar info contact already gave ────────────
    if contact_msgs and agent_msgs:
        contact_gave_price      = False
        contact_gave_motivation = False
        contact_gave_timeline   = False
        contact_gave_condition  = False
        ignored_pillars: list[str] = []

        for m in messages:
            sender = _sender(m)
            body   = _body(m)
            if sender == "contact":
                if _CONTACT_PRICE_RE.search(body):
                    contact_gave_price = True
                if _CONTACT_MOTIVATION_RE.search(body):
                    contact_gave_motivation = True
                if _CONTACT_TIMELINE_RE.search(body):
                    contact_gave_timeline = True
                if len(body) >= 30 and _CONTACT_CONDITION_RE.search(body):
                    contact_gave_condition = True
                continue
            if sender != "agent":
                continue
            if contact_gave_price and _AGENT_ASKS_PRICE_RE.search(body):
                ignored_pillars.append("asking price")
            if contact_gave_motivation and _AGENT_ASKS_MOTIVATION_RE.search(body):
                ignored_pillars.append("motivation")
            if contact_gave_timeline and _AGENT_ASKS_TIMELINE_RE.search(body):
                ignored_pillars.append("timeline")
            if contact_gave_condition and _AGENT_ASKS_CONDITION_RE.search(body):
                ignored_pillars.append("condition")

        if ignored_pillars:
            unique_pillars = list(dict.fromkeys(ignored_pillars))
            collected_flags.append(
                f"Asked for information contact already provided ({', '.join(unique_pillars)})."
            )
            if "Robotic Conversation" not in collected_actions:
                collected_actions.append("Robotic Conversation")
            if "Not Following Lead Flow" not in collected_actions:
                collected_actions.append("Not Following Lead Flow")
            _degrade({"compliance_score": 100, "sentiment_score": 80,
                      "professionalism_score": 85, "script_adherence_score": 80})

    # ── Emit combined result if any flags collected ───────────────────────────
    if collected_flags:
        _combined_summary = _sb.build_summary(
            messages, agent_name, contact_name, _flag_scores,
            model_used="prefilter_t1_v2",
        )
        _pillars_found = _detect_pillars(contact_msgs)
        # Determine label/AbvMV fields when inflated price was detected
        _label_correct  = None
        _label_should   = None
        _label_reason   = ""
        _label_lower    = (_actual_label or "").lower()
        _assigned_is_bluffer = "bluffer" in _label_lower
        _assigned_is_abv = (
            "abv mv" in _label_lower
            or "above market" in _label_lower
            or _label_lower == "dnc"
            or "do not call" in _label_lower
        )

        # Accept Bluffer, DNC, or Abv MV for both bluffer and inflated price scenarios
        _is_accepted_label = (
            "bluffer" in _label_lower
            or "do not call" in _label_lower
            or _label_lower == "dnc"
            or "abv mv" in _label_lower
            or "above market" in _label_lower
        )

        if (_inflated or _bluff) and _is_accepted_label:
            _label_correct = True
            _label_should  = _actual_label
            _label_reason  = (
                f"Contact used bluffer/inflated price ({_bluff_text[:30]}) — "
                f"'{_actual_label}' is an accepted team label."
            )
        elif _inflated and not _is_accepted_label:
            _label_should  = "Abv MV"
            _label_reason  = f"Contact stated price ${_price:,.0f}, well above typical market"
        elif _bluff and not _is_accepted_label:
            _label_should  = "Bluffer"
            _label_reason  = f"Contact made bluffer statement: '{_bluff_text[:60]}'"

        return PrefilterResult(
            tier_hit=1, decision="short_circuit",
            confidence=0.92,
            notes=f"{len(collected_flags)} flag(s): {'; '.join(collected_flags)[:120]}",
            predicted_scores=_flag_scores,
            result={
                "compliance_score":        _flag_scores["compliance_score"],
                "sentiment_score":         _flag_scores["sentiment_score"],
                "professionalism_score":   _flag_scores["professionalism_score"],
                "script_adherence_score":  _flag_scores["script_adherence_score"],
                "funnel_tier":             funnel_tier,
                "funnel_stage_reached":    "wide",
                "pillars_gathered":        _pillars_found,
                "rebuttals_used":          [],
                "label_assigned":          _actual_label or "Potential",
                "label_correct":           _label_correct,
                "label_should_be":         _label_should,
                "label_reason":            _label_reason,
                "red_flags":               collected_flags,
                "actions_triggered":       list(dict.fromkeys(collected_actions)),
                "summary":                 _combined_summary,
                "model_used":              "prefilter_t1_v2",
                "contact_name":            contact_name,
            },
        )

    # ── Check 3: contact never replied → drip / silent ───────────────────────
    if len(contact_msgs) == 0:
        scores = {
            "compliance_score": 100, "sentiment_score": 80,
            "professionalism_score": 95, "script_adherence_score": 60,
        }
        # A missed call event in the conversation makes "Missed Call" a valid label —
        # contact didn't text back but did pick up / miss the call, so the agent
        # correctly categorized it. Accept both "Missed Call" and "Stopped Responding".
        _has_missed_call = any(
            (_sender(m) or "").lower() == "system" and "missed call" in _body(m).lower()
            for m in messages
        )
        _label_lower = (_actual_label or "").lower()
        _missed_call_label_correct = (
            _has_missed_call
            and ("missed call" in _label_lower or "missed" in _label_lower)
        )
        if _missed_call_label_correct:
            return PrefilterResult(
                tier_hit=1, decision="short_circuit", confidence=0.95,
                notes=f"[{funnel_tier}] contact silent with missed call — label correct",
                predicted_scores=scores,
                result=_clean_result(
                    contact_name,
                    summary="Contact never replied. Missed call event recorded. Agent handled correctly.",
                    scores=scores,
                    funnel_tier=funnel_tier,
                    funnel_stage="none",
                    label_assigned="Missed Call",
                    label_reason="Missed call event present — Missed Call label is correct.",
                    actual_label=_actual_label,
                ),
            )
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.95,
            notes=f"[{funnel_tier}] contact silent — drip/no-reply",
            predicted_scores=scores,
            result=_clean_result(
                contact_name,
                summary="Contact never replied. Agent sent outreach templates only.",
                scores=scores,
                funnel_tier=funnel_tier,
                funnel_stage="none",
                label_assigned="Stopped Responding",
                label_reason="No contact engagement.",
                actual_label=_actual_label,
            ),
        )

    # ── Check 3b: Wrong name guard — agent addressed contact by wrong first name ──
    # If agent used a different name than the contact's first name, there's an F8 red flag.
    # Must go to Groq — never short-circuit a conversation with a wrong name.
    contact_first = (contact_name or "").split()[0].lower() if contact_name else ""
    _ADDR_RE = re.compile(r"\b(?:hi|hello|hey|dear)\s+([a-zA-Z]{2,20})\b", re.I)
    _SKIP_NAMES = {"there", "all", "sir", "friend", "just", "quick", "team", "adam",
                   "emma", "jack", "lisa", "noah", "james", "sarah"}
    if contact_first and len(contact_first) >= 2:
        for m in agent_msgs:
            name_match = _ADDR_RE.search(_body(m))
            if name_match:
                used = name_match.group(1).lower()
                if used != contact_first and used not in _SKIP_NAMES:
                    # Agent addressed by a different name → potential F8 → defer to Groq
                    return None

    # ── Check 4: Clean opt-out (agent stopped correctly) ─────────────────────
    # DNC HAS PRIORITY OVER WRONG NUMBER as per user/owner requirement.
    contact_opted_out = any(p.search(contact_text) for p in _OPT_OUT_PATTERNS)
    if contact_opted_out:
        optout_idx = next(
            (i for i, m in enumerate(messages)
             if _sender(m) == "contact"
             and any(p.search(_body(m)) for p in _OPT_OUT_PATTERNS)),
            None,
        )
        if optout_idx is not None:
            agent_after_optout = [m for m in messages[optout_idx + 1:] if _is_agent(m)]
            if len(agent_after_optout) <= 1:
                scores = {
                    "compliance_score": 100, "sentiment_score": 80,
                    "professionalism_score": 90, "script_adherence_score": 80,
                }
                return PrefilterResult(
                    tier_hit=1, decision="short_circuit", confidence=0.95,
                    notes=f"[{funnel_tier}] opt-out, agent stopped correctly",
                    predicted_scores=scores,
                    result=_clean_result(
                        contact_name,
                        summary="Contact opted out. Texter stopped messaging correctly. Compliance clean.",
                        scores=scores,
                        funnel_tier=funnel_tier,
                        funnel_stage="none",
                        label_assigned="DO Not Call",
                        label_reason="Contact used explicit opt-out language.",
                        actual_label=_actual_label,
                    ),
                )

    # ── Check 5: Wrong Number (explicit or identity mismatch) ─────────────────
    is_wn = any(p.search(contact_text) for p in _WRONG_NUMBER_PATTERNS)
    is_wi = any(p.search(contact_text) for p in _NOT_THIS_PERSON_PATTERNS) and not is_wn

    if is_wn or is_wi:
        # Find the first wrong-number / identity message from contact
        patterns_to_check = _WRONG_NUMBER_PATTERNS if is_wn else _NOT_THIS_PERSON_PATTERNS
        wn_idx = next(
            (i for i, m in enumerate(messages)
             if _sender(m) == "contact"
             and any(p.search(_body(m)) for p in patterns_to_check)),
            len(messages),
        )
        # All agent messages AFTER the wrong-number message
        agent_msgs_after_wn = [m for m in messages[wn_idx + 1:] if _is_agent(m)]
        agent_text_after_wn = " ".join(_body(m) for m in agent_msgs_after_wn)
        
        # Guard: Check for continued pitch using the helper
        from ._guards import agent_continued_pitch_after_wn
        continued_pitch = agent_continued_pitch_after_wn(messages)
        
        # If agent sent 3+ messages after WN that's suspicious regardless of content
        if continued_pitch or len(agent_msgs_after_wn) >= 3:
            from . import summary_builder
            _e4_scores = {
                "compliance_score": 60,
                "sentiment_score": 70,
                "professionalism_score": 75,
                "script_adherence_score": 60,
            }
            _e4_summary = summary_builder.build_summary(
                messages, agent_name, contact_name, _e4_scores,
                model_used="prefilter_t1_v2",
            )
            return PrefilterResult(
                tier_hit=1, decision="short_circuit", confidence=1.0,
                notes=f"F5: [{funnel_tier}] wrong number — agent continued pitch ({len(agent_msgs_after_wn)} msgs after WN)",
                predicted_scores=_e4_scores,
                result={
                    "compliance_score": _e4_scores["compliance_score"],
                    "sentiment_score": _e4_scores["sentiment_score"],
                    "professionalism_score": _e4_scores["professionalism_score"],
                    "script_adherence_score": _e4_scores["script_adherence_score"],
                    "funnel_tier": funnel_tier,
                    "funnel_stage_reached": "none",
                    "pillars_gathered": [],
                    "rebuttals_used": [],
                    "label_assigned": _actual_label or "Wrong Number",
                    "label_correct": None,
                    "label_should_be": None,
                    "label_reason": "",
                    "red_flags": [
                        "Continued original pitch after wrong number."
                    ],
                    "actions_triggered": ["Not Following Lead Flow"],
                    "summary": _e4_summary,
                    "model_used": "prefilter_t1_v2",
                    "contact_name": contact_name,
                },
            )
        scores = {
            "compliance_score": 100, "sentiment_score": 85,
            "professionalism_score": 95, "script_adherence_score": 100,
        }
        
        # Guard: If contact also opted out, DNC is a valid label.
        _label_lower = (_actual_label or "").lower()
        if contact_opted_out and ("dnc" in _label_lower or "do not call" in _label_lower):
            expected_label = _actual_label
            reason = "Contact stated wrong number AND explicit opt-out (DNC is correct)."
        else:
            expected_label = "Wrong Number"
            reason = "Contact explicitly stated wrong number."

        # Fix summary hallucination
        if len(agent_msgs_after_wn) > 0:
            summary = "Wrong number. Texter apologized and pivoted to referral close."
        else:
            summary = "Contact indicated wrong number. Texter stopped messaging."

        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.92,
            notes=f"[{funnel_tier}] wrong number — clean pivot",
            predicted_scores=scores,
            result=_clean_result(
                contact_name,
                summary=summary,
                scores=scores,
                funnel_tier=funnel_tier,
                funnel_stage="none",
                label_assigned=expected_label,
                label_reason=reason,
                actual_label=_actual_label,
            ),
        )

    # ── Check 6: Sold property ────────────────────────────────────────────────
    is_sold = any(p.search(contact_text) for p in _SOLD_PATTERNS)
    if is_sold and len(messages) <= 12:
        scores = {
            "compliance_score": 100, "sentiment_score": 85,
            "professionalism_score": 95, "script_adherence_score": 100,
        }
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.90,
            notes=f"[{funnel_tier}] sold — contact confirmed property sold",
            predicted_scores=scores,
            result=_clean_result(
                contact_name,
                summary="Contact confirmed property is sold. Texter handled appropriately.",
                scores=scores,
                funnel_tier=funnel_tier,
                funnel_stage="none",
                label_assigned="Sold",
                label_reason="Contact confirmed property is sold.",
                actual_label=_actual_label,
            ),
        )

    # ── Check 7: AbvMV guard ——————————————————————————————————————————————————
    # If the contact stated a very high price, always send to Groq for AbvMV scoring.
    contact_has_high_price = bool(_ABV_MV_RE.search(contact_text))
    if contact_has_high_price:
        return None  # defer — Groq will score AbvMV correctly

    # ── Check 8: Not Interested (soft refusal) ────────────────────────────────
    # NI short-circuit fires ONLY when:
    #   - agent replied ≥1 and ≤2 times after refusal (0 replies = "gave up" red flag)
    #   - no strong NI canceller in contact text
    #   - contact did NOT raise hand earlier (would be a lead)
    #   - normal rebuttal count after refusal; repeated refusal is still safe
    #     when the agent used only 1-2 follow-up rebuttals
    is_ni = (
        any(p.search(contact_text) for p in _NOT_INTERESTED_PATTERNS)
        and not _NOT_INTERESTED_CANCEL.search(contact_text)
    )
    contact_raised_hand = bool(_RAISED_HAND_RE.search(contact_text))

    # Count NI messages upfront — used by both NI and maybe-later checks.
    ni_msg_count = sum(
        1 for m in contact_msgs
        if any(p.search(_body(m)) for p in _NOT_INTERESTED_PATTERNS)
    ) if is_ni else 0

    if is_ni and not contact_raised_hand:
        first_ni_idx = next(
            (i for i, m in enumerate(messages)
             if _sender(m) == "contact"
             and any(p.search(_body(m)) for p in _NOT_INTERESTED_PATTERNS)),
            None,
        )
        if first_ni_idx is not None:
            agent_after_ni = [m for m in messages[first_ni_idx + 1:] if _is_agent(m)]
            # Check if contact sent a price-inquiry AFTER the initial NI
            # e.g. "I don't know how. Much do you want to pay?" — contact is interested.
            contact_after_ni = [m for m in messages[first_ni_idx + 1:] if _sender(m) == "contact"]
            contact_price_flipped = any(
                _POST_NI_PRICE_FLIP_RE.search(_body(m)) for m in contact_after_ni
            )
            if contact_price_flipped:
                # Contact reversed: now asking about the price → Potential, not NI
                # Defer to T4 which will correctly classify as Potential.
                return None

            # 0 replies = "gave up" red flag → Groq
            # 1-2 replies = standard rebuttal + close → safe SC
            # >2 replies = too persistent → Groq
            if 1 <= len(agent_after_ni) <= 2:
                scores = {
                    "compliance_score": 100, "sentiment_score": 80,
                    "professionalism_score": 90, "script_adherence_score": 100,
                }
                return PrefilterResult(
                    tier_hit=1, decision="short_circuit", confidence=0.88,
                    notes=f"[{funnel_tier}] not interested — {len(agent_after_ni)} agent msg(s) after refusal",
                    predicted_scores=scores,
                    result=_clean_result(
                        contact_name,
                        summary=f"{contact_name} declined the offer. Texter sent a professional rebuttal and closed the conversation cleanly.",
                        scores=scores,
                        funnel_tier=funnel_tier,
                        funnel_stage="wide",
                        label_assigned="Not Interested",
                        label_reason="Contact explicitly declined; texter followed up once and closed.",
                        actual_label=_actual_label,
                    ),
                )

    # ── Check 9: Maybe Later ──────────────────────────────────────────────────
    # Do not fire if contact also expressed NI (is_ni guard) or multiple refusals.
    is_maybe = any(p.search(contact_text) for p in _MAYBE_LATER_PATTERNS)
    if is_maybe and not is_ni and ni_msg_count < 2 and not contact_has_high_price and not contact_raised_hand:
        scores = {
            "compliance_score": 100, "sentiment_score": 85,
            "professionalism_score": 95, "script_adherence_score": 100,
        }
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.85,
            notes=f"[{funnel_tier}] maybe later",
            predicted_scores=scores,
            result=_clean_result(
                contact_name,
                summary="Contact indicated possible future interest. Texter closed cleanly.",
                scores=scores,
                funnel_tier=funnel_tier,
                funnel_stage="wide",
                label_assigned="Maybe Later",
                label_reason="Contact indicated possible future interest.",
                actual_label=_actual_label,
            ),
        )

    # NOTE: WF raised-hand leads are NOT short-circuited here.
    # Engaged leads (contact showed interest) must go to Groq so that rebuttal
    # quality, pillar coverage, and script adherence can be scored properly.
    # T1 only short-circuits trivially clean (NI/WN/Drip/Sold) conversations.

    # ── Check 10: MF/NF — check pillar coverage ───────────────────────────────
    # For Middle/Narrow funnel, only short-circuit if the conversation is clean
    # AND the contact did NOT reach the pillar threshold (meaning it's NOT a lead
    # that Groq needs to score for quality).  If pillars ≥ threshold → defer to Groq.
    if funnel_tier in ("MF", "NF"):
        pillars = _detect_pillars(contact_msgs)
        if len(pillars) >= pillar_threshold:
            # Potential lead — Groq must evaluate quality, rebuttal, and flags
            return None

    # Nothing definitive → pass to Tier 2
    return None
