"""
Shared guard helpers extracted from analyzer.py for reuse by Tier 4.

These are deterministic pattern-matching functions that enforce
compliance rules without any ML or API calls.

Imported by both analyzer.py (post-processing) and tier4_flag_generator.py.
"""
from __future__ import annotations

import re


# ── The 15 whitelisted flag strings ──────────────────────────────────────────
# T4, normalize_red_flags, and analyzer all use this exact list.
WHITELIST_FLAG_OUTPUTS = [
    "Continued texting after explicit opt-out.",
    "Used threatening, profane, or deceptive language.",
    "Stated a specific dollar offer.",
    "Gave up after first no with zero rebuttal.",
    "Continued original pitch after wrong number.",
    "Agreed to call without pre-qualifying.",
    "Revealed or promised 6+ month timeline.",
    "Sent incoherent message or wrong name.",
    "Ended conversation after lead showed interest.",
    "Pushed to close with zero property info.",
    "Did not escalate after all 4 pillars gathered.",
    "Skipped $1k referral close after high price.",
    "Agent re-asked for asking price after owner already stated it.",
    # FLAG 15 — added Phase 4 (above-market price handling)
    "Agent kept pushing after above-market price instead of referral close.",
    # FLAG 14 — address denial
    "Contact denied knowing the address after providing property details. Agent should have asked clarifying questions (parcel number, correct address) instead of closing the conversation. Label should be Potential or Undefined, not Bluffer.",
]

# ── Regex constants ──────────────────────────────────────────────────────────

OPTOUT_TEXT_RE = re.compile(
    r"\b(stop\s+texting|stop\s+messaging|stop\s+contacting|stop\s+calling"
    r"|stop\s+bothering\s+me|stop\s+these\s+texts|remove\s+me|unsubscribe"
    r"|leave\s+me\s+alone|don't\s+contact\s+me|take\s+me\s+off\s+your\s+list"
    r"|no\s+more\s+text|dont\s+text\s+me|don't\s+text\s+me"
    r"|if\s+you\s+could\s+stop|please\s+stop)\b"
    r"|^stop[.!]*$",
    re.I | re.MULTILINE,
)

SOFT_NO_RE = re.compile(
    r"\b(no|nope|not interested|no thanks|not for sale)\b", re.I
)

DNC_LABEL_RE = re.compile(r"\b(do\s*not\s*call|dnc)\b", re.I)

DNC_JOKE_PRICE_RE = re.compile(
    r"(\$\s?(?:9{5,}|\d{1,3}(?:,\d{3}){2,})\b|\b(?:million|billion)\s+dollars?\b"
    r"|\bmake\s+me\s+rich\b)",
    re.I,
)

# Agent-side dollar offer pattern (firm/specific offers — NOT template ranges)
DOLLAR_OFFER_RE = re.compile(
    r"\b(my\s+offer\s+is|i('|')?ll\s+offer\s+you|offering\s+you|"
    r"we('|')?ll\s+pay\s+you|i\s+can\s+do)\s*\$?\s*\d",
    re.I,
)

# Profanity / threatening language
PROFANITY_RE = re.compile(
    r"\b(fuck|shit|damn|hell|stfu|wtf|idiot|stupid|suck|"
    r"piss\s+off|go\s+to\s+hell|shut\s+up)\b",
    re.I,
)

# Timeline reveal (6+ months)
TIMELINE_RE = re.compile(
    r"\b(6\s*\+?\s*months?|six\s+months?|half\s+a?\s*year)\b", re.I
)

# Wrong number continued pitch
WRONG_NUMBER_RE = re.compile(
    r"\b(wrong\s+(number|person)|not\s+my\s+(house|property|number))\b", re.I
)

# Referral close pattern
REFERRAL_RE = re.compile(r"\breferral\b", re.I)
DOLLAR_RE = re.compile(r"\$[\d]", re.I)

# Pillar flag pattern
PILLAR_FLAG_RE = re.compile(
    r"did(?:n'?t| not) gather\s+([a-z _-]+?)\s+pillar", re.I
)

# Flag remap rules (common paraphrases → whitelist)
FLAG_REMAP_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bgave\s*up\b.*\bfirst\b.*\bno\b"),
     "Gave up after first no with zero rebuttal."),
    (re.compile(r"(?i)\bgave\s*up\b.*\bzero\b.*\brebuttal\b"),
     "Gave up after first no with zero rebuttal."),
    (re.compile(r"(?i)\bwrong\s*number\b.*\bkept\b.*\b(sell|selling|pitch|pushing)\b"),
     "Continued original pitch after wrong number."),
    (re.compile(r"(?i)\breveal(ed|ing)?\b.*\b6\b.*\bmonth"),
     "Revealed or promised 6+ month timeline."),
    (re.compile(r"(?i)\bpromis(ed|ing)?\b.*\b6\b.*\bmonth"),
     "Revealed or promised 6+ month timeline."),
    (re.compile(r"(?i)\bcontinued\b.*\b(opt\s*[- ]?out|unsubscribe|stop\s+text|remove\s+me|leave\s+me\s+alone)\b"),
     "Continued texting after explicit opt-out."),
    # FLAG 15 remap — dynamic text with price amount is normalised to the fixed canonical string
    (re.compile(r"(?i)\bkept\s+pushing\b.{0,40}\babove.?market\b"),
     "Agent kept pushing after above-market price instead of referral close."),
    (re.compile(r"(?i)\babove.?market\b.{0,40}\breferral\s+close\b"),
     "Agent kept pushing after above-market price instead of referral close."),
]


def _canon_flag_text(text: str) -> str:
    """Normalize flag text for comparison."""
    t = text.lower().strip()
    t = t.replace('"', "").replace("'", "")
    t = re.sub(r"[^\w\s$+.-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".")
    return t


def canon_flag_text(text: str) -> str:
    """Public alias for flag-text canonicalization — lets the deterministic T4
    tier and the dream worker share one normalization when comparing flag
    strings (learned-rule suppression matching)."""
    return _canon_flag_text(text)


_WHITELIST_CANON = {_canon_flag_text(x): x for x in WHITELIST_FLAG_OUTPUTS}


def normalize_red_flags(flags) -> list[str]:
    """Normalize + strictly enforce whitelist flag outputs."""
    if not flags or not isinstance(flags, list):
        return []
    pillars: list[str] = []
    kept: list[str] = []
    for f in flags:
        if not isinstance(f, str):
            continue
        if REFERRAL_RE.search(f) and DOLLAR_RE.search(f):
            # FLAG 15 legitimately mentions both referral and $ — don't suppress it
            if "kept pushing" not in f.lower():
                continue
        m = PILLAR_FLAG_RE.search(f)
        if m:
            pillars.append(m.group(1).strip().lower())
        else:
            kept.append(f.strip())
    if pillars:
        seen, ordered = set(), []
        for p in pillars:
            if p not in seen:
                seen.add(p)
                ordered.append(p)
        if len(ordered) >= 2:
            kept.append(f"Missed pillars: {', '.join(ordered)}.")
    seen_keys, out = set(), []
    for f in kept:
        remapped = _remap_flag_to_whitelist(f)
        key = _canon_flag_text(remapped or f)
        canonical = _WHITELIST_CANON.get(key)
        if not canonical:
            continue
        key = _canon_flag_text(canonical)
        if key and key not in seen_keys:
            seen_keys.add(key)
            out.append(canonical)
    return out


def _remap_flag_to_whitelist(text: str) -> str | None:
    """Map common paraphrases to exact whitelist OUTPUT lines."""
    for pattern, output in FLAG_REMAP_RULES:
        if pattern.search(text):
            return output
    return None


def agent_continued_after_opt_out(messages: list[dict]) -> bool:
    """
    Deterministic guard for FLAG 1.
    Returns True only if a contact message has explicit opt-out language
    AND there's a later non-contact message after that opt-out.
    """
    optout_idx: int | None = None
    for i, m in enumerate(messages or []):
        sender = (m.get("sender") or "").strip().lower()
        text = (m.get("message") or m.get("body") or "").strip()
        if sender in ("contact", "lead") and OPTOUT_TEXT_RE.search(text):
            optout_idx = i
            break
    if optout_idx is None:
        return False
    for later in (messages or [])[optout_idx + 1:]:
        sender = (later.get("sender") or "").strip().lower()
        if sender and sender not in ("contact", "lead"):
            return True
    return False


def last_message_from_contact(messages: list[dict]) -> bool:
    """True when the final message in sequence is from the contact/lead."""
    if not messages:
        return False
    last_sender = (messages[-1].get("sender") or "").strip().lower()
    return last_sender in ("contact", "lead")


def agent_replied_after_first_soft_no(messages: list[dict]) -> bool:
    """
    Deterministic guard for FLAG 4.
    Returns True only if a contact message has a soft refusal
    AND there's a later non-contact message after that refusal.
    """
    no_idx: int | None = None
    for i, m in enumerate(messages or []):
        sender = (m.get("sender") or "").strip().lower()
        text = (m.get("message") or m.get("body") or "").strip()
        if sender in ("contact", "lead") and SOFT_NO_RE.search(text):
            no_idx = i
            break
    if no_idx is None:
        return False
    for later in (messages or [])[no_idx + 1:]:
        sender = (later.get("sender") or "").strip().lower()
        if sender and sender not in ("contact", "lead"):
            return True
    return False


def contact_has_explicit_opt_out(messages: list[dict]) -> bool:
    """True when any contact/lead message contains explicit opt-out wording."""
    for m in messages or []:
        sender = (m.get("sender") or "").strip().lower()
        text = (m.get("message") or m.get("body") or "").strip()
        if sender in ("contact", "lead") and OPTOUT_TEXT_RE.search(text):
            return True
    return False


def contact_has_dnc_joke_price(messages: list[dict]) -> bool:
    """True only for absurd/joke prices that are treated like DNC exits."""
    for m in messages or []:
        sender = (m.get("sender") or "").strip().lower()
        text = (m.get("message") or m.get("body") or "").strip()
        if sender in ("contact", "lead") and DNC_JOKE_PRICE_RE.search(text):
            return True
    return False


def agent_continued_pitch_after_wn(messages: list[dict]) -> bool:
    """
    Deterministic guard for FLAG 5.
    Returns True if agent continued their original pitch after contact said wrong number.
    Returns False if they apologized or pivoted to a referral close.
    """
    wn_idx = None
    for i, m in enumerate(messages or []):
        sender = (m.get("sender") or "").strip().lower()
        body = (m.get("message") or m.get("body") or "").strip()
        if sender in ("contact", "lead") and WRONG_NUMBER_RE.search(body):
            wn_idx = i
            break
    
    if wn_idx is None:
        return False

    # If the contact re-engaged after the wrong-number message (referral,
    # question, address, property details), the agent is EXPECTED to switch
    # into funnel mode — later pitch-like messages are correct, not a
    # continued-pitch violation.
    if contact_reengaged_after_wn(messages, wn_idx):
        return False

    for later in (messages or [])[wn_idx + 1:]:
        sender = (later.get("sender") or "").strip().lower()
        body = (later.get("message") or later.get("body") or "").strip().lower()
        if sender and sender not in ("contact", "lead"):
            # A pitch keywords: sell, offer, property, home, house, price, cash
            # BUT MUST NOT have referral keywords: referral, someone, know, bring to us
            pitch_keywords = ["sell", "offer", "property", "home", "house", "price", "cash"]
            has_pitch = any(w in body for w in pitch_keywords)
            has_referral = REFERRAL_RE.search(body) or "someone" in body or "know" in body
            
            if has_pitch and not has_referral:
                return True
    return False


def apply_label_guards(result: dict, messages: list[dict]) -> None:
    """
    Deterministic label guard:
    Explicit opt-out by contact => correct label must be DO Not Call.
    No explicit opt-out/joke price => AI cannot force DO Not Call.
    """
    if not isinstance(result, dict):
        return
    has_opt_out = contact_has_explicit_opt_out(messages)
    has_joke_price = contact_has_dnc_joke_price(messages)
    assigned = str(result.get("label_assigned") or "").strip()
    should_be = str(result.get("label_should_be") or "").strip()

    # 1. No signal -> AI cannot force DNC
    if not has_opt_out and not has_joke_price:
        if result.get("label_correct") is False and DNC_LABEL_RE.search(should_be):
            result["label_correct"] = True
            result["label_should_be"] = assigned or should_be
            result["label_reason"] = (
                "No explicit opt-out or joke price appeared, so DO Not Call is not forced."
            )
            flags = list(result.get("red_flags") or [])
            result["red_flags"] = [
                f for f in flags
                if "wrong label" not in str(f).lower() or "do not call" not in str(f).lower()
            ]
        return

    # 2. Handle signals
    assigned_lower = assigned.lower()
    assigned_is_dnc = bool(DNC_LABEL_RE.search(assigned))
    assigned_is_bluffer = "bluffer" in assigned_lower

    if has_opt_out:
        # Explicit opt-out (STOP, etc) REQUIRES DNC label
        if assigned_is_dnc:
            result["label_correct"] = True
            result["label_should_be"] = assigned or "DO Not Call"
            result["label_reason"] = "Contact used explicit opt-out language; assigned label is in DNC group."
        else:
            result["label_correct"] = False
            result["label_should_be"] = "DO Not Call"
            result["label_reason"] = "Contact used explicit opt-out language, so the correct label is DO Not Call."
        return

    if has_joke_price:
        # Joke price (1 million, etc) ACCEPTED as DNC or Bluffer
        if assigned_is_dnc or assigned_is_bluffer:
            result["label_correct"] = True
            result["label_should_be"] = assigned
            result["label_reason"] = f"Contact used joke/inflated price; '{assigned}' is an accepted label."
        else:
            result["label_correct"] = False
            result["label_should_be"] = "Bluffer"
            result["label_reason"] = "Contact used joke/inflated price, so the correct label is Bluffer or DO Not Call."
        return


# ── Full-Convo Reversal Guard ────────────────────────────────────────────────
# Detects when a contact re-engages AFTER a negative signal (opt-out, hostility,
# NI, etc.). If the contact reversed, the pipeline should NOT short-circuit —
# the full conversation arc matters more than any single phrase.

_REVERSAL_ENGAGEMENT_RE = re.compile(
    r"\b("
    # ── Price inquiry: contact is asking about agent's offer ──────────────────
    r"how\s*[?.!,]*\s*much\s+(do|would|will|can|are)\s+you\s+(want|pay|offer|give|thinking)"
    r"|much\s+(do|would|will|can)\s+you\s+(want|pay|offer|give)"
    r"|what\s+(would|do|will|can)\s+you\s+(pay|offer|give)"
    r"|what.{0,20}\b(offer|buying\s+for|purchase\s+price)"
    r"|what'?s?\s+(your|the)\s+offer"
    r"|make\s+(me\s+)?an?\s+offer"
    r"|what\s+are\s+you\s+(willing|able|offering)(\s+to\s+pay)?"
    r"|what\s+kind\s+of\s+offer"
    r"|depends\s+on\s+the\s+price"
    r"|what\s+do\s+you\s+(have\s+in\s+mind|think\s+it'?s?\s+worth)"
    # ── Call request: contact wants to talk ───────────────────────────────────
    r"|call\s+me"
    r"|give\s+me\s+a\s+call"
    r"|reach\s+me\s+at"
    r"|schedule\s+a\s+call"
    r"|we\s+can\s+talk"
    r"|let'?s\s+talk"
    # ── Direct interest: contact is explicitly engaged ────────────────────────
    r"|yes\s+(please|i\s+(am|do|would|want|can))"
    r"|i'?m\s+(interested|open\s+to|willing)"
    r"|tell\s+me\s+more"
    r"|send\s+me\s+(info|details|the\s+offer)"
    r"|how\s+does\s+(your|the)\s+process\s+work"
    r"|what\s+(company|is\s+your\s+process)"
    r"|interested\s+in\s+(two|2|three|3|multiple|several)\s+propert"
    r"|we\s+can\s+chat"
    # ── Property detail sharing: contact discussing their property ────────────
    r"|bedroom|bathroom|bath|kitchen|garage|pool|basement|attic"
    r"|sqft|sq\s*ft|square\s+feet|acre"
    r"|roof|foundation|floor(ing)?|hvac"
    r"|renovated|remodel|new\s+(roof|floor|kitchen|bath)"
    r"|great\s+condition|good\s+condition|needs?\s+(work|repair|update)"
    r"|fixer|move.?in\s+ready"
    # ── Timeline discussion: contact engaging on timing ──────────────────────
    r"|asap|right\s+away|soon|urgently"
    r"|couple\s+(of\s+)?(weeks|months)"
    r"|few\s+(weeks|months)"
    r"|end\s+of\s+(the\s+)?(month|year)"
    r"|next\s+(week|month|year)"
    # ── Motivation sharing: contact explaining why they'd sell ────────────────
    r"|divorce|inherit|estate|probate|relocat|moving|downsize"
    r"|behind\s+on|foreclos|retir|need\s+to\s+sell|want\s+to\s+sell|ready\s+to\s+sell"
    r")\b",
    re.I,
)


def contact_reversed_after_index(messages: list[dict], signal_idx: int) -> bool:
    """
    Check if contact showed genuine engagement AFTER a negative signal.

    Scans all contact messages after ``signal_idx`` for positive engagement
    indicators (price inquiry, property details, call requests, timeline
    discussion, etc.).

    Returns True if contact reversed → caller should NOT short-circuit.
    Returns False if no reversal detected → safe to short-circuit.

    NOTE: This guard is deliberately conservative. It only fires on explicit
    engagement patterns — a simple "ok" or "?" reply does NOT count as a
    reversal. This prevents false Potential labels on contacts who just
    acknowledge receipt without re-engaging.
    """
    if not messages or signal_idx < 0:
        return False

    for m in messages[signal_idx + 1:]:
        sender = (m.get("sender") or "").strip().lower()
        if sender not in ("contact", "lead"):
            continue
        body = (m.get("message") or m.get("body") or "").strip()
        if not body or len(body) < 3:
            continue
        if _REVERSAL_ENGAGEMENT_RE.search(body):
            return True

    return False


# ── Wrong-Number Re-engagement Guard ─────────────────────────────────────────
# After a contact says "wrong number", the agent SHOULD pivot to the referral
# close. If the contact then re-engages — offers a referral, asks a question,
# gives an address, or volunteers property details — the agent is expected to
# switch into funnel mode. The agent's later pitch-like messages are correct
# handling, NOT a "continued original pitch after wrong number" violation.

_WN_REENGAGE_RE = re.compile(
    r"\b("
    # referral: contact offering someone else / another property
    r"someone|anyone|somebody|anybody|neighbor|friend|cousin|brother|sister"
    r"|buddy|co-?worker|colleague"
    r"|my\s+(mom|dad|mother|father|son|daughter|aunt|uncle|family|wife|husband)"
    r"|i\s+(have|know|own|got|do\s+have)\b"
    r"|know\s+(of\s+)?(someone|somebody|a\s+guy|anybody)"
    r"|what\s+about|how\s+about"
    # property types / details the contact volunteers
    r"|mobile\s+home|trailer|condo|duplex|townhouse|vacant\s+lot|land"
    r"|a\s+(house|home|property|lot)\b"
    r"|rental\s+propert|investment\s+propert"
    r"|remodel|renovat|bedroom|bathroom|acre"
    r")\b",
    re.I,
)

# Address-like: house number + street name + street-type suffix.
_WN_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+[\w'.-]+(?:\s+[\w'.-]+){0,5}\s+"
    r"(dr|drive|st|street|ave|avenue|rd|road|ln|lane|blvd|boulevard"
    r"|ct|court|way|cir|circle|pl|place|hwy|pkwy|terrace|trail|trl)\b",
    re.I,
)


def contact_reengaged_after_wn(messages: list[dict], wn_idx: int | None) -> bool:
    """
    True if the contact re-engaged AFTER the wrong-number message.

    Re-engagement = offering a referral, asking a substantive question,
    giving an address, or volunteering property details. When this happens
    the agent is expected to switch into funnel mode, so FLAG 5 ("continued
    original pitch after wrong number") must NOT fire.
    """
    if wn_idx is None or wn_idx < 0:
        return False
    for m in (messages or [])[wn_idx + 1:]:
        sender = (m.get("sender") or "").strip().lower()
        if sender not in ("contact", "lead"):
            continue
        body = (m.get("message") or m.get("body") or "").strip()
        if len(body) < 2:
            continue
        if (_WN_REENGAGE_RE.search(body)
                or _WN_ADDRESS_RE.search(body)
                or _REVERSAL_ENGAGEMENT_RE.search(body)):
            return True
    return False
