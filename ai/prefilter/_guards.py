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
    # FLAG 16 — no handoff after a valid lead push (label_validator.NO_HANDOFF_FLAG)
    "No handoff message sent after lead push.",
]

# ── Phase 1: stable flag IDs + rule-assigned metadata ────────────────────────
# The whitelist text is the canonical identity used across the pipeline. These
# maps bolt explainability metadata onto each flag WITHOUT changing red_flags
# (which stays a list[str]). Keyed by exact whitelist text.

FLAG_ID_MAP: dict[str, str] = {
    "Continued texting after explicit opt-out.":                                  "F1",
    "Used threatening, profane, or deceptive language.":                          "F2",
    "Stated a specific dollar offer.":                                            "F3",
    "Gave up after first no with zero rebuttal.":                                 "F4",
    "Continued original pitch after wrong number.":                              "F5",
    "Agreed to call without pre-qualifying.":                                     "F6",
    "Revealed or promised 6+ month timeline.":                                    "F7",
    "Sent incoherent message or wrong name.":                                     "F8",
    "Ended conversation after lead showed interest.":                             "F9",
    "Pushed to close with zero property info.":                                   "F10",
    "Did not escalate after all 4 pillars gathered.":                             "F11",
    "Skipped $1k referral close after high price.":                               "F12",
    "Agent re-asked for asking price after owner already stated it.":             "F13",
    "Agent kept pushing after above-market price instead of referral close.":     "F15",
    "Contact denied knowing the address after providing property details. Agent should have asked clarifying questions (parcel number, correct address) instead of closing the conversation. Label should be Potential or Undefined, not Bluffer.": "F14",
    "No handoff message sent after lead push.":                                    "F16",
}

# Severity is a fixed business attribute of the flag — never model-decided.
SEVERITY_MAP: dict[str, str] = {
    "F1": "critical", "F2": "high",   "F3": "critical", "F4": "high",
    "F5": "high",     "F6": "medium", "F7": "medium",   "F8": "high",
    "F9": "high",     "F10": "medium","F11": "medium",  "F12": "medium",
    "F13": "low",     "F14": "medium","F15": "medium", "F16": "medium",
}

# Rule-assigned confidence by detection strength (no model confidence available).
#   regex-verified compliance → 0.90 | guard-verified flow → 0.80
#   model-judgment-only       → 0.60 | fragile regex (F13)  → 0.40
CONFIDENCE_MAP: dict[str, float] = {
    "F1": 0.90, "F2": 0.90, "F3": 0.90, "F4": 0.80, "F5": 0.80,
    "F6": 0.60, "F7": 0.60, "F8": 0.60, "F9": 0.60, "F10": 0.60,
    "F11": 0.60, "F12": 0.60, "F13": 0.40, "F14": 0.60, "F15": 0.60,
    "F16": 0.85,
}

# Context-heavy / fragile flags routed to Needs-Review more aggressively
# (needs_review whenever confidence < 0.75 instead of the default < 0.55).
REVIEW_BIAS: set[str] = {"F7", "F13", "F15"}

WRONG_LABEL_FLAG_ID = "WRONG_LABEL"
SEVERITY_MAP[WRONG_LABEL_FLAG_ID] = "medium"
CONFIDENCE_MAP[WRONG_LABEL_FLAG_ID] = 0.70

DEFAULT_COACHING: dict[str, str] = {
    "F1": "Reinforce that any opt-out (stop/remove me) means stop immediately — no further messages.",
    "F2": "Review professional tone standards; the agent must stay polite regardless of the lead.",
    "F3": "Coach the agent to give a cash range, never a firm single-number offer over text.",
    "F4": "Send at least one scripted rebuttal (Future / Other Properties / $1k Referral) before exiting a soft no.",
    "F5": "On a wrong number, apologise and pivot to a referral ask — stop the original pitch.",
    "F6": "Gather at least one pillar before agreeing to a scheduled call.",
    "F7": "Confirm the lead's actual timeline before introducing a 6-month future window.",
    "F8": "Proofread before sending; verify the contact's correct name.",
    "F9": "Close an engaged lead with a handoff message instead of going silent.",
    "F10": "Collect basic property info before pushing for a call.",
    "F11": "When all pillars are gathered, escalate with a clear call-to-action.",
    "F12": "On a high/above-market price, use the $1k referral close before ending.",
    "F13": "Acknowledge the price the owner already stated — don't re-ask for it.",
    "F14": "On a push label, always send a handoff message (partner/team will reach out).",
    "F15": "On an above-market price, switch to the referral close rather than continuing to push.",
    "F16": "A pushed lead must be closed with a handoff message — coach the agent to always tell the lead the team will reach out.",
    WRONG_LABEL_FLAG_ID: "Re-label the conversation per the audit; review the labelling rule that was missed.",
}

EXPLAIN_TEMPLATE: dict[str, str] = {
    "F1": "The lead used explicit opt-out language and the agent kept messaging.",
    "F2": "An agent message contained threatening, profane, or deceptive language.",
    "F3": "The agent stated a specific firm dollar offer instead of a range.",
    "F4": "The lead gave a soft 'no' and the agent stopped without any rebuttal.",
    "F5": "The lead said wrong number and the agent continued the original pitch.",
    "F6": "The agent agreed to a call before gathering any qualifying pillar.",
    "F7": "The agent introduced a 6+ month timeline before confirming a shorter one.",
    "F8": "An agent message was incoherent or used the wrong contact name.",
    "F9": "The lead showed interest and the agent went silent without a handoff.",
    "F10": "The agent pushed to close with zero property information gathered.",
    "F11": "All four pillars were gathered but the agent did not escalate to a call.",
    "F12": "The lead gave a high price and the agent ended without the $1k referral close.",
    "F13": "The owner already stated an asking price and the agent re-asked for it.",
    "F14": "A push label was assigned but no handoff message was sent.",
    "F15": "The lead's price was above market and the agent kept pushing instead of the referral close.",
    "F16": "The lead was pushed (hand raise) but the agent never sent a handoff message.",
    WRONG_LABEL_FLAG_ID: "The audit found the assigned label does not match the conversation.",
}


def flag_id_for(text: str) -> str:
    """Return the stable F-code for a whitelist flag string ('' if unknown)."""
    return FLAG_ID_MAP.get((text or "").strip(), "")


def confidence_tier(flag_id: str, confidence: float) -> str:
    """Map a confidence score to a tier, with a stricter bar for fragile flags."""
    bar = 0.75 if flag_id in REVIEW_BIAS else 0.55
    if confidence < bar:
        return "needs_review"
    if confidence < 0.80:
        return "medium"
    return "high"


def _msg_fields(m: dict) -> tuple[str, str]:
    """Return (sender_lower, body) for a parsed message dict."""
    sender = (m.get("sender") or "").strip().lower()
    body = (m.get("message") or m.get("body") or "").strip()
    return sender, body


def _ev(m: dict, idx: int) -> dict:
    """Build one evidence entry from a message + its index."""
    sender = (m.get("sender") or "").strip() or "Unknown"
    body = (m.get("message") or m.get("body") or "").strip()
    quote = body if len(body) <= 240 else body[:237] + "..."
    return {"seq": m.get("seq", idx), "sender": sender, "quote": quote}


def extract_evidence(flag_id: str, messages: list[dict]) -> list[dict]:
    """
    Re-match the detector that fires a flag to pinpoint the offending message(s).

    Reuses the existing module regexes/guards. For model-judgment-only flags
    that have no deterministic detector (F6/F8/F9/F10/F11/F12), returns [] —
    the UI then falls back to the full transcript.
    """
    msgs = messages or []

    def _first_contact(rx) -> list[dict]:
        for i, m in enumerate(msgs):
            s, b = _msg_fields(m)
            if s in ("contact", "lead") and rx.search(b):
                return [_ev(m, i)]
        return []

    def _first_agent(rx) -> list[dict]:
        for i, m in enumerate(msgs):
            s, b = _msg_fields(m)
            if s and s not in ("contact", "lead") and rx.search(b):
                return [_ev(m, i)]
        return []

    def _contact_then_next_agent(rx) -> list[dict]:
        for i, m in enumerate(msgs):
            s, b = _msg_fields(m)
            if s in ("contact", "lead") and rx.search(b):
                out = [_ev(m, i)]
                for j in range(i + 1, len(msgs)):
                    s2, _ = _msg_fields(msgs[j])
                    if s2 and s2 not in ("contact", "lead"):
                        out.append(_ev(msgs[j], j))
                        break
                return out
        return []

    if flag_id == "F1":
        return _contact_then_next_agent(OPTOUT_TEXT_RE)
    if flag_id == "F2":
        return _first_agent(PROFANITY_RE)
    if flag_id == "F3":
        return _first_agent(DOLLAR_OFFER_RE)
    if flag_id == "F4":
        ev = _first_contact(SOFT_NO_RE)
        # add the agent's final message (the weak exit) if present
        for j in range(len(msgs) - 1, -1, -1):
            s, _ = _msg_fields(msgs[j])
            if s and s not in ("contact", "lead"):
                ev = ev + [_ev(msgs[j], j)]
                break
        return ev
    if flag_id == "F5":
        return _contact_then_next_agent(WRONG_NUMBER_RE)
    if flag_id == "F7":
        return _first_agent(TIMELINE_RE)
    if flag_id == "F13":
        return _first_agent(PILLAR_FLAG_RE)
    if flag_id == "F15":
        return _first_contact(DNC_JOKE_PRICE_RE)
    # F6, F8, F9, F10, F11, F12, F14, WRONG_LABEL: no clean single-message detector
    return []


# Dynamic / prefilter flag families → nearest canonical F-code (substring).
# The prefilter and label_validator emit flag phrasings that are NOT in the
# 15-string whitelist (e.g. "Re-asked closing timeline same day...", dynamic
# above-market price flags). These map them to the nearest family so they still
# get a severity + tier + routing; the original text stays as the explanation.
_DYNAMIC_REMAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bre-?asked\b", re.I),                          "F13"),
    (re.compile(r"inflated price|above[\s-]?market", re.I),       "F15"),
    (re.compile(r"\bno handoff\b", re.I),                         "F14"),
    (re.compile(r"missed pillars?", re.I),                        "F11"),
    (re.compile(r"opt[\s-]?out|unsubscribe|stop\s+text", re.I),   "F1"),
]


def _remap_dynamic_flag(text: str) -> str:
    for rx, fid in _DYNAMIC_REMAP:
        if rx.search(text or ""):
            return fid
    return ""


def _detail_for(fid: str, text: str, messages, source, *, canonical: bool) -> dict:
    conf = CONFIDENCE_MAP.get(fid, 0.60)
    return {
        "flag_id": fid,
        "flag_text": text.strip(),
        "severity": SEVERITY_MAP.get(fid, "medium"),
        "confidence": conf,
        "confidence_tier": confidence_tier(fid, conf),
        "evidence": extract_evidence(fid, messages),
        # Canonical flags use the curated template; remapped/dynamic flags keep
        # their own (more specific) text as the explanation.
        "explanation": EXPLAIN_TEMPLATE.get(fid, "") if canonical else text.strip(),
        "coaching": DEFAULT_COACHING.get(fid, ""),
        "source": source or "groq",
        "origin": "deterministic",
    }


def _detail_wrong_label(text: str, source) -> dict:
    """Wrong-label detail — flag_text is the EXACT original string so the
    dashboard's flag_details lookup matches (prefilter wrong-label flags may
    carry suffixes like \"(contact said: 'six million')\")."""
    fid = WRONG_LABEL_FLAG_ID
    conf = CONFIDENCE_MAP[fid]
    return {
        "flag_id": fid,
        "flag_text": text.strip(),
        "severity": SEVERITY_MAP[fid],
        "confidence": conf,
        "confidence_tier": confidence_tier(fid, conf),
        "evidence": [],
        "explanation": text.strip(),   # the string already explains itself
        "coaching": DEFAULT_COACHING[fid],
        "source": source or "groq",
        "origin": "deterministic",
    }


def _detail_generic(text: str, source) -> dict:
    """Catch-all so NO flag is ever dropped from flag_details."""
    conf = 0.60
    return {
        "flag_id": "OTHER",
        "flag_text": text.strip(),
        "severity": "medium",
        "confidence": conf,
        "confidence_tier": "medium",
        "evidence": [],
        "explanation": text.strip(),
        "coaching": "Review this flag against the conversation transcript.",
        "source": source or "groq",
        "origin": "deterministic",
    }


def build_flag_details(flags, messages, source=None) -> list[dict]:
    """
    Build rich, rule-assigned flag_details for a list of red-flag strings.
    EVERY flag produces exactly one detail object (never dropped); flag_text
    always equals the original string so the dashboard can match it. Pure /
    deterministic — no model or API calls, zero extra token cost.

    Resolution order per flag: wrong-label → exact whitelist (canonical) →
    dynamic family remap → generic 'OTHER'.
    """
    out: list[dict] = []
    for text in flags or []:
        if not isinstance(text, str) or not text.strip():
            continue
        t = text.strip()
        if t.lower().startswith("wrong label:"):
            out.append(_detail_wrong_label(t, source))
            continue
        fid = flag_id_for(t)
        if fid:
            out.append(_detail_for(fid, t, messages, source, canonical=True))
            continue
        fid = _remap_dynamic_flag(t)
        if fid:
            out.append(_detail_for(fid, t, messages, source, canonical=False))
            continue
        out.append(_detail_generic(t, source))
    return out


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

    _contact_text = " \n ".join(
        (m.get("body") or m.get("message") or "")
        for m in (messages or [])
        if (m.get("sender") or "").strip().lower() in ("contact", "lead")
    )
    _wants_ni = bool(re.search(r"\bnot\s+interested\b", should_be, re.I))

    # Guard A: texter chose DNC, auditor wants Not Interested, contact was
    # mocking/condescending ("do you ask dumb things on purpose?"). Dismissive
    # hostility without a regex opt-out is an accepted DNC scenario — confirm
    # the texter's label instead of flagging it.
    if (
        result.get("label_correct") is False
        and _wants_ni
        and DNC_LABEL_RE.search(assigned)
    ):
        from ai.prefilter.summary_builder import _CONDESCENSION_RE
        if _CONDESCENSION_RE.search(_contact_text):
            result["label_correct"] = True
            result["label_should_be"] = assigned
            result["label_reason"] = (
                "Contact paired the refusal with mocking/condescending language — "
                "dismissive hostility is an accepted Do Not Call scenario, so the "
                "texter's label stands (not forced to Not Interested)."
            )
            return

    # Guard B: texter chose Abv MV / Bluffer, auditor wants Not Interested, but
    # the contact stated a concrete price before declining. The "No" rejects the
    # agent's number (price disagreement), not the idea of selling.
    if (
        result.get("label_correct") is False
        and _wants_ni
        and re.search(r"\babv\s*mv\b|\babove\s+market\b|\bbluffer\b", assigned, re.I)
    ):
        from ai.prefilter.summary_builder import (
            detect_abv_mv_response,
            _BUYER_SIDE_REJECTION_RE,
        )
        _abv = detect_abv_mv_response(messages)
        if _abv["contact_stated_price"]:
            _buyer_side = _BUYER_SIDE_REJECTION_RE.search(_contact_text)
            result["label_correct"] = True
            result["label_should_be"] = assigned
            result["label_reason"] = (
                f"Contact quoted ${_abv['price_amount']:,.0f} before declining — the "
                "'No' rejects the agent's price range, not the idea of selling."
                + (" The buy-side reply ('I'd buy at that price') confirms the number "
                   "was rejected as too low." if _buyer_side else "")
                + f" '{assigned}' is the correct team label for a price-disagreement decline."
            )
            return

    # Guard D: texter chose DNC, auditor wants Wrong Number, but the contact
    # revealed they are a minor ("I'm 15"). Kid-DNC compliance rule: a minor on
    # the line is always DO Not Call — it beats Wrong Number even when "not
    # Robert / random text" also fired.
    if (
        result.get("label_correct") is False
        and DNC_LABEL_RE.search(assigned)
        and re.search(r"\bwrong\s+(number|person)\b", should_be, re.I)
    ):
        from ai.prefilter.label_validator import _DNC_MINOR_OWNER
        if any(p.search(_contact_text) for p in _DNC_MINOR_OWNER):
            result["label_correct"] = True
            result["label_should_be"] = assigned
            result["label_reason"] = (
                "Contact revealed they are a minor — compliance requires DO Not "
                "Call (kid-DNC rule beats Wrong Number). The texter's label stands."
            )
            return

    # Guard C: auditor wants Bluffer, but the contact never quoted a concrete
    # dollar amount and made no bluff/paranoid statement. Holding out for
    # "full value" / refusing to name a number first is a NEGOTIATION STANCE,
    # not a bluff — Bluffer requires a joke-tier price ($1M+) or time-wasting
    # signals (e.g. "the FBI monitors this phone"). A contact asking "what's
    # your best offer?" is engaged, not bluffing.
    if (
        result.get("label_correct") is False
        and re.search(r"\bbluffer\b", should_be, re.I)
        and not re.search(r"\bbluffer\b", assigned, re.I)
        and not has_joke_price
    ):
        from ai.prefilter.summary_builder import _parse_contact_price
        from ai.prefilter.tier1_phrases_v2 import _has_bluffer_indicator
        _stated_concrete_price = any(
            _parse_contact_price((m.get("body") or m.get("message") or "").strip()) is not None
            for m in (messages or [])
            if (m.get("sender") or "").strip().lower() in ("contact", "lead")
        )
        _bluff, _ = _has_bluffer_indicator(messages or [])
        if not _stated_concrete_price and not _bluff:
            result["label_correct"] = True
            result["label_should_be"] = assigned
            result["label_reason"] = (
                "Contact never quoted a concrete dollar amount and made no "
                "bluff/time-wasting statement — wanting 'full value' or flipping "
                "the price question back to the agent is a negotiation stance, "
                f"not a Bluffer signal. '{assigned}' stands."
            )
            return

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
        # Joke price (1 million, etc) ACCEPTED as DNC, Bluffer, or Abv MV
        # (same accepted-label group as tier1_phrases_v2 / label_validator)
        assigned_is_abv = bool(re.search(r"\babv\s*mv\b|\babove\s+market\b", assigned, re.I))
        if assigned_is_dnc or assigned_is_bluffer or assigned_is_abv:
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
