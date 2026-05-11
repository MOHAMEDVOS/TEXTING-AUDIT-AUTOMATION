"""Deterministic label checks for ML-prefiltered clean conversations."""
from __future__ import annotations

import re

from ai.prefilter.summary_builder import detect_fu_track, validate_fu_label, detect_abv_mv_response
from ai.prefilter.tier1_phrases_v2 import _PILLAR_PATTERNS, _PILLAR_THRESHOLD

# Any contact price >= $1M → team labels as Do Not Call (inflated/ABV MV).
# EXCEPTION: if the assigned label is "Bluffer", the inflated price IS the
# bluffing behavior — accept it as correct instead of overriding to DNC.
_INFLATED_PRICE_WORD_RE = re.compile(
    r"\b((a|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
    r"(million|mil)\b",
    re.I,
)
_INFLATED_PRICE_NUMERIC_RE = re.compile(
    r"(?:\$\s*)?(\d{1,4})(?:[.,](\d{3}))*\s*(million|mil|m\b)",
    re.I,
)
# Matches bare dollar amounts >= $1,000,000 after comma-stripping
_INFLATED_PRICE_BARE_RE = re.compile(r"\$\s*[\d,]{8,}", re.I)  # $1,000,000 = 9 chars with commas
_SEVEN_FIGURE_RE = re.compile(r"\b(7|seven)\s*[-\s]?\s*figure\b", re.I)
_INFLATED_WORD_NUMS = {
    "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _contact_stated_million_plus(messages: list[dict]) -> bool:
    """True if any contact message contains a price >= $1M."""
    for m in messages:
        if (_sender(m) or "").lower() != "contact":
            continue
        body = _body(m)
        if _SEVEN_FIGURE_RE.search(body):
            return True
        body_lower = body.lower()
        word_m = _INFLATED_PRICE_WORD_RE.search(body_lower)
        if word_m:
            multiplier = (word_m.group(2) or "one").lower()
            if _INFLATED_WORD_NUMS.get(multiplier, 1) >= 1:
                return True
        if _INFLATED_PRICE_NUMERIC_RE.search(body_lower):
            return True
        # Bare dollar amount e.g. "$2,000,000" — strip commas and check digit count
        bare_m = _INFLATED_PRICE_BARE_RE.search(body)
        if bare_m:
            digits = re.sub(r"[^\d]", "", bare_m.group())
            if int(digits) >= 1_000_000:
                return True
    return False


_WRONG_NUMBER = [
    re.compile(r"\bwrong\s+(number|phone|person|#)\b", re.I),
    re.compile(r"\bwrong\s*#\b", re.I),
    re.compile(r"\bro+ng\s+(number|#)\b", re.I),  # "Rong number" typo
    re.compile(r"\bnot\s+(me|mine|my\s+property|the\s+owner)\b", re.I),
    re.compile(r"\byou\s+have\s+the\s+wrong\s+(person|number)\b", re.I),
    re.compile(r"\bway\s+off\s+base\b", re.I),
    re.compile(r"\bmy\s+name\s+is\s+not\b", re.I),
    re.compile(r"\bi\s+don'?t\s+(live|own|have).{0,40}\b(address|property|house|home)\b", re.I),
    re.compile(r"\bnot\s+\w+'?s?\s+(number|phone)\b", re.I),  # "not Ronald's phone"
    re.compile(r"\bnot\s+familiar\s+with\s+that\s+address\b", re.I),
    # "I am not [Name]" / "It is not [Name]" — only proper names, not NI phrases
    # Excludes: interested, selling, ready, looking, available, the, sure, doing, etc.
    re.compile(
        r"(?i:\b(i'?m|i\s+am|this\s+is|it\s+is|it'?s)\s+not\s+)"
        r"(?!(?i:interested|selling|ready|looking|available|the|sure|doing|something|going|for\s+sale|at\s+))"
        r"(?=[A-Z])[A-Za-z]{3,}\b",
    ),
    # "Who is [Name]?" — contact confused about who is being texted
    re.compile(r"(?i:who\s+is\s+)(?=[A-Z])[A-Za-z]{2,}\b.*\?"),
    # "That's my daughter's/son's/sister's/brother's house"
    re.compile(r"\b(my|his|her|their)\s+(daughter|son|sister|brother|mother|father|parent|spouse|wife|husband|relative|friend)'?s?\s+(house|home|property|place)\b", re.I),
    # "I don't own" / "I don't have a home" / "I rent"
    re.compile(r"\bdon'?t\s+(own|have\s+a\s+(house|home|property))\b", re.I),
    re.compile(r"\bi\s+(rent|am\s+renting|don'?t\s+own)\b", re.I),
    # "Her number is X" — forwarding to actual owner
    re.compile(r"\b(her|his|their)\s+number\s+is\b", re.I),
    # "I don't have a home" — explicit WN phrase from real data
    re.compile(r"\bi\s+don'?t\s+have\s+a\s+(home|house|property)\b", re.I),
    # "If I owned it" — not the owner
    re.compile(r"\bif\s+i\s+(owned|own)\s+(it|the)\b", re.I),
    # "Someone texted you from my phone"
    re.compile(r"\bsomeone\s+(texted|messaged|contacted)\s+you\s+from\s+my\b", re.I),
    # Variations found in real data
    re.compile(r"\bwrong\s*#[!]*\b", re.I),
    re.compile(r"\bnot\s+\w+\b.{0,20}\bwrong\s*#\b", re.I),   # "Not Ivan, wrong #"
    re.compile(r"\bwrong\s+contact\b", re.I),
    re.compile(r"\bwrong\s+party\b", re.I),
    re.compile(r"\bno\s+longer\s+(has\s+)?this\s+(phone\s+)?(number|#)\b", re.I),
    re.compile(r"\bno\s+longer\s+the\s+(holder|owner)\s+of\s+this\s+number\b", re.I),
    re.compile(r"\bnot\s+affiliated\s+with\s+this\s+address\b", re.I),
    re.compile(r"\bwe\s+do\s+not\s+(live|manage|own).{0,30}\baddress\b", re.I),
    re.compile(r"\bdo\s+not\s+(live|own|manage)\s+(here|there|at\s+that)\b", re.I),
    re.compile(r"\bdon'?t\s+(live|manage|own)\s+(here|there|at\s+that)\b", re.I),
    re.compile(r"\b(is|are)\s+deceased\b", re.I),   # "Lethais deceased"
    re.compile(r"\bi\s+do\s+not\s+own\s+a\s+(house|property|home)\s+at\b", re.I),
    re.compile(r"\bthat'?s?\s+not\s+my\s+(place|property|house|number)\b", re.I),
    re.compile(r"\bwho\s+(the\s+)?(hell\s+)?is\s+\w+\?", re.I),   # "Who the hell is Amjad?"
    # "I'm not [Name]" — excludes NI/selling phrases like "I'm not interested"
    re.compile(
        r"\bi'?m\s+not\s+(?!(?:interested|selling|ready|looking|available|sure|doing|going|for\s+sale|at\s+)\b)[A-Za-z]{3,}\b",
        re.I,
    ),
    # "This isn't [Name]" — same exclusion guard
    re.compile(
        r"\bthis\s+isn'?t\s+(?!(?:interested|selling|ready|looking|available|sure|doing|going|for\s+sale|at\s+)\b)[A-Za-z]{3,}\b",
        re.I,
    ),
    re.compile(r"\bi\s+am\s+not\s+affiliated\b", re.I),
    re.compile(r"\bnot\s+\w+'?s?\s+(phone|number)\s+anymore\b", re.I),
]

_DNC = [
    re.compile(r"\bstop\s+(texting|messaging|contacting|calling|bothering)\s+me\b", re.I),
    re.compile(r"\bstop\s+(texting|messaging|contacting|calling)\b", re.I),
    re.compile(r"\bno\s+more\s+(texts?|messages?|calls?|contact)\b", re.I),
    re.compile(r"\bno\s+more\s+(texting|messaging|contacting|calling)\b", re.I),
    re.compile(r"\bplease\s+(no\s+more|stop)\s+(texts?|messages?)\b", re.I),
    re.compile(r"\bdo\s+not\s+(text|contact|call|message|txt)\b", re.I),
    re.compile(r"\bdon'?t\s+(text|txt|contact|call|message)\s+me\b", re.I),
    re.compile(r"\bplease\s+do\s+not\s+(text|txt|contact|call|message)\b", re.I),
    re.compile(r"\bremove\s+me\b", re.I),
    re.compile(r"\btake\s+.{0,20}off\s+your\s+list\b", re.I),  # "take my house off your list"
    re.compile(r"\boff\s+your\s+(list|database|system)\b", re.I),
    re.compile(r"\bopt\s*out\b", re.I),
    re.compile(r"\bkick\s+rocks\b", re.I),
    re.compile(r"\bdont\s+contact\b", re.I),
    re.compile(r"\bno\s+please\s+don'?t\s+ask\b", re.I),
    # Middle finger emoji alone = DNC
    re.compile(r"^\s*[\U0001F595][\U0001F3FB-\U0001F3FF]?\s*$", re.MULTILINE),
    re.compile(r"\balone\s+time\b", re.I),
    re.compile(r"\b(meeting|seeing)\s+you\b.{0,80}\b(fun|nice|good)\b.{0,80}\b(alone|private)\b", re.I),
    re.compile(r"\bgo\s+somewhere\b.{0,80}\b(alone|private)\b", re.I),
    re.compile(r"\bjust\s+want\s+to\s+(meet|see)\s+you\b", re.I),
    re.compile(r"\bnot\s+(about|for)\s+(the\s+)?(house|property)\b.{0,80}\b(you|meet|date)\b", re.I),
    # Real patterns from data
    re.compile(r"\bcease\s+and\s+desist\b", re.I),
    re.compile(r"\bi\s+do\s+not\s+wish\s+to\s+be\s+contacted\b", re.I),
    re.compile(r"\blose\s+my\s+(number|#|text)\b", re.I),
    re.compile(r"\bloose\s+my\s+(number|#|text)\b", re.I),  # "Loose my number" typo
    re.compile(r"\bplease\s+don'?t\s+bother\b", re.I),
    re.compile(r"\bdon'?t\s+bother\b.{0,30}\bmore\b", re.I),
    re.compile(r"\breporting\s+this\s+number\s+to\b", re.I),   # "reporting to BBB"
    re.compile(r"\bno\s+please\s+remove\s+from\b", re.I),
    re.compile(r"\bremove\s+from\s+(mailing\s+)?list\b", re.I),
    re.compile(r"\bgo\s+away\b", re.I),
    re.compile(r"\bunsolicited\s+texts\b", re.I),
    re.compile(r"\bhow\s+many\s+times\s+do\s+(you|yall|ya'?ll)\s+need\s+to\s+be\s+told\b", re.I),
    # Middle/ring finger emoji alone = DNC
    re.compile(r"^\s*[\U0001F595\U0001F918][\U0001F3FB-\U0001F3FF]?\s*$", re.MULTILINE),
    # Blocking / spam complaints — hostile DNC signals
    re.compile(r"\b(gonna\s+)?keep\s+blocking\s+you\b", re.I),
    re.compile(r"\bblocking\s+(you|this\s+(number|#)|all\s+of\s+you)\b", re.I),
    re.compile(r"\bspam\s+bot(s)?\b", re.I),
    re.compile(r"\btired\s+of\s+(you|your|these)\s+(spam|texts?|messages?|calls?|bots?)\b", re.I),
    re.compile(r"\bstop\s+(sending|with)\s+(these\s+)?(spam|unsolicited)\b", re.I),
    re.compile(r"\byou\s+(pieces?\s+of\s+shit|piece\s+of\s+shit|assholes?|fuckers?)\b", re.I),

    # ── Conjunction patterns: "don't text or call me", "stop texting and calling" ──
    re.compile(r"\bdon'?t\s+(text|txt|call|contact|message)\s+(or|and|,)\s+(text|txt|call|contact|message)(\s+me)?\b", re.I),
    re.compile(r"\bplease\s+don'?t\s+(text|txt|call|contact|message)\s+(or|and|,)\s+(text|txt|call|contact|message)\b", re.I),
    re.compile(r"\bdo\s+not\s+(text|txt|call|contact|message)\s+(or|and|,)\s+(text|txt|call|contact|message)\b", re.I),
    re.compile(r"\bstop\s+(texting|calling|messaging|contacting)\s+(and|or|,)\s+(texting|calling|messaging|contacting)(\s+me)?\b", re.I),
    re.compile(r"\bno\s+more\s+(texts?|calls?|messages?)\s+(or|and|,)\s+(texts?|calls?|messages?)\b", re.I),
    re.compile(r"\bplease\s+(no\s+more|stop)\s+(texts?|calls?|messages?)\s+(or|and|,)\s+(texts?|calls?|messages?)\b", re.I),
    # "don't text, call, or message me" — 3-verb lists
    re.compile(r"\bdon'?t\s+(text|txt|call|contact|message),?\s+(text|txt|call|contact|message),?\s+(or|and)\s+(text|txt|call|contact|message)(\s+me)?\b", re.I),

    # ── "Again" / "Anymore" / "Ever" patterns ──
    re.compile(r"\bdon'?t\s+(text|txt|call|contact|message)\s+me\s+again\b", re.I),
    re.compile(r"\bdon'?t\s+(text|txt|call|contact|message)\s+(me\s+)?anymore\b", re.I),
    re.compile(r"\bdon'?t\s+ever\s+(text|txt|call|contact|message)\s+me\b", re.I),
    re.compile(r"\bnever\s+(text|txt|call|contact|message)\s+me\s+(again|anymore)\b", re.I),
    re.compile(r"\bnever\s+(text|txt|call|contact|message)\s+(this\s+)?(number|phone)\s+(again|anymore)?\b", re.I),
    re.compile(r"\bplease\s+never\s+(text|txt|call|contact|message)\b", re.I),
    re.compile(r"\bdo\s+not\s+(ever\s+)?(text|txt|call|contact|message)\s+me\s+again\b", re.I),
    re.compile(r"\bdo\s+not\s+(text|txt|call|contact|message)\s+(me\s+)?anymore\b", re.I),
    re.compile(r"\bstop\s+(texting|calling|messaging|contacting)\s+me\s+(again|anymore|ever)\b", re.I),

    # ── "Please don't [verb]" without requiring "me" at end ──
    re.compile(r"\bplease\s+don'?t\s+(text|txt|call|contact|message)\b", re.I),
    re.compile(r"\bplease\s+stop\s+(texting|calling|messaging|contacting)\b", re.I),
    re.compile(r"\bplease\s+just\s+stop\s+(texting|calling|messaging|contacting)?\b", re.I),

    # ── "Leave me alone" variants (explicit in prompts as opt-out) ──
    re.compile(r"\bleave\s+me\s+(alone|be)\b", re.I),
    re.compile(r"\bjust\s+leave\s+me\s+(alone|be)\b", re.I),
    re.compile(r"\bplease\s+(just\s+)?leave\s+me\s+(alone|be)\b", re.I),
    re.compile(r"\bleave\s+me\s+the\s+(hell|fuck|f\*ck|fck|heck)\s+alone\b", re.I),
    re.compile(r"\bleave\s+(us|him|her|them)\s+(alone|be)\b", re.I),

    # ── Quit / modal verb "stop" variants ──
    re.compile(r"\bquit\s+(texting|calling|messaging|contacting|bothering)\s*me?\b", re.I),
    re.compile(r"\bjust\s+stop\b", re.I),
    re.compile(r"\bcan\s+you\s+(please\s+)?stop\s+(texting|calling|messaging|contacting)\b", re.I),
    re.compile(r"\bwould\s+you\s+(please\s+)?stop\s+(texting|calling|messaging|contacting)\b", re.I),
    re.compile(r"\bwill\s+you\s+(please\s+)?stop\s+(texting|calling|messaging|contacting)\b", re.I),
    re.compile(r"\bcould\s+you\s+(please\s+)?stop\s+(texting|calling|messaging|contacting)\b", re.I),

    # ── Abbreviation patterns: "pls", "plz", "dont" (no apostrophe) ──
    re.compile(r"\bplz\s+(stop|don'?t|dont)\s*(texting|calling|messaging|contacting)?\b", re.I),
    re.compile(r"\bpls\s+(stop|don'?t|dont)\s*(texting|calling|messaging|contacting)?\b", re.I),
    re.compile(r"\bdont\s+(text|txt|call|contact|message)\s+me\b", re.I),
    re.compile(r"\bdont\s+(text|txt|call|contact|message)\s+(or|and)\s+(text|txt|call|contact|message)\b", re.I),
    re.compile(r"\bdont\s+ever\s+(text|txt|call|contact|message)\b", re.I),

    # ── Exasperated / repeated request patterns ──
    re.compile(r"\bi\s+(said|already\s+said)\s+(stop|no)\b", re.I),
    re.compile(r"\bi\s+(told|already\s+told)\s+you\s+(to\s+)?(stop|no)\b", re.I),
    re.compile(r"\bfor\s+the\s+last\s+time\b", re.I),
    re.compile(r"\benough\s+already\b", re.I),
    re.compile(r"\bhow\s+many\s+times\s+(do\s+)?i\s+(have\s+to|need\s+to|gotta)\s+(say|tell)\b", re.I),
    re.compile(r"\bi'?ve\s+(asked|told)\s+you\s+to\s+stop\b", re.I),
    re.compile(r"\bi\s+already\s+asked\s+you\s+to\s+stop\b", re.I),
    re.compile(r"\bseriously\s+stop\b", re.I),
    re.compile(r"\bi\s+said\s+don'?t\s+(text|call|contact|message)\b", re.I),

    # ── Threat / legal / harassment patterns ──
    re.compile(r"\bthis\s+is\s+harassment\b", re.I),
    re.compile(r"\b(i'?m|i\s+am)\s+(going\s+to|gonna)\s+(report|sue|file)\b", re.I),
    re.compile(r"\bi\s+will\s+(report|sue|file)\b", re.I),
    re.compile(r"\bcontacting\s+(my|a|an)\s+(lawyer|attorney|legal)\b", re.I),
    re.compile(r"\bdo\s+not\s+call\s+list\b", re.I),
    re.compile(r"\bnational\s+do\s+not\s+call\b", re.I),
    re.compile(r"\btcpa\b", re.I),
    re.compile(r"\bfiling\s+a\s+(complaint|report|lawsuit)\b", re.I),
    re.compile(r"\billegal\s+to\s+(text|call|contact|message)\b", re.I),
    re.compile(r"\bi'?ll\s+(call|contact)\s+(the\s+)?(police|cops|authorities|fcc|ftc|bbb)\b", re.I),
    re.compile(r"\breport(ing)?\s+(you|this|your\s+number)\s+to\b", re.I),

    # ── "No further" / finality patterns ──
    re.compile(r"\bno\s+further\s+(contact|communication|texts?|messages?|calls?)\b", re.I),
    re.compile(r"\bdon'?t\s+reach\s+out\b", re.I),
    re.compile(r"\bdo\s+not\s+reach\s+out\b", re.I),
    re.compile(r"\bnever\s+reach\s+out\b", re.I),
    re.compile(r"\bthis\s+conversation\s+is\s+over\b", re.I),
    re.compile(r"\bwe'?re\s+done\s+here\b", re.I),
    re.compile(r"\bi'?m\s+done\s+(with\s+)?(you|this|these)\b", re.I),
    re.compile(r"\bno\s+more\s+communication\b", re.I),
    re.compile(r"\bstop\s+all\s+(contact|communication|texts?|messages?)\b", re.I),
    re.compile(r"\bend\s+this\s+conversation\b", re.I),
    re.compile(r"\bdon'?t\s+(want|need)\s+(any\s+)?(more\s+)?(texts?|messages?|calls?|contact)\b", re.I),
    re.compile(r"\bi\s+don'?t\s+want\s+to\s+(hear|be\s+contacted|be\s+texted|be\s+called)\b", re.I),
    re.compile(r"\bplease\s+(do\s+not|don'?t)\s+(ever\s+)?reach\s+out\b", re.I),
]

_SOLD = [
    re.compile(r"\b(sold|already\s+sold)\b", re.I),
    re.compile(r"\bno\s+longer\s+own\b", re.I),
]

# Phrases that indicate "sold" refers to a neighboring/other/third property, not the subject
_SOLD_NEIGHBOR_CONTEXT = [
    re.compile(r"\b(next\s+door|neighbor(?:s)?|nearby|down\s+the\s+street|across\s+the\s+street|adjacent)\b.{0,60}\bsold\b", re.I),
    re.compile(r"\bsold\b.{0,60}\b(next\s+door|neighbor(?:s)?|nearby|down\s+the\s+street|across\s+the\s+street|adjacent)\b", re.I),
    re.compile(r"\bhouse\s+next\s+(?:door\s+)?sold\b", re.I),
    re.compile(r"\bthe\s+(?:house|property|home)\s+(?:next\s+door|nearby|adjacent)\s+sold\b", re.I),
    # "I sold a 3rd property" / "just sold another property" — not the subject address
    re.compile(r"\b(just\s+)?sold\s+(a|an|another|my\s+(?:2nd|3rd|4th|other|another))\s+(?:\d+\w*\s+)?(?:property|house|home|place)\b", re.I),
    re.compile(r"\bsold\s+(?:a\s+)?(?:2nd|3rd|4th|5th|second|third|fourth|fifth|other|another)\s+(?:property|house|home)\b", re.I),
]

_NOT_INTERESTED = [
    re.compile(r"\bnot\s+(interested|selling|looking|ready|for\s+sale|at\s+this\s+time|yet)\b", re.I),
    re.compile(r"\bnot\s+at\s+all(\s+likely)?\b", re.I),
    re.compile(r"\b(no|not)\s+(for\s+)?sale\b", re.I),
    re.compile(r"\bnot\s+(selling|interested)\b", re.I),
    re.compile(r"\bno\s+thank(s|\s+you)?\b", re.I),
    re.compile(r"\bnever\s+sell\b", re.I),
    re.compile(r"\bwill\s+never\s+sell\b", re.I),
    re.compile(r"\bnot\s+now\b", re.I),
    re.compile(r"\bnot\s+at\s+(the\s+)?moment\b", re.I),
    re.compile(r"\bnot\s+at\s+this\s+time\b", re.I),
    re.compile(r"\bno\s+we\s+are\s+fine\b", re.I),
    re.compile(r"\bno,?\s+i\s+don'?t\b", re.I),
    re.compile(r"\bnot\s+int?e?r?e?sted\b", re.I),  # covers "not intrested" typo
    re.compile(r"\bnot\s+a\s+chance\b", re.I),
    re.compile(r"\bzero\s+interest\b", re.I),
    re.compile(r"\bim\s+not\s+selling\b", re.I),
    re.compile(r"\bi'?m\s+not\s+selling\b", re.I),
    re.compile(r"\babsolutely\s+not\b", re.I),
    re.compile(r"\bno\s+ty\b", re.I),
    re.compile(r"\bno,?\s+thank\s+you\b.{0,30}\b(day|bless|well)\b", re.I),
    re.compile(r"\bdon'?t\s+want\s+to\s+sell\b", re.I),
    re.compile(r"\bi\s+don'?t\s+want\s+to\s+sell\b", re.I),
    # standalone "No" with optional emoji/punctuation, or repeated No's
    re.compile(r"^\s*no[o!?.]*\s*(?:[\U0001F300-\U0001FFFF]|\d{0,2})?\s*$", re.I | re.MULTILINE),
    re.compile(r"^\s*nope[.!o]?\s*$", re.I | re.MULTILINE),
    re.compile(r"^\s*never[.!]?\s*$", re.I | re.MULTILINE),
    re.compile(r"\bno+pe+\b", re.I),   # Nooooo, nooooope
    # thumbs-down emoji (any skin tone) alone on a line
    re.compile(r"^\s*\U0001F44E[\U0001F3FB-\U0001F3FF]?\s*$", re.MULTILINE),
    re.compile(r"^\s*[\U0001F4AF\U0001F918]+\s*$", re.MULTILINE),
    # "family house/home" — been in the family, not selling
    re.compile(r"\b(family\s+(house|home|property)|it'?s?\s+(a\s+)?family\s+(house|home|property))\b", re.I),
    re.compile(r"\b(been\s+in\s+(the|our|my)\s+family)\b", re.I),
    re.compile(r"\bin\s+(the|our|my)\s+family\s+(for|since|over)\b", re.I),
    re.compile(r"\b(since|over|for)\s+\d{2,4}\s*(yrs?|years?)\b.{0,40}\b(family|keep|keeping|hold|holding)\b", re.I),
    re.compile(r"\b(keep|keeping|hold|holding)\s+(it|the\s+(house|home|property)).{0,30}\b(family)\b", re.I),
]

_MAYBE_LATER = [
    # ── Core "maybe / later" signals ──────────────────────────────────────────
    re.compile(r"\bmaybe\s+(later|in\s+a\s+few|in\s+the\s+future|next\s+year|sometime)\b", re.I),
    re.compile(r"\bpossibly\s+(later|soon|in\s+the\s+future|down\s+the\s+road)\b", re.I),
    re.compile(r"\bnear\s+future\b", re.I),
    re.compile(r"\bdown\s+the\s+road\b", re.I),
    re.compile(r"\bin\s+the\s+future\b", re.I),
    re.compile(r"\bsome\s+(other\s+)?time\b", re.I),
    re.compile(r"\bnot\s+right\s+now\b", re.I),
    re.compile(r"\bnot\s+yet\b", re.I),

    # ── "Check back" / "Try again" / "Reach out later" ─────────────────────────
    re.compile(r"\bcheck\s+back\b", re.I),                            # "check back at end of year"
    re.compile(r"\btry\s+(again|back)\b.{0,40}\b(year|month|later|future)\b", re.I),
    re.compile(r"\breach\s+out\b.{0,30}\b(later|again|future|year|month)\b", re.I),
    re.compile(r"\bcontact\s+(me|us)\b.{0,30}\b(later|again|future|year|month)\b", re.I),
    re.compile(r"\bcall\s+(me|us)\b.{0,30}\b(later|again|future|year|month)\b", re.I),
    re.compile(r"\btext\s+(me|us)\b.{0,30}\b(later|again|future|year|month)\b", re.I),
    re.compile(r"\bgive\s+(me|us)\s+a\s+(call|text|ring)\b.{0,30}\b(later|again|year|month)\b", re.I),
    re.compile(r"\b(hit|reach)\s+me\s+(up|back)\b.{0,30}\b(later|year|month)\b", re.I),

    # ── Specific future time references ────────────────────────────────────────
    re.compile(r"\b(end|beginning|start|first)\s+of\s+the\s+(year|month|quarter|summer|fall|spring|winter)\b", re.I),
    re.compile(r"\b(end|beginning|start)\s+of\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I),
    re.compile(r"\bnext\s+(year|month|spring|summer|fall|winter|january|february|march|april|may|june|july|august|september|october|november|december)\b", re.I),
    re.compile(r"\bin\s+(a\s+)?(couple|few|several)\s+(of\s+)?(months?|weeks?|years?)\b", re.I),
    re.compile(r"\bin\s+\d+\s+(months?|weeks?|years?)\b", re.I),     # "in 3 months", "in 6 months"
    re.compile(r"\b(3|6|12|two|three|six|twelve)\s+(months?|years?)\s+(from\s+now|later)\b", re.I),
    re.compile(r"\bafter\s+the\s+(holiday|holidays|summer|winter|season|new\s+year)\b", re.I),
    re.compile(r"\bwhen\s+(things|the\s+market|i|we)\s+(settle|calm|stabilize|improve|change|are\s+ready)\b", re.I),

    # ── Soft "not now but open" signals ────────────────────────────────────────
    re.compile(r"\bnot\s+at\s+this\s+time\b.{0,60}\b(check\s+back|later|year|month|future|try\s+again)\b", re.I),
    re.compile(r"\b(check\s+back|try\s+again|reach\s+out).{0,60}\bnot\s+at\s+this\s+time\b", re.I),
    re.compile(r"\bnot\s+now\b.{0,40}\b(later|year|month|future|check\s+back)\b", re.I),
    re.compile(r"\b(later|future|year|month|check\s+back).{0,40}\bnot\s+now\b", re.I),
    re.compile(r"\bmaybe\s+(in\s+)?(the\s+)?future\b", re.I),
    re.compile(r"\bopen\s+to\s+(it\s+)?(in\s+the\s+future|later|down\s+the\s+road)\b", re.I),
    re.compile(r"\bwilling\s+to\s+(reconsider|consider\s+it)\s+(later|in\s+the\s+future|next\s+year)\b", re.I),
    re.compile(r"\bperhaps\s+(later|in\s+the\s+future|next\s+year|in\s+a\s+few)\b", re.I),
]

# Explicit "check back" invitation — contact is saying "contact me again later".
# When this fires alongside a NI pattern, Maybe Later wins.
_FUTURE_CALLBACK = re.compile(
    r"\b("
    r"check\s+back|try\s+again|reach\s+out.{0,20}(later|again|year|month)"
    r"|contact.{0,10}(later|again|year|month)"
    r"|call.{0,10}(later|again|year|month)"
    r"|text.{0,10}(later|again|year|month)"
    r"|hit.{0,5}(me|us).{0,5}(up|back).{0,20}(later|year|month)"
    r"|end\s+of\s+the\s+(year|month)"
    r"|first\s+of\s+the\s+(year|month)"
    r"|beginning\s+of\s+the\s+(year|month)"
    r"|next\s+(year|month|spring|summer|fall|winter)"
    r"|in\s+\d+\s+(months?|weeks?|years?)"
    r"|in\s+(a\s+)?(few|couple)\s+(months?|years?)"
    r")\b",
    re.I,
)

_AI_REQUIRED_LABELS = {
    # FU drip labels — now validated by detect_fu_track() in Phase 2.
    # Kept here as reference only; label_requires_ai() exempts them.
    "wl drip",
    "ap drip",
    "hl drip",
    # Push/Lead labels — now validated locally in Phase 3 by validate_push_label()
    "waiting to be pushed",
    "pushed to client",
    "lead",
    "fu1",
    "fu2",
    "fu3",
    "fui",
    # Complex engagement labels — T3 confuses these, must use Groq
    # Phase 4: ABV MV and Bluffer are now validated locally
    "investor",
    "scenario g",
}

# FU drip labels validated locally by detect_fu_track() — do NOT escalate to AI.
_LOCAL_FU_LABELS = {"wl drip", "ap drip", "hl drip", "reason fu"}

# Push/Lead labels validated locally by validate_push_label() — do NOT escalate to AI.
_LOCAL_PUSH_LABELS = {"waiting to be pushed", "pushed to client", "lead"}

_CALL_ME_RE = re.compile(
    r"\b(call\s+me|give\s+me\s+a\s+call|call\s+(us|my\s+husband|my\s+wife)"
    r"|reach\s+me\s+at\b.{0,20}\d"
    r"|schedule\s+a\s+call|we\s+can\s+talk)\b",
    re.I,
)

# Phase 4: ABV MV / Bluffer labels validated locally by detect_abv_mv_response()
_LOCAL_ABV_LABELS = {"abv mv", "above market value", "bluffer"}


def _body(message: dict) -> str:
    return (message.get("body") or message.get("message") or "").strip()


def _sender(message: dict) -> str:
    return (message.get("sender") or "").lower()


def _norm(label: str | None) -> str:
    return re.sub(r"\s+", " ", (label or "").strip()).lower()


def _is_bluffer_label(label: str | None) -> bool:
    """Return True if the assigned label is the 'Bluffer' label (any casing)."""
    return _norm(label) == "bluffer"


def _label_key(label: str | None) -> str:
    normalized = _norm(label)
    if normalized in {"dnc", "do not call", "do not call (dnc)"}:
        return "do not call"
    # These all mean the same thing — treat as equivalent for correctness checks
    if normalized in {"not interested", "verified", "decision maker",
                      "verified, not interested", "not interested, verified",
                      "decision maker, not interested", "not interested, decision maker",
                      "decision maker not interested"}:
        return "not interested"
    # Compound labels containing "not interested" as a component
    parts = [p.strip() for p in re.split(r"[,;/|+]", normalized) if p.strip()]
    if "not interested" in parts:
        return "not interested"
    # "Missed Call", "Undefined", "Stop Responding" are all "Stopped Responding"
    if normalized in {"missed call", "undefined", "stop responding",
                      "stopped responding", "fu3", "listed"}:
        return "stopped responding"
    return normalized


def label_requires_ai(assigned_labels: list[str] | None) -> tuple[bool, str | None]:
    """Labels whose correctness depends on timing/campaign context must use AI.

    Phase 2: FU drip labels (wl drip / ap drip / hl drip / reason fu) are
    now validated locally by detect_fu_track() and do NOT require AI.
    """
    for raw_label in assigned_labels or []:
        label = _norm(raw_label)
        parts = [part.strip() for part in re.split(r"[,;/|+]", label) if part.strip()]
        candidates = {label, *parts}

        for candidate in candidates:
            # Phase 2, 3, 4: FU, Push, and ABV labels are handled locally
            if candidate in _LOCAL_FU_LABELS or candidate in _LOCAL_PUSH_LABELS or candidate in _LOCAL_ABV_LABELS:
                continue
            if candidate in _AI_REQUIRED_LABELS:
                return True, raw_label
            if re.fullmatch(r"fu\s*[123]", candidate):
                return True, raw_label

    return False, None


_POSITIVE_ENGAGEMENT = [
    re.compile(r"\byes\b.{0,20}\b(absolutely|definitely|sure|of\s+course|please|okay|ok)\b", re.I),
    # "absolutely"/"definitely" only when NOT followed by "not" — "Definitely not" is still NI
    re.compile(r"\b(absolutely|definitely)(?!\s+not\b)(\s|$)", re.I),
    re.compile(r"\byes\s+please\b|\byes\s+i\s+(am|do|would|want)\b", re.I),
    re.compile(r"\binterested\s+in\s+(two|2|three|3|multiple|several)\s+propert", re.I),
    re.compile(r"\bwe\s+can\s+chat\b", re.I),
    re.compile(r"\bhow\s+(does|do)\s+your\s+process\s+work\b", re.I),
    re.compile(r"\bwhat\s+(company|is\s+your\s+process|are\s+you\s+offering)\b", re.I),

    # ── Potential reversal: contact asks agent for their offer/price ──────────
    # "How much do you want to pay?" / "What would you offer?" after saying No
    # = contact is open to hearing a number → Potential, NOT Not Interested
    re.compile(r"\bhow\s*[?.!,]*\s*much\s+(do|would|will|can)\s+you\s+(want|pay|offer|give)\b", re.I),
    re.compile(r"\bwhat\s+(would|do|will|can)\s+you\s+(pay|offer|give)\b", re.I),
    re.compile(r"\bwhat'?s?\s+(your|the)\s+offer\b", re.I),
    re.compile(r"\bwhat\s+are\s+you\s+(willing|able)\s+to\s+pay\b", re.I),
    re.compile(r"\bmake\s+(me\s+)?an?\s+offer\b", re.I),
    re.compile(r"\bwhat\s+(number|price|amount)\s+(are\s+you|do\s+you)\b", re.I),
    re.compile(r"\bwhat\s+are\s+you\s+offering\b", re.I),
    re.compile(r"\bhow\s*[?.!,]*\s*much\s+(are\s+you|you)\s+(offering|paying|thinking)\b", re.I),
    re.compile(r"\bwhat\s+do\s+you\s+(have\s+in\s+mind|think\s+it'?s?\s+worth)\b", re.I),
    # "I don't know… how much do you want to pay" — split across a short message
    re.compile(r"\bhow\s*[?.!,]*\s*much.{0,30}\bwant\s+to\s+pay\b", re.I),
    # Contact asks anything about agent's offer after initial No
    re.compile(r"\bwhat.{0,20}\b(offer|pay|buying\s+for|purchase\s+price)\b", re.I),
    # "Much do you want to pay" (no 'how')
    re.compile(r"\bmuch\s+(do|would|will|can)\s+you\s+(want|pay|offer|give)\b", re.I),
]


def _contact_reversed_to_interested(messages: list[dict]) -> bool:
    """True if contact initially declined but later showed genuine interest."""
    contact_msgs = [_body(m) for m in messages if _sender(m) == "contact" and _body(m)]
    if len(contact_msgs) < 2:
        return False
    later_msgs = "\n".join(contact_msgs[1:])
    return any(p.search(later_msgs) for p in _POSITIVE_ENGAGEMENT)


_STOPPED_RESPONDING_LABELS = {
    "stopped responding", "stop responding", "missed call",
    "undefined", "listed",
}


def _expected_label(contact_text: str, messages: list[dict], assigned_label: str | None = None) -> tuple[str | None, str]:
    """Return the ML's expected label and reason for the conversation.

    PRIORITY ORDER (highest wins):
        1. Do Not Call  — contact used any opt-out language (ABSOLUTE PRIORITY over all other labels)
        2. Wrong Number — contact said wrong person/number (only if no DNC)
        3. Sold         — property already sold (only if no DNC)
        4. Bluffer      — contact quoted $1M+ price (only if no DNC)
        5. Potential    — contact reversed to interested after initial refusal
        6. Not Interested / Maybe Later / Stopped Responding

    DNC wins over EVERYTHING. If a contact says "wrong number please stop texting",
    the correct label is Do Not Call, not Wrong Number.
    """
    assigned_key = _label_key(assigned_label)

    # Detect all signals upfront
    has_wn  = any(p.search(contact_text) for p in _WRONG_NUMBER)
    has_dnc = any(p.search(contact_text) for p in _DNC)
    has_ni  = any(p.search(contact_text) for p in _NOT_INTERESTED)

    # Sold: only fire if "sold" refers to the subject property, not a neighbor's/adjacent sale
    _sold_raw      = any(p.search(contact_text) for p in _SOLD)
    _sold_neighbor = any(p.search(contact_text) for p in _SOLD_NEIGHBOR_CONTEXT)
    has_sold = _sold_raw and not _sold_neighbor

    # ── PRIORITY 1: DNC — beats EVERYTHING (WN, Sold, NI, Bluffer, Maybe Later) ──
    # If contact opted out in any way, the actionable label is always Do Not Call.
    if has_dnc:
        return "Do Not Call", (
            "ML detected opt-out language. DNC takes priority over all other labels "
            "(Wrong Number, Sold, Not Interested, Bluffer, etc.)."
        )

    # ── PRIORITY 2: Wrong Number (only if no DNC) ──────────────────────────────
    if has_wn:
        return "Wrong Number", "ML detected wrong-number language."

    # ── PRIORITY 3: Sold (only if no DNC) ──────────────────────────────────────
    if has_sold:
        return "Sold", "ML detected sold-property language."

    # ── PRIORITY 4: Bluffer / million-dollar price (only if no DNC) ─────────────
    # Contact quoting $1M+ is a classic bluffing deflection ("Price Bluffer" type).
    # This is NOT an opt-out signal by itself; opt-outs are handled above via DNC.
    if _contact_stated_million_plus(messages):
        if _is_bluffer_label(assigned_label):
            return "Bluffer", (
                "ML detected inflated price (>= $1M) — correctly labeled Bluffer: "
                "contact quoted an unrealistic price to deter the buyer."
            )
        # DNC and Abv MV are also accepted team conventions for this case
        _ak = _label_key(assigned_label)
        if _ak == "do not call" or _norm(assigned_label) in {"abv mv", "above market value"}:
            return assigned_label, (
                "ML detected inflated price (>= $1M) — "
                f"'{assigned_label}' is an accepted team label for this scenario."
            )
        return "Bluffer", (
            "ML detected inflated price (>= $1M) — "
            "contact quoted an unrealistic price to deter the buyer (Bluffer)."
        )

    # Guard: if agent labeled WN but contact text only has NI phrases (no explicit WN phrase),
    # the contact may have said "No" to "Are you the owner?" — ambiguous without agent context.
    # Don't override a WN label to NI on regex alone; defer to Groq.
    if assigned_key == "wrong number" and has_ni and not has_wn:
        return None, ""

    # Guard: if agent labeled DNC but no DNC pattern matched, don't override to NI.
    # Contact may have said "No more texts" in a way not yet covered, or context requires Groq.
    if assigned_key == "do not call" and has_ni and not has_dnc:
        return None, ""

    # Guard: if agent labeled DNC but Sold fired on neighbor context (not own property),
    # the contact is negotiating — don't override DNC/NI with a false Sold signal.
    if assigned_key == "do not call" and _sold_raw and _sold_neighbor and not has_dnc:
        return None, ""

    # Stopped Responding / Missed Call / Undefined / Listed:
    # correct if contact has no messages OR contact sent only very short ambiguous responses
    if assigned_key == "stopped responding":
        contact_messages = [m for m in messages if _sender(m) == "contact" and _body(m)]
        has_missed_call = any(
            (_sender(m) or "").lower() == "system" and "missed call" in _body(m).lower()
            for m in messages
        )
        # No contact reply at all → Stopped Responding is correct
        if not contact_messages:
            return "Stopped Responding", "ML detected no contact response."
        # Has missed call event → Missed Call label is correct
        if has_missed_call:
            return "Missed Call", "ML detected missed call system event."
        # Contact replied but with only noise (single char, emoji, ?) → ambiguous, let Groq decide
        non_trivial = [m for m in contact_messages
                       if len(_body(m).strip().replace("?", "").strip()) > 2]
        if not non_trivial:
            return "Stopped Responding", "ML: contact reply was trivial/unclear."
        return None, ""

    # ── PRIORITY 5: Potential — contact reversed to interested ──────────────────
    if _contact_reversed_to_interested(messages):
        return "Potential", "ML detected contact reversal (initial disinterest followed by interest/inquiry)."

    # ── PRIORITY 6: Not Interested / Maybe Later ─────────────────────────────────
    # KEY RULE: if contact said "not at this time" / "not now" BUT ALSO gave a future
    # callback signal ("check back at end of year", "try again in a few months"),
    # Maybe Later WINS over Not Interested — the contact is inviting future contact.
    has_future_callback = _FUTURE_CALLBACK.search(contact_text) is not None
    if any(p.search(contact_text) for p in _MAYBE_LATER):
        return "Maybe Later", "ML detected future/later timing."
    if has_future_callback:
        return "Maybe Later", "ML detected explicit future callback invitation (contact said check back later)."
    if any(p.search(contact_text) for p in _NOT_INTERESTED):
        return "Not Interested", "ML detected disinterest."
    if any(p.search(contact_text) for p in _MAYBE_LATER):
        return "Maybe Later", "ML detected future/later timing."

    contact_messages = [m for m in messages if _sender(m) == "contact" and _body(m)]
    if not contact_messages:
        return "Stopped Responding", "ML detected no contact response."

    return None, ""



def validate_push_label(messages: list[dict], assigned_label: str, funnel_tier: str) -> dict:
    """
    Validate Lead / Pushed to Client labels against funnel thresholds.

    WF (Wide Funnel)   → 0 pillars needed
    MF (Middle Funnel) → 2 pillars needed
    NF (Narrow Funnel) → 3 pillars needed

    Also handles the 'Call Me' override: if contact explicitly asks for a call,
    they can be pushed regardless of the pillar count.
    """
    contact_bodies = [_body(m) for m in messages if _sender(m) == "contact"]
    contact_text = "\n".join(contact_bodies)

    # 1. Did they ask to be called? (Overrides pillar requirements)
    if _CALL_ME_RE.search(contact_text):
        return {
            "label_correct": True,
            "label_should_be": assigned_label,
            "label_reason": f"ML detected 'Call Me' override. Contact requested a call, satisfying {funnel_tier} push requirements.",
        }

    # 2. Count distinct pillars
    collected_pillars = set()
    for pillar_name, pattern in _PILLAR_PATTERNS.items():
        if pattern.search(contact_text):
            collected_pillars.add(pillar_name)

    required = _PILLAR_THRESHOLD.get(funnel_tier, 3)
    has_count = len(collected_pillars)

    if has_count >= required:
        reason = (
            f"ML detected {has_count}/{required} required pillars for {funnel_tier} funnel "
            f"({', '.join(collected_pillars)}). Push label is valid."
        ) if required > 0 else f"ML detected Wide Funnel (WF) — 0 pillars required. Push label is valid."
        return {
            "label_correct": True,
            "label_should_be": assigned_label,
            "label_reason": reason,
        }
    else:
        reason = (
            f"ML detected only {has_count}/{required} required pillars for {funnel_tier} funnel "
            f"({' '.join(collected_pillars) if collected_pillars else 'none'}). Push label is INVALID (premature)."
        )
        return {
            "label_correct": False,
            # If they didn't meet the threshold, they shouldn't be pushed.
            # We defer the actual expected label to Groq (or fallback to None),
            # but we explicitly mark the push label as incorrect.
            "label_should_be": None,
            "label_reason": reason,
        }


def validate_label(messages: list[dict] | None, assigned_label: str | None, funnel_tier: str = "NF") -> dict:
    """Return label_correct/label_should_be/label_reason for simple ML-safe cases.

    Phase 2: FU drip labels are validated locally via detect_fu_track().
    Phase 3: Push/Lead labels are validated locally via validate_push_label()
             using funnel_tier pillar thresholds.
    """
    assigned = (assigned_label or "").strip()
    if not messages or not assigned:
        return {"label_correct": None, "label_should_be": None, "label_reason": ""}

    norm_assigned = _norm(assigned)

    # Phase 4: handle ABV MV / Bluffer labels locally using price detection
    if norm_assigned in _LOCAL_ABV_LABELS:
        abv = detect_abv_mv_response(messages)
        # If contact stated a price
        if abv["contact_stated_price"]:
            price = abv["price_amount"]
            if abv["agent_did_referral_close"]:
                # Agent correctly handled the above-market price
                return {
                    "label_correct": True,
                    "label_should_be": assigned,
                    "label_reason": (
                        f"ML detected above-market price (${price:,.0f}). "
                        "Agent correctly used referral close per script."
                    ),
                }
            elif abv["agent_kept_pushing"]:
                # Agent violated script by pushing after high price
                return {
                    "label_correct": True,  # label is correct (ABV MV), but agent behavior is flagged
                    "label_should_be": assigned,
                    "label_reason": (
                        f"ML detected above-market price (${price:,.0f}). "
                        "Label is correct but agent kept pushing instead of referral close (FLAG 15)."
                    ),
                }
            else:
                # Agent neither closed nor pushed — could be correct if they just stopped
                return {
                    "label_correct": True,
                    "label_should_be": assigned,
                    "label_reason": (
                        f"ML detected above-market price (${price:,.0f}). "
                        f"'{assigned}' label is valid for this scenario."
                    ),
                }
        else:
            # No price detected — can't validate ABV MV locally, defer
            return {"label_correct": None, "label_should_be": None, "label_reason": ""}

    # Phase 3: handle Push/Lead labels locally using funnel thresholds
    if norm_assigned in _LOCAL_PUSH_LABELS:
        push_result = validate_push_label(messages, assigned, funnel_tier)
        return {
            "label_correct": push_result["label_correct"],
            "label_should_be": push_result["label_should_be"],
            "label_reason": push_result["label_reason"],
        }

    # Phase 2: handle FU drip labels locally
    if norm_assigned in _LOCAL_FU_LABELS:
        fu_result = validate_fu_label(messages, assigned)
        return {
            "label_correct": fu_result["label_correct"],
            "label_should_be": fu_result["label_should_be"],
            "label_reason": fu_result["label_reason"],
        }

    contact_text = "\n".join(_body(m) for m in messages if _sender(m) == "contact")
    expected, reason = _expected_label(contact_text, messages, assigned_label=assigned)
    if not expected:
        return {"label_correct": None, "label_should_be": None, "label_reason": ""}

    return {
        "label_correct": _label_key(assigned) == _label_key(expected),
        "label_should_be": expected,
        "label_reason": reason,
    }
