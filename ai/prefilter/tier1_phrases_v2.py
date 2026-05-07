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
    # Note: "nothing about this interests me. Give me a number..." is NOT opt-out
    # Only match when it's a hard stop (followed by end of message or hard punctuation)
    re.compile(r"\bnothing\s+about\s+this.{0,20}interests\s+me[.!]\s*$", re.I | re.MULTILINE),
]

# ── Suspicious / risky patterns → always escalate ────────────────────────────

_SUSPICIOUS_PATTERNS = [
    re.compile(r"\b(fuck\s+you|piss\s+off|go\s+to\s+hell|scammer|fraud)\b", re.I),
    re.compile(r"\b(my\s+offer\s+is|i('|')?ll\s+offer\s+you|offering\s+you)\s*\$?\d{3,}", re.I),
]

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
    re.compile(r"\b[Ii](?:'|'|')?m\s+not\s+[A-Z]\w+\b"),
    re.compile(r"\bthat(?:'|'|')?s?\s+not\s+(me|my\s+name)\b", re.I),
    re.compile(r"\byou(?:'|'|')?(?:ve|'?re)\s+(got|texting\s+the)\s+wrong\b", re.I),
    re.compile(r"\bi(?:'|'|')?m\s+not\s+(?:that|the|this)\s+person\b", re.I),
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
    re.compile(r"\b(for\s+sale|could\s+it\s+be\s+sold|be\s+available|listing\s+it)\b", re.I),  # "could be for sale within"
]

# ── Not Interested (soft refusal) ─────────────────────────────────────────────

_NOT_INTERESTED_PATTERNS = [
    re.compile(r"\bnot\s+(at\s+this\s+time|interested|for\s+sale|looking|selling|ready|yet)\b", re.I),
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
    r"\b(absolutely\s+not|certainly\s+not|definitely\s+not|not\s+at\s+all)\b", re.I
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

# ── Question guard (abandoned lead detector) ──────────────────────────────────

_QUESTION_RE = re.compile(
    r"\?|\b(what|where|when|who|which|how|why)\b",
    re.I,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _body(m: dict) -> str:
    return (m.get("body") or m.get("message") or "").strip()


def _sender(m: dict) -> str:
    return (m.get("sender") or "").lower()


def _detect_pillars(contact_msgs: list[dict]) -> list[str]:
    contact_text = " ".join(_body(m) for m in contact_msgs)
    return [pillar for pillar, pat in _PILLAR_PATTERNS.items() if pat.search(contact_text)]


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
) -> dict:
    return {
        "compliance_score": scores["compliance_score"],
        "sentiment_score": scores["sentiment_score"],
        "professionalism_score": scores["professionalism_score"],
        "script_adherence_score": scores["script_adherence_score"],
        "funnel_tier": funnel_tier,
        "funnel_stage_reached": funnel_stage,
        "pillars_gathered": pillars or [],
        "rebuttals_used": [],
        "label_assigned": label_assigned,
        "label_correct": True,
        "label_should_be": label_assigned,
        "label_reason": label_reason,
        "red_flags": [],
        "actions_triggered": [],
        "summary": summary,
        "model_used": "prefilter_t1_v2",
        "contact_name": contact_name,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def evaluate(
    messages: list[dict],
    funnel_tier: str,
    agent_name: str,
    contact_name: str,
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

    pillar_threshold = _PILLAR_THRESHOLD[funnel_tier]

    # Normalize message fields once
    messages = [
        {**m, "body": _body(m), "sender": _sender(m)}
        for m in messages
    ]

    contact_msgs = [m for m in messages if _sender(m) == "contact"]
    agent_msgs   = [m for m in messages if _sender(m) == "agent"]
    contact_text = " \n ".join(_body(m) for m in contact_msgs)
    all_text     = " \n ".join(_body(m) for m in messages)

    # ── GUARD: Contact asked a question, agent never replied ──────────────────
    if contact_msgs and agent_msgs:
        last_q_idx = None
        for i, m in enumerate(messages):
            if _sender(m) != "agent" and _QUESTION_RE.search(_body(m)):
                last_q_idx = i
        if last_q_idx is not None:
            agent_replied = any(_sender(m) == "agent" for m in messages[last_q_idx + 1:])
            if not agent_replied:
                return PrefilterResult(
                    tier_hit=1, decision="escalate", confidence=1.0,
                    notes="contact asked question; agent did not reply — potential abandonment",
                )

    # ── Check 1: explicit opt-out — escalate if agent continued ──────────────
    for pat in _OPT_OUT_PATTERNS:
        if pat.search(contact_text):
            optout_idx = next(
                (i for i, m in enumerate(messages)
                 if _sender(m) == "contact" and pat.search(_body(m))),
                None,
            )
            if optout_idx is not None:
                agent_after = [m for m in messages[optout_idx + 1:] if _sender(m) == "agent"]
                if len(agent_after) >= 2:
                    return PrefilterResult(
                        tier_hit=1, decision="escalate", confidence=1.0,
                        notes=f"opt-out detected AND agent continued ({len(agent_after)} msgs): /{pat.pattern}/",
                    )
                break  # clean opt-out → handled in Check 6

    # ── Check 2: suspicious patterns → always escalate ───────────────────────
    for pat in _SUSPICIOUS_PATTERNS:
        if pat.search(all_text):
            return PrefilterResult(
                tier_hit=1, decision="escalate", confidence=1.0,
                notes=f"suspicious pattern: /{pat.pattern}/",
            )

    # ── Check 3: contact never replied → drip / silent ───────────────────────
    if len(contact_msgs) == 0:
        scores = {
            "compliance_score": 100, "sentiment_score": 80,
            "professionalism_score": 95, "script_adherence_score": 60,
        }
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

    # ── Check 4: Wrong Number (explicit or identity mismatch) ─────────────────
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
        agent_msgs_after_wn = [m for m in messages[wn_idx + 1:] if _sender(m) == "agent"]
        agent_text_after_wn = " ".join(_body(m) for m in agent_msgs_after_wn)
        continued_pitch = any(p.search(agent_text_after_wn) for p in _AGENT_PITCH_AFTER_WN)
        # If agent sent 3+ messages after WN that's suspicious regardless of content
        if continued_pitch or len(agent_msgs_after_wn) >= 3:
            return PrefilterResult(
                tier_hit=1, decision="escalate", confidence=1.0,
                notes=f"[{funnel_tier}] wrong number but agent continued ({len(agent_msgs_after_wn)} msgs after WN)",
            )
        scores = {
            "compliance_score": 100, "sentiment_score": 85,
            "professionalism_score": 95, "script_adherence_score": 100,
        }
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.92,
            notes=f"[{funnel_tier}] wrong number — clean pivot",
            predicted_scores=scores,
            result=_clean_result(
                contact_name,
                summary="Wrong number. Texter apologized and pivoted to referral close.",
                scores=scores,
                funnel_tier=funnel_tier,
                funnel_stage="none",
                label_assigned="Wrong Number",
                label_reason="Contact explicitly stated wrong number.",
            ),
        )

    # ── Check 5: Sold property ────────────────────────────────────────────────
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
            ),
        )

    # ── Check 6: Clean opt-out (agent stopped correctly) ─────────────────────
    contact_opted_out = any(p.search(contact_text) for p in _OPT_OUT_PATTERNS)
    if contact_opted_out:
        optout_idx = next(
            (i for i, m in enumerate(messages)
             if _sender(m) == "contact"
             and any(p.search(_body(m)) for p in _OPT_OUT_PATTERNS)),
            None,
        )
        if optout_idx is not None:
            agent_after_optout = [m for m in messages[optout_idx + 1:] if _sender(m) == "agent"]
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
    #   - no good rebuttal (those need Groq to score quality)
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
        if ni_msg_count >= 2:
            # Multiple refusals → agent may have been too persistent → Groq must score
            return None

        first_ni_idx = next(
            (i for i, m in enumerate(messages)
             if _sender(m) == "contact"
             and any(p.search(_body(m)) for p in _NOT_INTERESTED_PATTERNS)),
            None,
        )
        if first_ni_idx is not None:
            agent_after_ni = [m for m in messages[first_ni_idx + 1:] if _sender(m) == "agent"]
            # Check if contact sent ANY engagement signal after the NI (flip to interest)
            contact_after_ni = [m for m in messages[first_ni_idx + 1:] if _sender(m) == "contact"]
            contact_flipped = any(
                _POST_NI_FLIP_RE.search(_body(m)) for m in contact_after_ni
            )
            if contact_flipped:
                return None  # contact may have changed mind → Groq must evaluate

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
                        summary="Contact declined. Texter sent a rebuttal and closed cleanly.",
                        scores=scores,
                        funnel_tier=funnel_tier,
                        funnel_stage="wide",
                        label_assigned="Not Interested",
                        label_reason="Contact explicitly declined.",
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
