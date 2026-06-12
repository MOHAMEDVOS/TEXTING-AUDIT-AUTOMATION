"""
Smart summary builder for ML prefilter short-circuited conversations.

Extracts real facts from the conversation (message count, contact tone,
agent behavior, topic signals) and builds a descriptive summary that
reads like Groq output — not a mechanical "Tier X said clean" message.

Used by all three tiers when short-circuiting.

Message-type classifier (Phase 1 — SMS Script training):
  Distinguishes rebuttals / follow-ups / pillar questions so the ML
  never conflates them the way the old `_count_rebuttals` helper did.
"""
from __future__ import annotations

import re
from typing import Optional

from ai.prefilter.pillar_detection import detect_gathered_pillars


# ── Tone / intent detection patterns ─────────────────────────────────────────

_HOSTILE_PATTERNS = [
    re.compile(r"\b(fuck|shit|damn|hell|stfu|wtf|idiot|stupid|suck|scam|spam|harass)\b", re.I),
    re.compile(r"\b(stop\s+(texting|calling|contacting)|leave\s+me\s+alone|do\s+not\s+contact)\b", re.I),
    re.compile(r"\bstop[.!]*(?:\s|$)", re.I),  # Catch "if you could STOP." or "STOP thanks"
    re.compile(r"[\U0001F621\U0001F620\U0001F92C\U0001F595]"),  # angry/rude emoji
]

_NOT_INTERESTED_PATTERNS = [
    re.compile(r"\b(not\s+interested|no\s+thanks?|nah|nope|pass)\b", re.I),
    re.compile(r"\b(don'?t\s+want|not\s+selling|not\s+for\s+sale)\b", re.I),
    re.compile(r"\b(never|absolutely\s+not)\b", re.I),
    # Bare "no" as a standalone reply (whole message or very short)
    re.compile(r"^no[.!]?\s*$", re.I | re.MULTILINE),
]

_MAYBE_PATTERNS = [
    re.compile(r"\b(maybe|possibly|might|could\s+be|thinking\s+about)\b", re.I),
    re.compile(r"\b(not\s+sure|let\s+me\s+think|down\s+the\s+road)\b", re.I),
]

_SOLD_PATTERNS = [
    # Bare "Sold" as a standalone reply (whole message or very short) — most common case
    re.compile(r"^\s*sold[.!]?\s*$", re.I | re.MULTILINE),
    # Compound sold phrases
    re.compile(r"\b(already\s+sold|just\s+sold|under\s+contract|sold\s+it)\b", re.I),
    re.compile(r"\b(closing\s+soon|in\s+escrow|have\s+an?\s+agent)\b", re.I),
    # "it's sold" / "it was sold" / "property is sold"
    re.compile(r"\b(it'?s?\s+sold|was\s+sold|is\s+sold|property\s+sold)\b", re.I),
    re.compile(r"\bno\s+longer\s+(available|for\s+sale|on\s+the\s+market)\b", re.I),
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
    # Potential reversal: contact asks agent for their price/offer after saying No
    re.compile(r"\bhow\s*[?.!,]*\s*much\s+(do|would|will|can)\s+you\s+(want|pay|offer|give)\b", re.I),
    re.compile(r"\bwhat\s+(would|do|will|can)\s+you\s+(pay|offer|give)\b", re.I),
    re.compile(r"\bwhat'?s?\s+(your|the)\s+offer\b", re.I),
    re.compile(r"\bwhat\s+are\s+you\s+(willing|able)\s+to\s+pay\b", re.I),
    re.compile(r"\bmake\s+(me\s+)?an?\s+offer\b", re.I),
    re.compile(r"\bwhat\s+are\s+you\s+offering\b", re.I),
    re.compile(r"\bhow\s*[?.!,]*\s*much.{0,30}\bwant\s+to\s+pay\b", re.I),
    re.compile(r"\bwhat.{0,20}\b(offer|buying\s+for|purchase\s+price)\b", re.I),
    re.compile(r"\bmuch\s+(do|would|will|can)\s+you\s+(want|pay|offer|give)\b", re.I),
]


# ── Condescension / mockery — dismissive hostility without explicit opt-out ──
# "Do you ask dumb things on purpose?" — contact is mocking the agent. Not a
# regex opt-out, but the team accepts DO Not Call for dismissive/mocking
# contacts, so label guards must never force "Not Interested" over it.
_CONDESCENSION_RE = re.compile(
    r"\b(dumb|stupid|silly|ridiculous|pointless|idiotic)\s+(question|things?|stuff|texts?|messages?)\b"
    r"|\bdo\s+you\s+(ask|say|send)\s+(dumb|stupid|silly)\b"
    r"|\bare\s+you\s+(a\s+bot|a\s+robot|stupid|dumb|illiterate)\b"
    r"|\bis\s+this\s+a\s+(bot|joke|scam)\b"
    r"|\b(can|do)\s+you\s+(even\s+|not\s+)?read\b"
    r"|\bwhat\s+part\s+of\s+no\b"
    r"|\bmakes?\s+no\s+sense\b"
    r"|\bwast(e|ing)\s+(of\s+)?(my\s+)?time\b",
    re.I,
)

# ── Buyer-side price rejection — contact says the agent's number is too LOW ──
# "I'd buy more at that price" / "way too low" — the contact is rejecting the
# agent's range as an investor would, NOT declining to sell. This is the
# signature of an Abv MV conversation, never a plain Not Interested.
_BUYER_SIDE_REJECTION_RE = re.compile(
    r"\bi(?:'?d|\s+would)?\s+buy\s+(?:more\s+|one\s+|another\s+)?(?:at|for)\s+(?:that|this)\s+price\b"
    r"|\b(?:that'?s|way|much|far)\s+too\s+low\b"
    r"|\bnot\s+(?:nearly\s+|even\s+)?enough\b"
    r"|\bi\s+paid\s+more\s+than\s+that\b"
    r"|\b(?:it'?s|house\s+is|property\s+is)\s+worth\s+(?:way\s+|a\s+lot\s+|much\s+)?more\b",
    re.I,
)

# Permission-style opener: "May I ask you something?" — invites a free "No"
# that gets misread as disinterest. Used for coaching feedback.
_PERMISSION_OPENER_RE = re.compile(
    r"\b(may|can|could)\s+i\s+ask\s+you\s+(something|a\s+question|a\s+quick\s+question)\b",
    re.I,
)


def _classify_contact_tone(contact_msgs: list[dict]) -> str:
    """Classify the overall contact tone from their messages."""
    if not contact_msgs:
        return "silent"

    all_text = " ".join(m.get("body") or m.get("message", "") for m in contact_msgs)
    # Per-message texts for patterns that need per-message matching (e.g. bare "No")
    msg_texts = [m.get("body") or m.get("message", "") for m in contact_msgs]

    # Hostility check: run on all_text AND last message — last msg wins over earlier positives
    last_text = msg_texts[-1] if msg_texts else ""
    if any(p.search(all_text) for p in _HOSTILE_PATTERNS) or any(p.search(last_text) for p in _HOSTILE_PATTERNS):
        return "hostile"
    if any(p.search(all_text) for p in _WRONG_NUMBER_PATTERNS):
        return "wrong_number"
    if any(p.search(all_text) for p in _SOLD_PATTERNS):
        return "already_sold"

    # Reversal check: if any later message is a positive/price-inquiry signal,
    # the contact recovered from an initial No → classify as potential,
    # even if an earlier message matched not_interested.
    later_texts = msg_texts[1:] if len(msg_texts) > 1 else msg_texts
    later_joined = " ".join(later_texts)
    if any(p.search(later_joined) for p in _POSITIVE_PATTERNS):
        return "potential"

    # Not-interested: check all_text AND each individual message (catches bare "No")
    if any(p.search(all_text) for p in _NOT_INTERESTED_PATTERNS) or \
       any(p.search(t) for t in msg_texts for p in _NOT_INTERESTED_PATTERNS):
        return "not_interested"
    if any(p.search(all_text) for p in _POSITIVE_PATTERNS):
        return "interested"
    if any(p.search(all_text) for p in _MAYBE_PATTERNS):
        return "maybe"

    # Check if all messages are very short (emoji, one word, etc.)
    if all(len((m.get("body") or m.get("message") or "").strip()) < 10 for m in contact_msgs):
        if any(_EMOJI_ONLY.match((m.get("body") or m.get("message") or "").strip()) for m in contact_msgs):
            return "emoji_only"
        return "brief"

    return "neutral"


def _describe_agent_opening(agent_msgs: list[dict]) -> str:
    """Describe how the agent opened the conversation."""
    if not agent_msgs:
        return ""
    first = (agent_msgs[0].get("body") or agent_msgs[0].get("message") or "").strip()
    if len(first) < 20:
        return "sent a brief initial message"
    if any(w in first.lower() for w in ["hi ", "hey ", "hello", "good morning", "good afternoon"]):
        return "sent a warm initial message"
    if any(w in first.lower() for w in ["follow", "checking", "reaching back"]):
        return "sent a follow-up message"
    return "sent an initial outreach message"


# ── SMS Script message-type patterns ─────────────────────────────────────────
# Source: SMS script.txt — three distinct agent message types:
#   1. REBUTTAL  — direct response to contact saying "No"
#   2. FOLLOW-UP — scheduled check-in when contact stopped replying
#   3. PILLAR Q  — qualifying question (condition / price / timeline / motivation)

# Rebuttal anchors — phrases the script prescribes after each "No"
_REBUTTAL_PATTERNS = [
    re.compile(r"\bno\s+worries\b.{0,60}\b(sell|selling|sale)\b", re.I),
    re.compile(r"\bnot\s+a\s+problem\b.{0,80}\b(flexible|timeline|process|whenever)\b", re.I),
    re.compile(r"\bwhenever\s+you\s+are\s+ready\b", re.I),
    re.compile(r"\bdo\s+you\s+think\s+it\s+could\s+be\s+for\s+sale.{0,40}\b6\s+months\b", re.I),
    re.compile(r"\bunderstood\b.{0,80}\b(referral|know\s+someone|\$1.?000)\b", re.I),
    # Generic rebuttal openers the script uses
    re.compile(r"\b(we('re)?\s+very\s+flexible|start\s+the\s+process\s+whenever)\b", re.I),
    # 2nd-rebuttal variant: flexible + 6-month reference even without "sell"
    re.compile(r"\bvery\s+flexible\b.{0,120}\b(6\s+months?|six\s+months?)\b", re.I),
    re.compile(r"\b(6\s+months?|six\s+months?)\b.{0,60}\bflexible\b", re.I),
    # Fallback: polite continuation phrase after a "No" with no FU/pillar match
    re.compile(r"\bno\s+worries\s+at\s+all\b", re.I),
    re.compile(r"\btotally\s+understandable\b", re.I),
]

# Follow-up track anchors — WL / AP / HL / Reason FU
_FOLLOW_UP_PATTERNS = [
    # WL (Waiting List — after condition)
    re.compile(r"\bjust\s+checking\s+in\b.{0,60}\b(updates?|everything\s+still\s+look)\b", re.I),
    re.compile(r"\bstill\s+want\s+to\s+sell\b", re.I),
    re.compile(r"\bno\s+longer\s+thinking\s+about\s+selling\b", re.I),
    # AP (After Price)
    re.compile(r"\bprice\s+in\s+mind\b.{0,60}\bwant\s+for\b", re.I),
    re.compile(r"\bi\s+can\s+get\s+you\s+a\s+price\b", re.I),
    re.compile(r"\bstill\s+thinking\s+to\s+possibly\s+sell\b", re.I),
    # HL (Hold — after timeline)
    re.compile(r"\bsell\s+soon\s+or\s+down\s+the\s+road\b", re.I),
    re.compile(r"\bget\s+a\s+price\s+over\s+to\s+you\b", re.I),
    re.compile(r"\bdon'?t\s+want\s+to\s+be\s+a\s+bother\b", re.I),
    # Reason FU (after motivation)
    re.compile(r"\bit'?s\s+okay\s+if.{0,40}\bstill\s+want\s+to\s+sell\b", re.I),
    re.compile(r"\bchecking\s+in\s+one\s+last\s+time\b", re.I),
    re.compile(r"\bif\s+you'?re\s+still\s+thinking\s+about\s+the\s+sale\b", re.I),
    # Generic FU signals
    re.compile(r"\btouching\s+base\b", re.I),
    re.compile(r"\breaching\s+back\b", re.I),
    re.compile(r"\bjust\s+checking\s+(in|on)\b", re.I),
]

# Pillar question anchors (already detected in tier1_phrases_v2, mirrored here)
_PILLAR_Q_PATTERNS = [
    # Condition
    re.compile(r"\bdone\s+any\s+(repairs?|updates?|upgrades?|renovation)\b", re.I),
    re.compile(r"\b(beds?|baths?|bedroom|bathroom)\b.{0,40}\b(correct|right|confirm)\b", re.I),
    re.compile(r"\brepairs?\s+in\s+the\s+last\s+\d+\s+(to\s+\d+\s+)?years?\b", re.I),
    # Price
    re.compile(r"\bwhere.{0,30}\bneed\s+to\s+be\s+on\s+price\b", re.I),
    re.compile(r"\bsell\s+your\s+home\s+as.?is.{0,60}\bcovere?d\s+all.{0,30}\bclosing\s+costs\b", re.I),
    # Motivation
    re.compile(r"\breason\s+for\s+selling\b", re.I),
    re.compile(r"\bcould\s+you\s+share.{0,30}\breason\b", re.I),
    re.compile(r"\bwhat\s+is\s+it\s+that'?s\s+making\s+you\s+consider\b", re.I),
    # Timeline
    re.compile(r"\bwhat\s+kind\s+of\s+timeline.{0,30}\bclosing\b", re.I),
    re.compile(r"\bdo\s+you\s+think\s+we\s+can\s+do\s+the\s+closing\s+within\b", re.I),
]

# Contact "No" signals — used to decide if the NEXT agent message is a rebuttal
_CONTACT_NO_PATTERNS = [
    re.compile(r"^\s*no[.!?]?\s*$", re.I | re.MULTILINE),
    re.compile(r"\bnot\s+interested\b", re.I),
    re.compile(r"\bnot\s+(selling|for\s+sale|now|ready)\b", re.I),
    re.compile(r"\bnever\b", re.I),
    re.compile(r"\bnope\b", re.I),
    re.compile(r"\bno\s+thank(s|\s+you)?\b", re.I),
    re.compile(r"\babsolutely\s+not\b", re.I),
    re.compile(r"\bdon'?t\s+want\s+to\s+sell\b", re.I),
]

def _is_contact_no(body: str) -> bool:
    """True if this contact message is a refusal/No."""
    return any(p.search(body) for p in _CONTACT_NO_PATTERNS)


# ── Phase 4: Above Market Value — agent response detection ───────────────────
# Source: SMS script.txt § Evaluate asking price:
#   "If above market: 'I appreciate the reply, but that is going to be more
#    than I can pay. If by chance you know anyone looking to sell, I pay $1,000
#    on all referrals I close on!' (End conversation)"
#
# Two detections:
#   1. Agent sent the referral-close → correct script behaviour
#   2. Agent kept pushing after high price → script violation (FLAG 15)

_AGENT_REFERRAL_CLOSE_RE = re.compile(
    r"\b(more\s+than\s+i\s+can\s+pay"
    r"|that.{0,30}going\s+to\s+be\s+more\s+than"
    r"|appreciate\s+the\s+reply.{0,60}more\s+than"
    r"|out\s+of\s+(my|our)\s+(price\s+)?range"
    r"|too\s+(high|much)\s+for\s+(me|us)"
    r"|can'?t\s+go\s+that\s+high"
    r"|unfortunately.{0,40}(too\s+high|out\s+of.{0,15}range|more\s+than)"
    r"|outside\s+(my|our)\s+budget"
    r"|\$\s*1[,.]?000\s+(for\s+)?(referral|anyone|any\s+home)"
    r"|pay\s+\$?\s*1[,.]?000.{0,30}referral"
    r"|referral.{0,30}\$?\s*1[,.]?000)\b",
    re.I,
)

# Agent keeps pushing the deal after the contact stated a high price
# (any selling/closing language AFTER the price message)
_AGENT_KEPT_PUSHING_RE = re.compile(
    r"\b(what\s+kind\s+of\s+timeline"
    r"|when.{0,20}closing"
    r"|reason\s+for\s+selling"
    r"|we\s+can\s+still\s+work"
    r"|let\s+me\s+see\s+what\s+i\s+can\s+do"
    r"|i'?ll\s+get\s+back\s+to\s+you"
    r"|i\s+can\s+get\s+you\s+a\s+price"
    r"|push\s+the\s+lead"
    r"|we'?re\s+very\s+flexible)\b",
    re.I,
)

# Contact price patterns — any dollar amount the contact states
_CONTACT_PRICE_RE = re.compile(
    r"(?:\$\s*)(\d[\d,]*)\s*(k|thousand|million|mil|m)?\b",
    re.I,
)
_CONTACT_PRICE_WORD_RE = re.compile(
    r"\b((a|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
    r"(million|mil)\b",
    re.I,
)
# Bare suffixed amounts with no dollar sign — "500k", "500 thousand", "1.2 million".
# A unit suffix is REQUIRED here (bare "500" is too ambiguous to be a price).
_CONTACT_PRICE_BARE_RE = re.compile(
    r"\b(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|grand|million|mil)\b",
    re.I,
)
_WORD_NUMS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _parse_contact_price(body: str) -> float | None:
    """Extract dollar amount from a contact message. Returns None if no price found."""
    body_lower = body.lower()

    # Word form: "a million", "two million"
    wm = _CONTACT_PRICE_WORD_RE.search(body_lower)
    if wm:
        mult_word = (wm.group(2) or "one").lower()
        return _WORD_NUMS.get(mult_word, 1) * 1_000_000

    # Numeric form: "$500k", "$1,200,000", "$2M"
    for m in _CONTACT_PRICE_RE.finditer(body):
        num_str = m.group(1).replace(",", "")
        unit = (m.group(2) or "").lower()
        try:
            num = float(num_str)
        except ValueError:
            continue
        if unit in ("k", "thousand"):
            return num * 1_000
        elif unit in ("million", "mil", "m"):
            return num * 1_000_000
        else:
            return num

    # No-$ suffixed form: "close to 500k", "around 500 thousand"
    bm = _CONTACT_PRICE_BARE_RE.search(body_lower)
    if bm:
        try:
            num = float(bm.group(1).replace(",", ""))
        except ValueError:
            return None
        unit = bm.group(2).lower()
        value = num * (1_000_000 if unit in ("million", "mil") else 1_000)
        # Sanity floor — a real asking price, not "5k run" / "10k followers"
        if value >= 10_000:
            return value
    return None


def detect_abv_mv_response(messages: list[dict]) -> dict:
    """
    Detect above-market price scenarios and validate agent response.

    Returns:
      {
        'contact_stated_price': bool,
        'price_amount': float | None,
        'agent_did_referral_close': bool,
        'agent_kept_pushing': bool,
        'price_msg_index': int | None,
      }

    Source: SMS script.txt § Evaluate asking price.
    """
    result = {
        "contact_stated_price": False,
        "price_amount": None,
        "agent_did_referral_close": False,
        "agent_kept_pushing": False,
        "price_msg_index": None,
    }

    # Step 1: Find the contact's price statement
    price_idx: int | None = None
    for i, m in enumerate(messages):
        sender = (m.get("sender") or "").lower()
        body = (m.get("body") or m.get("message") or "").strip()
        if sender != "contact" or not body:
            continue
        price = _parse_contact_price(body)
        if price is not None:
            result["contact_stated_price"] = True
            result["price_amount"] = price
            result["price_msg_index"] = i
            price_idx = i
            break  # Use the FIRST price statement

    if price_idx is None:
        return result

    # Step 2: Check agent messages AFTER the price statement
    for m in messages[price_idx + 1:]:
        sender = (m.get("sender") or "").lower()
        body = (m.get("body") or m.get("message") or "").strip()
        if sender in ("contact", "system", "") or not body:
            continue

        # Did agent do the referral close?
        if _AGENT_REFERRAL_CLOSE_RE.search(body):
            result["agent_did_referral_close"] = True

        # Did agent keep pushing the deal instead?
        if _AGENT_KEPT_PUSHING_RE.search(body):
            result["agent_kept_pushing"] = True

    return result


# ── Phase 2: Follow-up track detection ───────────────────────────────────────
# Source: SMS script.txt — 4 FU tracks, each triggered by the last pillar asked.
#
#   WL (Waiting List)  — agent last asked about CONDITION  → "any updates?"
#   AP (After Price)   — agent last asked about PRICE      → "had a price in mind?"
#   HL (Hold)          — agent last asked about TIMELINE   → "sell soon or down the road?"
#   Reason             — agent last asked about MOTIVATION → "reason for selling?"
#
# The track determines which FU messages (FU1/FU2/FU3) are correct.

# Per-pillar agent-question anchors (used to detect last pillar asked)
_PILLAR_CONDITION_RE = re.compile(
    r"\b(done\s+any\s+(repairs?|updates?|upgrades?|renovation)"
    r"|repairs?\s+in\s+the\s+last\s+\d+"
    r"|beds?\s*[/,]?\s*baths?.{0,40}\b(correct|right|confirm)"
    r"|confirm.{0,40}\b(beds?|baths?|bedroom|bathroom)"
    r"|what.{0,30}condition\b"
    r"|needs?\s+(work|repair|updating))\b",
    re.I,
)
_PILLAR_PRICE_RE = re.compile(
    r"\b(where.{0,30}need\s+to\s+be\s+on\s+price"
    r"|sell.{0,40}as.?is.{0,60}closing\s+costs"
    r"|how\s+much.{0,30}(want|asking|expect)"
    r"|what.{0,30}(price|asking\s+price|asking\s+for)"
    r"|had\s+a\s+price\s+in\s+mind)\b",
    re.I,
)
_PILLAR_TIMELINE_RE = re.compile(
    r"\b(what\s+kind\s+of\s+timeline"
    r"|when.{0,30}(looking\s+to\s+sell|close|move)"
    r"|sell\s+soon\s+or\s+down\s+the\s+road"
    r"|closing\s+within\b"
    r"|how\s+soon)\b",
    re.I,
)
_PILLAR_MOTIVATION_RE = re.compile(
    r"\b(reason\s+for\s+selling"
    r"|could\s+you\s+share.{0,30}reason"
    r"|what\s+is\s+it\s+that.{0,20}making\s+you\s+consider"
    r"|why.{0,30}(selling|looking\s+to\s+sell)"
    r"|mind\s+(if\s+i\s+ask|me\s+asking).{0,40}reason)\b",
    re.I,
)

# Map pillar → FU track name (matches label_validator._AI_REQUIRED_LABELS)
_PILLAR_TO_TRACK = {
    "condition": "wl drip",
    "price":     "ap drip",
    "timeline":  "hl drip",
    "motivation":"reason fu",
}

# FU message patterns per track (FU1/FU2/FU3 check phrases from SMS script)
_FU_TRACK_PATTERNS: dict[str, list[re.Pattern]] = {
    "wl drip": [
        re.compile(r"\bjust\s+checking\s+in\b.{0,80}\b(updates?|everything\s+still\s+look)\b", re.I),
        re.compile(r"\bstill\s+want\s+to\s+sell\b", re.I),
        re.compile(r"\bno\s+longer\s+thinking\s+about\s+selling\b", re.I),
    ],
    "ap drip": [
        re.compile(r"\bprice\s+in\s+mind\b", re.I),
        re.compile(r"\bi\s+can\s+get\s+you\s+a\s+price\b", re.I),
        re.compile(r"\bstill\s+thinking\s+to\s+possibly\s+sell\b", re.I),
    ],
    "hl drip": [
        re.compile(r"\bsell\s+soon\s+or\s+down\s+the\s+road\b", re.I),
        re.compile(r"\bget\s+a\s+price\s+over\s+to\s+you\b", re.I),
        re.compile(r"\bdon'?t\s+want\s+to\s+be\s+a\s+bother\b", re.I),
    ],
    "reason fu": [
        re.compile(r"\bit'?s\s+okay\s+if.{0,60}\bstill\s+want\s+to\s+sell\b", re.I),
        re.compile(r"\bchecking\s+in\s+one\s+last\s+time\b", re.I),
        re.compile(r"\bif\s+you'?re\s+still\s+thinking\s+about\s+the\s+sale\b", re.I),
    ],
}


def detect_fu_track(messages: list[dict]) -> str | None:
    """
    Identify which follow-up track the conversation is on.

    Walks messages in order; the LAST pillar question asked by the agent
    before contact stopped replying determines the track:
      'wl drip'   — last pillar was condition
      'ap drip'   — last pillar was price
      'hl drip'   — last pillar was timeline
      'reason fu' — last pillar was motivation

    Returns None when:
      - No pillar was asked (no follow-up warranted yet)
      - Contact is still actively replying (not in FU stage)

    Source: SMS script.txt § Follow-up sequences.
    """
    last_pillar: str | None = None
    last_agent_pillar_idx: int = -1
    contact_replied_after_last_pillar = False

    for i, m in enumerate(messages):
        sender = (m.get("sender") or "").lower()
        body   = (m.get("body") or m.get("message") or "").strip()
        if not body:
            continue

        if sender == "contact":
            if last_agent_pillar_idx >= 0:
                # Contact said something after the last pillar question
                contact_replied_after_last_pillar = True
            continue

        if sender in ("system", ""):
            continue

        # Agent message — check which pillar it asks about
        if _PILLAR_CONDITION_RE.search(body):
            last_pillar = "condition"
            last_agent_pillar_idx = i
            contact_replied_after_last_pillar = False
        elif _PILLAR_PRICE_RE.search(body):
            last_pillar = "price"
            last_agent_pillar_idx = i
            contact_replied_after_last_pillar = False
        elif _PILLAR_TIMELINE_RE.search(body):
            last_pillar = "timeline"
            last_agent_pillar_idx = i
            contact_replied_after_last_pillar = False
        elif _PILLAR_MOTIVATION_RE.search(body):
            last_pillar = "motivation"
            last_agent_pillar_idx = i
            contact_replied_after_last_pillar = False

    # No pillar ever asked → not in FU territory
    if last_pillar is None:
        return None

    # Contact replied after the last pillar → conversation still active, not FU stage
    if contact_replied_after_last_pillar:
        return None

    return _PILLAR_TO_TRACK.get(last_pillar)


def validate_fu_label(messages: list[dict], assigned_label: str) -> dict:
    """
    Validate FU-category labels (WL Drip / AP Drip / HL Drip / Reason FU).

    Returns:
      {
        'label_correct': bool | None,
        'label_should_be': str | None,
        'label_reason': str,
        'fu_track': str | None,       # detected track
        'fu_messages_match': bool,    # True if agent sent correct FU messages
      }

    If the track cannot be determined, returns label_correct=None (defer to Groq).
    """
    norm_label = re.sub(r"\s+", " ", (assigned_label or "").strip()).lower()
    track = detect_fu_track(messages)

    if track is None:
        return {
            "label_correct": None,
            "label_should_be": None,
            "label_reason": "Could not determine FU track from conversation.",
            "fu_track": None,
            "fu_messages_match": False,
        }

    # Check that the assigned label matches the detected track
    label_correct = norm_label == track

    # Check that the agent actually sent the right FU messages for this track
    agent_bodies = [
        (m.get("body") or m.get("message") or "")
        for m in messages
        if (m.get("sender") or "").lower() not in ("contact", "system", "")
    ]
    combined_agent = " ".join(agent_bodies)
    track_patterns = _FU_TRACK_PATTERNS.get(track, [])
    fu_messages_match = any(p.search(combined_agent) for p in track_patterns)

    if label_correct:
        reason = (
            f"ML detected '{track}' track (last pillar asked → {track}). "
            f"Agent FU messages {'match' if fu_messages_match else 'do NOT match'} expected track."
        )
    else:
        reason = (
            f"ML detected '{track}' track but label is '{norm_label}'. "
            f"Expected label: '{track}'."
        )

    return {
        "label_correct": label_correct,
        "label_should_be": track,
        "label_reason": reason,
        "fu_track": track,
        "fu_messages_match": fu_messages_match,
    }



def classify_agent_message(body: str, prev_contact_said_no: bool) -> str:
    """
    Classify a single agent message as one of:
      'opener'         — first outreach to the contact
      'rebuttal'       — direct response to a contact 'No'
      'follow_up'      — scheduled check-in (WL/AP/HL/Reason FU track)
      'pillar_question'— qualifying question (condition/price/timeline/motivation)
      'other'          — acknowledgement, call scheduling, wrap-up, etc.

    Source: SMS script.txt — the three message types are distinct in the script.
    """
    body_l = body.lower()

    # Pillar questions take priority — even when prev contact said No,
    # an agent asking a qualifying question is NOT a rebuttal.
    if any(p.search(body) for p in _PILLAR_Q_PATTERNS):
        return "pillar_question"

    # Follow-up patterns — scheduled check-ins, not rebuttals.
    if any(p.search(body) for p in _FOLLOW_UP_PATTERNS):
        return "follow_up"

    # Rebuttal — ONLY when the previous contact message was a refusal.
    if prev_contact_said_no and any(p.search(body) for p in _REBUTTAL_PATTERNS):
        return "rebuttal"

    # Opener heuristic — short initial outreach
    if any(w in body_l for w in ["i'd love to chat", "opportunities for your property",
                                  "love to chat about", "reaching out regarding"]):
        return "opener"

    return "other"


def classify_agent_messages(messages: list[dict]) -> dict:
    """
    Walk all messages in order and classify each agent message.
    Returns a summary dict:
      {
        'rebuttals':        int,   # true rebuttals after contact No
        'follow_ups':       int,   # scheduled FU check-ins
        'pillar_questions': int,   # qualifying questions
        'openers':          int,
        'other':            int,
        'rebuttal_count_exceeded': bool,  # > 3 rebuttals = script violation
      }
    """
    counts = {"rebuttals": 0, "follow_ups": 0, "pillar_questions": 0,
              "openers": 0, "other": 0}

    # classify_agent_message returns singular forms; map them to the plural
    # dict keys so counts[msg_type] increments the right bucket.
    _KEY_MAP = {
        "rebuttal":       "rebuttals",
        "follow_up":      "follow_ups",
        "pillar_question":"pillar_questions",
        "opener":         "openers",
        "other":          "other",
    }

    prev_contact_said_no = False

    for m in messages:
        sender = (m.get("sender") or "").lower()
        body   = (m.get("body") or m.get("message") or "").strip()
        if not body:
            continue

        if sender == "contact":
            prev_contact_said_no = _is_contact_no(body)
            continue

        if sender in ("system", ""):
            continue

        # Agent message — classify and increment the correct plural key
        msg_type = classify_agent_message(body, prev_contact_said_no)
        key = _KEY_MAP.get(msg_type, "other")
        counts[key] += 1
        # Reset after agent responds; next contact reply will set it fresh
        prev_contact_said_no = False

    counts["rebuttal_count_exceeded"] = counts["rebuttals"] > 3
    return counts


def _count_rebuttals(agent_msgs: list[dict], contact_msgs: list[dict]) -> int:
    """Legacy shim — kept for callers that pass separate lists.
    Use classify_agent_messages(all_messages) for accurate results."""
    if not contact_msgs:
        return 0
    return max(0, len(agent_msgs) - 1)


def _detect_referral_close(agent_msgs: list[dict]) -> bool:
    """Check if agent mentioned referral or $1k."""
    for m in agent_msgs:
        body = (m.get("body") or m.get("message") or "").lower()
        if any(w in body for w in ["referral", "refer", "$1k", "$1,000", "1000 for"]):
            return True
    return False


def _q(msg: dict, max_len: int = 68) -> str:
    """Return a quoted, truncated snippet from a single message dict."""
    body = (msg.get("body") or msg.get("message") or "").strip()
    if len(body) > max_len:
        body = body[:max_len].rstrip() + "..."
    return f'"{body}"'


def _find_first(msgs: list[dict], patterns: list) -> dict | None:
    """Return the first message whose body matches any pattern."""
    for m in msgs:
        body = m.get("body") or m.get("message") or ""
        if any(p.search(body) for p in patterns):
            return m
    return None




def coaching_bullets(messages: list[dict]) -> list[str]:
    """
    Deterministic coaching observations appended to every ML summary.

    These give reviewers the same kind of "rich feedback" Groq produces:
    repetition, unanswered questions, weak openers, tone friction — facts a
    regex can prove from the transcript, phrased as actionable coaching.
    """
    agent_msgs   = [m for m in messages if (m.get("sender") or "").lower() not in ("contact", "system", "")]
    contact_msgs = [m for m in messages if (m.get("sender") or "").lower() == "contact"]
    bullets: list[str] = []

    # 1. Verbatim duplicate agent message — robotic / copy-paste messaging
    seen: dict[str, int] = {}
    for m in agent_msgs:
        body = re.sub(r"\s+", " ", (m.get("body") or m.get("message") or "").strip().lower())
        if len(body) < 40:
            continue
        seen[body] = seen.get(body, 0) + 1
    if any(c >= 2 for c in seen.values()):
        bullets.append(
            "Texter sent the same message twice verbatim — repeated copy-paste "
            "reads as robotic; vary the phrasing on the second send"
        )

    # 2. Conversation ended on an unanswered contact question
    last_substantive = next(
        (m for m in reversed(messages)
         if (m.get("body") or m.get("message") or "").strip()), None,
    )
    if last_substantive is not None:
        sender = (last_substantive.get("sender") or "").lower()
        body = (last_substantive.get("body") or last_substantive.get("message") or "")
        if sender == "contact" and "?" in body:
            bullets.append(
                f"Conversation ended with the contact's question unanswered: {_q(last_substantive)} — "
                "a reply (even a closing one) was owed here"
            )

    # 3. Permission-style opener followed by a "No" the agent talked past
    perm_idx = next(
        (i for i, m in enumerate(messages)
         if (m.get("sender") or "").lower() not in ("contact", "system", "")
         and _PERMISSION_OPENER_RE.search(m.get("body") or m.get("message") or "")),
        None,
    )
    if perm_idx is not None:
        after = messages[perm_idx + 1:]
        contact_said_no = any(
            (m.get("sender") or "").lower() == "contact" and _is_contact_no(
                (m.get("body") or m.get("message") or "").strip())
            for m in after[:2]
        )
        agent_continued = any(
            (m.get("sender") or "").lower() not in ("contact", "system", "")
            for m in after[1:]
        )
        if contact_said_no and agent_continued:
            bullets.append(
                "Opener asked permission ('May I ask you something?') and the contact said No — "
                "continuing past that 'No' reads as ignoring them; the script opener "
                "(address + opportunity) avoids inviting a free refusal"
            )

    # 4. Contact showed condescension/mockery — tone friction signal
    contact_text = " \n ".join(
        (m.get("body") or m.get("message") or "") for m in contact_msgs
    )
    if _CONDESCENSION_RE.search(contact_text):
        mock_msg = next(
            (m for m in contact_msgs
             if _CONDESCENSION_RE.search(m.get("body") or m.get("message") or "")), None,
        )
        if mock_msg is not None:
            bullets.append(
                f"Contact responded with mockery/condescension: {_q(mock_msg)} — "
                "dismissive hostility; DO Not Call is an accepted close for this tone"
            )

    return bullets


def build_summary(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    scores: dict,
    *,
    model_used: str = "prefilter",
) -> str:
    """
    Build a rich, bullet-point summary from conversation content.

    Each bullet captures a key fact with actual quoted snippets from the
    conversation so the reviewer can understand what happened at a glance.
    Format:
        * Contact said: "..."
        * Texter rebutted with 2 follow-up messages
        * Contact then asked: "..." -> Potential
    """
    agent_msgs   = [m for m in messages if (m.get("sender") or "").lower() != "contact"]
    contact_msgs = [m for m in messages if (m.get("sender") or "").lower() == "contact"]

    tone         = _classify_contact_tone(contact_msgs)
    has_referral = _detect_referral_close(agent_msgs)
    n_agent      = len(agent_msgs)
    n_contact    = len(contact_msgs)
    # Use the real classifier — not the old "every agent msg = rebuttal" hack
    msg_classification = classify_agent_messages(messages)
    rebuttals    = msg_classification["rebuttals"]
    follow_ups   = msg_classification["follow_ups"]
    pillar_qs    = msg_classification["pillar_questions"]

    bullets: list[str] = []

    # ─────────────────────────────────────────────────────────────────────────
    if tone == "silent":
        if n_agent == 1:
            bullets.append(f"Texter sent an initial outreach to {contact_name}")
        else:
            bullets.append(f"Texter sent {n_agent} messages — {contact_name} never replied")
        bullets.append("No contact engagement — one-sided outreach")
        if has_referral:
            bullets.append("Referral close included in outreach")
        bullets.append("No rule violations detected")

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "hostile":
        hostile_msg = _find_first(contact_msgs, _HOSTILE_PATTERNS) or contact_msgs[-1]
        bullets.append(f"Contact replied: {_q(hostile_msg)} — hostile language detected")
        bullets.append("Texter did not escalate — handled professionally")
        bullets.append("Conversation ended without compliance violations")

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "wrong_number":
        wn_msg = _find_first(contact_msgs, _WRONG_NUMBER_PATTERNS) or contact_msgs[0]
        bullets.append(f"Contact said: {_q(wn_msg)}")
        bullets.append(f"{contact_name} is not the property owner or indicated wrong number")
        if has_referral:
            bullets.append("Texter pivoted to referral close")
        else:
            bullets.append("Texter acknowledged and closed the conversation")

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "already_sold":
        sold_msg = _find_first(contact_msgs, _SOLD_PATTERNS) or contact_msgs[0]
        bullets.append(f"Contact said: {_q(sold_msg)}")
        bullets.append("Property is already sold or under contract")
        if has_referral:
            bullets.append("Texter pivoted to referral close")
        else:
            bullets.append("Texter acknowledged and ended conversation")

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "potential":
        # Step 1: find the initial NI message
        ni_msg = _find_first(contact_msgs, _NOT_INTERESTED_PATTERNS)
        if ni_msg:
            bullets.append(f"{contact_name} initially said: {_q(ni_msg)}")

        # Step 2: texter's rebuttal breakdown (rebuttals vs follow-ups vs pillar questions)
        if rebuttals == 1:
            bullets.append("Texter sent 1 professional rebuttal to keep the conversation open")
        elif rebuttals == 2:
            bullets.append("Texter sent 2 script-prescribed rebuttals before contact reconsidered")
        elif rebuttals >= 3:
            bullets.append(f"Texter sent {rebuttals} rebuttals")
        if follow_ups:
            bullets.append(f"Texter also sent {follow_ups} scheduled follow-up message(s)")
        if pillar_qs:
            bullets.append(f"Texter asked {pillar_qs} qualifying question(s) (pillar questions)")

        # Step 3: find the reversal message (post-NI positive/price inquiry)
        found_ni = False
        reversal_msg = None
        for m in contact_msgs:
            body = m.get("body") or m.get("message") or ""
            if not found_ni and _find_first([m], _NOT_INTERESTED_PATTERNS):
                found_ni = True
                continue
            if found_ni and any(p.search(body) for p in _POSITIVE_PATTERNS):
                reversal_msg = m
                break

        if reversal_msg:
            bullets.append(f"Contact then asked: {_q(reversal_msg)}")
            bullets.append(
                f"Price inquiry after initial decline — {contact_name} is open to hearing a number (Potential)"
            )
        else:
            # No reversal AFTER the decline — check whether the conversation
            # actually ENDED on a No (engagement came before the decline).
            _last_contact = contact_msgs[-1] if contact_msgs else None
            _ended_on_no = _last_contact is not None and (
                _is_contact_no((_last_contact.get("body") or _last_contact.get("message") or "").strip())
                or _BUYER_SIDE_REJECTION_RE.search(
                    _last_contact.get("body") or _last_contact.get("message") or "")
            )
            if _ended_on_no:
                bullets.append(
                    "Contact engaged mid-conversation but ultimately declined — "
                    "the conversation ended on a refusal"
                )
            else:
                bullets.append(
                    "Contact showed renewed engagement after initial decline — labeled Potential"
                )

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "not_interested":
        ni_msg = _find_first(contact_msgs, _NOT_INTERESTED_PATTERNS) or contact_msgs[0]
        bullets.append(f"{contact_name} said: {_q(ni_msg)}")
        # Accurate breakdown: true rebuttals vs follow-ups vs pillar questions
        if rebuttals == 0 and follow_ups == 0:
            bullets.append("Texter sent initial outreach only — no rebuttal attempted")
        elif rebuttals == 1:
            bullets.append("Texter sent 1 rebuttal then closed cleanly")
        elif rebuttals == 2:
            bullets.append("Texter sent 2 rebuttals then closed cleanly")
        elif rebuttals == 3:
            bullets.append("Texter used all 3 script-prescribed rebuttals then closed")
        elif rebuttals > 3:
            bullets.append(f"Texter sent {rebuttals} rebuttals — very persistent outreach")
        if follow_ups:
            bullets.append(f"Texter also sent {follow_ups} follow-up message(s) during silence")
        if pillar_qs:
            bullets.append(f"Texter asked {pillar_qs} qualifying question(s)")
        if has_referral:
            bullets.append("Referral close included")
        bullets.append("Contact did not reverse — conversation concluded")

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "interested":
        first_pos = _find_first(contact_msgs, _POSITIVE_PATTERNS) or contact_msgs[0]
        bullets.append(f"{contact_name} replied: {_q(first_pos)}")
        pillars = sorted(detect_gathered_pillars(messages))
        if pillars:
            bullets.append(f"Pillars gathered: {', '.join(pillars)}")
        # Breakdown: pillar questions asked vs total agent messages
        if pillar_qs:
            bullets.append(f"Texter asked {pillar_qs} qualifying question(s) to gather pillars")
        bullets.append(
            f"Texter sent {n_agent} total messages to qualify the lead"
            + (" — referral close included" if has_referral else "")
        )

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "maybe":
        maybe_msg = _find_first(contact_msgs, _MAYBE_PATTERNS) or contact_msgs[0]
        bullets.append(f"{contact_name} said: {_q(maybe_msg)}")
        bullets.append("Contact expressed tentative or future interest")
        if follow_ups:
            bullets.append(f"Texter sent {follow_ups} scheduled follow-up message(s)")
        if pillar_qs:
            bullets.append(f"Texter asked {pillar_qs} qualifying question(s)")
        bullets.append(
            f"Texter sent {n_agent} total qualifying messages"
            + (" — referral close included" if has_referral else "")
        )

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "emoji_only":
        if contact_msgs:
            bullets.append(f"Contact replied only with emoji: {_q(contact_msgs[-1])}")
        bullets.append("No substantive text engagement from contact")
        if n_agent > 1:
            bullets.append(f"Texter sent {n_agent} follow-up messages — no text response received")

    # ─────────────────────────────────────────────────────────────────────────
    elif tone == "brief":
        if contact_msgs:
            bullets.append(f"{contact_name} gave a brief reply: {_q(contact_msgs[-1])}")
        bullets.append("No clear intent expressed")
        if n_agent > 1:
            bullets.append(f"Texter sent {n_agent} total messages")

    # ─────────────────────────────────────────────────────────────────────────
    else:  # neutral / fallback
        if contact_msgs:
            bullets.append(f"Last contact reply: {_q(contact_msgs[-1])}")
        bullets.append(
            f"Texter exchanged {n_agent + n_contact} messages with {contact_name}"
        )
        bullets.append("No compliance issues detected")

    # ── Price-negotiation context (Abv MV) ────────────────────────────────────
    # A "No" AFTER the contact quoted a price is a price disagreement, not
    # seller disinterest — surface that distinction whatever the tone branch.
    _abv = detect_abv_mv_response(messages)
    if _abv["contact_stated_price"] and _abv["price_msg_index"] is not None:
        _decline_after_price = any(
            (m.get("sender") or "").lower() == "contact"
            and _is_contact_no((m.get("body") or m.get("message") or "").strip())
            for m in messages[_abv["price_msg_index"] + 1:]
        )
        if _decline_after_price:
            price_msg = messages[_abv["price_msg_index"]]
            bullets.append(
                f"Contact quoted an asking price of ${_abv['price_amount']:,.0f}: {_q(price_msg)}"
            )
            if _abv["agent_did_referral_close"]:
                bullets.append(
                    "Texter used the script's above-market referral close ($1k referral offer)"
                )
            buyer_msg = _find_first(contact_msgs, [_BUYER_SIDE_REJECTION_RE])
            if buyer_msg:
                bullets.append(
                    f"Contact rejected the texter's range as too low: {_q(buyer_msg)} — "
                    "a price disagreement (Abv MV territory), not disinterest in selling"
                )
            else:
                bullets.append(
                    "The decline came after a price exchange — disagreement on price, "
                    "not a refusal to sell (Abv MV is an accepted label here)"
                )

    # ── Coaching observations (duplicates, unanswered questions, tone) ────────
    for cb in coaching_bullets(messages):
        if cb not in bullets:
            bullets.append(cb)

    # ── Compliance note ───────────────────────────────────────────────────────
    comp = scores.get("compliance_score", 100)
    if comp < 80:
        bullets.append(f"Compliance score: {comp} — review flagged messages")
    elif comp >= 95 and tone not in ("silent", "hostile"):
        bullets.append("No rule violations detected")

    return "\n".join(f"* {b}" for b in bullets)



def detect_label(
    messages: list[dict],
    contact_name: str,
) -> tuple[str, str]:
    """
    Detect a reasonable label + label_reason from conversation content.
    Returns (label, label_reason).
    """
    agent_msgs   = [m for m in messages if (m.get("sender") or "").lower() != "contact"]
    contact_msgs = [m for m in messages if (m.get("sender") or "").lower() == "contact"]
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
        return ("Sold",
                f"{contact_name} clearly stated the property is already sold or under contract.")

    if tone == "not_interested":
        return ("Not interested",
                f"{contact_name} expressed disinterest in selling.")

    if tone == "interested":
        return ("New Lead",
                f"{contact_name} showed direct interest in the conversation.")

    if tone == "potential":
        return ("Potential",
                f"{contact_name} expressed curiosity or inquired about an offer after initial hesitation.")

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
    agent_msgs   = [m for m in messages if (m.get("sender") or "").lower() != "contact"]
    contact_msgs = [m for m in messages if (m.get("sender") or "").lower() == "contact"]

    if not contact_msgs:
        return "none"

    tone = _classify_contact_tone(contact_msgs)
    if tone in ("silent", "hostile", "wrong_number", "emoji_only"):
        return "none"

    if tone in ("interested", "maybe"):
        # Count pillars the LEAD actually answered — not topic keywords the
        # agent merely asked about (that inflated the funnel stage).
        pillar_hits = len(detect_gathered_pillars(messages))
        if pillar_hits >= 3:
            return "mid_funnel"
        if pillar_hits >= 1:
            return "wide_funnel"
        return "initial_contact"

    return "none"


def _get_snippet(msg: dict, max_len: int = 50) -> str:
    """Get a short snippet of a message for quoting in summaries."""
    body = (msg.get("body") or msg.get("message") or "").strip()
    if not body:
        return ""
    if len(body) <= max_len:
        return body
    # Cut at word boundary
    truncated = body[:max_len].rsplit(" ", 1)[0]
    return truncated + "..."
