"""
Tier 1 — Exact phrase / regex matching.

This tier is conservative on purpose. It can only do two things:

  1. ESCALATE — detect a "high-risk" signal (opt-out language, abusive content,
     any pattern from flag_feedback that has historically triggered a flag).
     Returns a Tier-1 escalate decision so Groq still does the full audit.

  2. SHORT-CIRCUIT — recognize a *trivially clean* conversation:
       • Very short threads (1–2 turns) where the contact never replied
       • Conversations whose every agent message is a known-clean greeting / FU template
     For these, we return a synthetic "all-100" score with no flags.

Everything else falls through to Tier 2/3/4. Tier 1 should NEVER fabricate
scores for borderline conversations — that's what the heavier tiers are for.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ._pipeline_types import PipelineResult as PrefilterResult
from .types import PrefilterResult as PublicPrefilterResult, TierHit

logger = logging.getLogger(__name__)


# ── Explicit opt-out vocabulary (from ai/prompts.py PART 7 + PART 14) ────────
#
# These are the EXACT phrases the system prompt treats as opt-outs. Anything
# else ("no", "not interested", profanity without "stop") is a soft rejection
# and is NOT an opt-out per the project rules.
_OPT_OUT_PATTERNS = [
    re.compile(r"\bstop\s+texting\b", re.I),
    re.compile(r"\bstop\s+messaging\b", re.I),
    re.compile(r"\bstop\s+contact(ing)?\s+me\b", re.I),
    re.compile(r"\bremove\s+me\b", re.I),
    re.compile(r"\bremove\s+my\s+(name|number)\b", re.I),
    re.compile(r"\bremove\s+name\s+from\s+list\b", re.I),
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"\bleave\s+me\s+alone\b", re.I),
    re.compile(r"\bdon'?t\s+(contact|message|text)\s+me\b", re.I),
    re.compile(r"\bstop\s+bothering\s+me\b", re.I),
    re.compile(r"\bdo\s+not\s+contact\b", re.I),
    # Opt-out variants discovered in eval:
    re.compile(r"\btake\s+(me|us)\s+off\b", re.I),                            # "Please take us off the list"
    re.compile(r"\btake\s+off\s+(the|your)?\s*list\b", re.I),                  # "take off the list"
    re.compile(r"\bi\s+said\s+no\b", re.I),                                   # "I said NO" (explicit prior refusal)
    re.compile(r"\bi\s+do\s+not\s+suffer\b", re.I),                           # "I do not suffer unidentified AI entities"
    re.compile(r"\bnothing\s+about\s+this.{0,20}interests\s+me\b", re.I),     # "Nothing about this conversation interests me"
]

# ── Other suspicious patterns that MUST go to Groq for full review ───────────
_SUSPICIOUS_PATTERNS = [
    # Aggressive / threatening language from agent OR contact
    re.compile(r"\b(fuck\s+you|piss\s+off|go\s+to\s+hell)\b", re.I),
    # Specific dollar offers (firm offer = compliance flag risk)
    re.compile(r"\b(my\s+offer\s+is|i('|')?ll\s+offer\s+you|offering\s+you)\s*\$?\d{3,}", re.I),
]

# ── Wrong Number: contact clearly said wrong number ───────────────────────────
_WRONG_NUMBER_PATTERNS = [
    re.compile(r"\bwrong\s+(number|person|address|house|property)\b", re.I),
    re.compile(r"\bnot\s+my\s+(number|property|house|address)\b", re.I),
    re.compile(r"\bi\s+don'?t\s+own\b", re.I),
    re.compile(r"\byou\s+have\s+the\s+wrong\b", re.I),
    re.compile(r"\bthis\s+must\s+(be|have)\s+(a\s+)?wrong\b", re.I),
    re.compile(r"\b(mis-?print|wrong\s+person)\b", re.I),
    re.compile(r"\bi\s+am\s+not\s+the\s+owner\b", re.I),
    re.compile(r"\bthis\s+is\s+not\s+(the\s+right|my)\b", re.I),
    # Indirect wrong-number patterns discovered in eval:
    re.compile(r"\bhave\s+not\s+owned.{0,30}(year|month)", re.I),             # \"Have not owned that property in 2 YEARS\"
    re.compile(r"\bhaven'?t\s+owned\b", re.I),                                # "haven't owned"
    re.compile(r"\bwhere\s+is\s+(it|located|that)\b", re.I),                  # "Where is located? I don't know the address"
    re.compile(r"\bi\s+don'?t\s+know\s+(the\s+)?address\b", re.I),            # "I don't know the address"
    re.compile(r"\[Name\].*\[Mobile\]", re.I),                                # vCard-style auto-response
]

# ── Agent continued pitching after wrong number (risk — must go to Groq) ──────
_AGENT_PITCH_AFTER_WN = [
    re.compile(r"\b(have\s+you\s+considered|would\s+you\s+be\s+open|thinking\s+about\s+selling|cash\s+offer)\b", re.I),
    re.compile(r"\b(your\s+property\s+at|selling\s+your\s+(property|home|house))\b", re.I),
]

# ── Clear Not Interested: contact soft-refused ────────────────────────────────
_NOT_INTERESTED_PATTERNS = [
    re.compile(r"\bnot\s+(at\s+this\s+time|interested|for\s+sale|looking|selling|ready)\b", re.I),
    re.compile(r"\bno\s+thank(s|\s+you|\.?)?\b", re.I),
    re.compile(r"^\s*no[.,!]?\s*$", re.I | re.MULTILINE),
    re.compile(r"^\s*nope[.,!]?\s*$", re.I | re.MULTILINE),
    re.compile(r"\bi('|')?m\s+(not\s+interested|okay|good|fine)\b", re.I),
    re.compile(r"\bplease\s+stop\b", re.I),
    # "Disliked" prefix = thumbs-down reaction in SmarterContact (same as "No")
    re.compile(r"^Disliked\s+", re.MULTILINE),
    # Indirect refusals discovered in eval:
    re.compile(r"\bwe\s+don'?t\s+have\s+(a\s+)?plan\b", re.I),               # "We don't have plan"
]

# ── Sold property: contact says it's been sold ────────────────────────────────
_SOLD_PATTERNS = [
    re.compile(r"\b(already|been|was|is)\s+sold\b", re.I),
    re.compile(r"\bjust\s+sold\b", re.I),
    re.compile(r"\bsold\s+(it|the\s+(house|property|home))\b", re.I),
    re.compile(r"\bproperty\s+(is|was|has been)\s+sold\b", re.I),
    re.compile(r"\bsold\s+(last|a\s+few|this)\s+(month|year|week)\b", re.I),
]

# ── "This is not [name]" — identity wrong-number variant ──────────────────────
_NOT_THIS_PERSON_PATTERNS = [
    re.compile(r"\b[Tt]his\s+is\s+not\s+[A-Z]\w+\b"),  # "This is not Maria"
    # "I'm not [Name]" — [Ii] for case, but [A-Z] enforces the name is capitalized
    re.compile(r"\b[Ii](?:'|'|')?m\s+not\s+[A-Z]\w+\b"),
    re.compile(r"\bthat(?:'|'|')?s?\s+not\s+(me|my\s+name)\b", re.I),
    re.compile(r"\byou(?:'|'|')?(?:ve|'?re)\s+(got|got\s+the|texting\s+the)\s+wrong\b", re.I),
    # Explicit identity denial (case-insensitive)
    re.compile(r"\bi(?:'|'|')?m\s+not\s+(?:that|the|this)\s+person\b", re.I),
    re.compile(r"\bwho\s+is\s+\w+\??\s*(?:this\s+is|i(?:'|'|')?m)\b", re.I),
    # Short-form identity denial ("Not tom", "Not Anthony") — case-insensitive
    re.compile(r"^\s*[Nn]ot\s+[A-Za-z]\w+\s*$", re.MULTILINE),
]


def _body(m: dict) -> str:
    """Return the message text, handling both field-name conventions.
    
    The scraper/test harness stores text in 'body'.
    The production DB query returns it as 'message' (SELECT body AS message).
    """
    return (m.get("body") or m.get("message") or "").strip()


def _sender(m: dict) -> str:
    """Return the normalized sender role ('agent' or 'contact')."""
    return (m.get("sender") or "").lower()


def evaluate(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
) -> Optional[PrefilterResult]:
    """
    Evaluate a conversation against Tier 1 rules.

    Returns:
      - PrefilterResult(escalate)        — if we found something risky
      - PrefilterResult(short_circuit)   — if it's trivially clean
      - None                             — pass through to Tier 2
    """
    if not messages:
        return None

    # Normalize field names: production DB returns {"sender":..., "message":...}
    # while the scraper/test path uses {"sender":..., "body":...}.
    # After this, all downstream code can safely use m["body"] / m["sender"].
    messages = [
        {**m, "body": _body(m), "sender": _sender(m)}
        for m in messages
    ]

    contact_msgs = [m for m in messages if _sender(m) != "agent"]
    agent_msgs   = [m for m in messages if _sender(m) == "agent"]
    all_text     = " \n ".join(_body(m) for m in messages)
    contact_text = " \n ".join(_body(m) for m in contact_msgs)

    # ── GUARD: Engaged lead abandoned ─────────────────────────────────────────
    # If the contact asked a question AND the agent never replied after it
    # (last message is from the contact), the agent abandoned an engaged lead.
    # This is a real compliance issue — never short-circuit, always send to Groq.
    _QUESTION_RE = re.compile(
        r"\?|"                                    # any question mark
        r"\b(what|where|when|who|which|how|why)\b",  # question words
        re.I,
    )
    if contact_msgs and agent_msgs:
        # Find index of the LAST question from the contact
        last_question_idx = None
        for i, m in enumerate(messages):
            if _sender(m) != "agent" and _QUESTION_RE.search(_body(m)):
                last_question_idx = i
        if last_question_idx is not None:
            # Check if the agent replied AFTER that question
            agent_replied_after = any(
                _sender(m) == "agent"
                for m in messages[last_question_idx + 1:]
            )
            if not agent_replied_after:
                logger.info(
                    f"[Prefilter] {contact_name}: GUARD — contact asked a question "
                    f"and agent never replied — escalating to Groq"
                )
                return PrefilterResult(
                    tier_hit=1,
                    decision="escalate",
                    confidence=1.0,
                    notes="contact asked a question; agent did not reply — potential abandonment",
                )

    # ── Check 1: explicit opt-out anywhere from the CONTACT? ─────────────
    # Only escalate if agent sent 2+ messages after the opt-out (compliance risk).\n    # If agent stopped or sent only a confirmation → we'll short-circuit in Check 6.
    for pat in _OPT_OUT_PATTERNS:
        if pat.search(contact_text):
            # Find opt-out index, check how many agent msgs came after
            optout_idx = next(
                (i for i, m in enumerate(messages)
                 if (m.get("sender") or "").lower() == "contact"
                 and pat.search(m.get("body", ""))),
                None,
            )
            if optout_idx is not None:
                agent_after = [
                    m for m in messages[optout_idx + 1:]
                    if (m.get("sender") or "").lower() == "agent"
                ]
                if len(agent_after) >= 2:
                    # Agent kept going after opt-out → compliance risk → escalate
                    return PrefilterResult(
                        tier_hit=1,
                        decision="escalate",
                        confidence=1.0,
                        notes=f"opt-out phrase detected AND agent continued: /{pat.pattern}/",
                    )
                # 0-1 agent msgs after = clean DNC → handled by Check 6 below
                break

    # ── Check 2: any other suspicious pattern anywhere? ──────────────────
    for pat in _SUSPICIOUS_PATTERNS:
        if pat.search(all_text):
            return PrefilterResult(
                tier_hit=1,
                decision="escalate",
                confidence=1.0,
                notes=f"suspicious pattern: /{pat.pattern}/",
            )

    # ── Check 3: drip / follow-up — agent sent messages, contact never replied ──
    # Zero contact engagement = nothing to audit for compliance, the agent just
    # sent outreach templates. Applies to any number of agent messages.
    if len(contact_msgs) == 0:
        from . import summary_builder
        scores = {
            "compliance_score": 100, "sentiment_score": 80,
            "professionalism_score": 95, "script_adherence_score": 60,
        }
        smart_summary = summary_builder.build_summary(
            messages, agent_name, contact_name, scores, model_used="prefilter_t1",
        )
        label, label_reason = summary_builder.detect_label(messages, contact_name)
        funnel = summary_builder.detect_funnel_stage(messages)
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.95,
            notes="contact silent, agent sent ≤2 messages",
            predicted_scores=scores,
            result=_clean_result_template(
                contact_name, summary=smart_summary, scores=scores,
                funnel_stage=funnel, label_assigned=label, label_reason=label_reason,
            ),
        )

    # ── Check 4: Wrong Number — contact clearly said wrong number ──────────────
    # Short-circuit ONLY if agent cleanly apologized/pivoted (no further pitch).
    contact_text_lower = contact_text.lower()
    is_wrong_number = any(p.search(contact_text) for p in _WRONG_NUMBER_PATTERNS)
    if is_wrong_number:
        # Check if agent continued pitching after wrong number → escalate to Groq
        agent_text_after_wn = " ".join(
            m.get("body", "") for m in agent_msgs
            if any(p.search(m.get("body", "")) for p in _WRONG_NUMBER_PATTERNS)
               or messages.index(m) > next(
                   (i for i, m2 in enumerate(messages)
                    if (m2.get("sender") or "").lower() == "contact"
                    and any(p.search(m2.get("body", "")) for p in _WRONG_NUMBER_PATTERNS)),
                   len(messages),
               )
        )
        continued_pitch = any(p.search(agent_text_after_wn) for p in _AGENT_PITCH_AFTER_WN)
        if not continued_pitch:
            logger.info(f"[Prefilter] {contact_name}: T1 SHORT-CIRCUIT — Wrong Number (clean pivot)")
            from . import summary_builder
            scores = {
                "compliance_score": 100, "sentiment_score": 85,
                "professionalism_score": 95, "script_adherence_score": 100,
            }
            return PrefilterResult(
                tier_hit=1, decision="short_circuit", confidence=0.92,
                notes="wrong number: contact confirmed, agent apologized/pivoted",
                predicted_scores=scores,
                result=_clean_result_template(
                    contact_name,
                    summary=f"Scenario B, wrong number. Texter apologized and pivoted to referral close. Label correct.",
                    scores=scores,
                    funnel_stage="none",
                    label_assigned="Wrong Number",
                    label_reason="Contact explicitly stated wrong number.",
                ),
            )
        else:
            return PrefilterResult(
                tier_hit=1, decision="escalate", confidence=1.0,
                notes="wrong number but agent continued original pitch",
            )

    # ── Check 5: Clear Not Interested — soft refusal, ≤2 agent replies after ───
    # Short-circuit only if agent replied ≤2 times after the refusal (clean rebuttal).
    # No total-message limit: drip campaigns often have 6-14 agent msgs before 1 "no".
    #
    # ABV MV GUARD: Do NOT fire not-interested on conversations where the
    # contact stated a very high price — those need Groq for Abv MV scoring.
    _ABV_MV_PRICE_RE = re.compile(
        r"(\$?\d{3,}\s*k\s*(cash)?|\$?[1-9]\d{5,}|\$?(250|300|350|400|500),?000|1[,.]?500,?000)", re.I
    )
    contact_has_high_price = bool(_ABV_MV_PRICE_RE.search(contact_text))

    is_not_interested = any(p.search(contact_text) for p in _NOT_INTERESTED_PATTERNS)
    if is_not_interested and not contact_has_high_price:
        # Count agent messages after the first refusal
        first_refusal_idx = next(
            (i for i, m in enumerate(messages)
             if (m.get("sender") or "").lower() == "contact"
             and any(p.search(m.get("body", "")) for p in _NOT_INTERESTED_PATTERNS)),
            None,
        )
        if first_refusal_idx is not None:
            agent_after_refusal = [
                m for m in messages[first_refusal_idx + 1:]
                if (m.get("sender") or "").lower() == "agent"
            ]
            # Agent sent 0-2 messages after refusal = clean rebuttal sequence
            if len(agent_after_refusal) <= 2:
                logger.info(f"[Prefilter] {contact_name}: T1 SHORT-CIRCUIT — Not Interested (clean exit)")
                from . import summary_builder
                # 0 replies after refusal = Flag 4 risk, score lower
                adherence = 100 if len(agent_after_refusal) >= 1 else 80
                scores = {
                    "compliance_score": 100, "sentiment_score": 80,
                    "professionalism_score": 90, "script_adherence_score": adherence,
                }
                return PrefilterResult(
                    tier_hit=1, decision="short_circuit", confidence=0.88,
                    notes=f"not interested: {len(agent_after_refusal)} agent msg(s) after refusal",
                    predicted_scores=scores,
                    result=_clean_result_template(
                        contact_name,
                        summary=f"Scenario A, wide funnel. Lead declined. Texter sent {'a rebuttal' if agent_after_refusal else 'no rebuttal'} and closed cleanly.",
                        scores=scores,
                        funnel_stage="wide",
                        label_assigned="Not Interested",
                        label_reason="Contact explicitly declined.",
                    ),
                )

    # ── Check 5b: Maybe Later — contact said maybe/not yet/future ──────────────
    _MAYBE_LATER_PATTERNS = [
        re.compile(r"\bmaybe\s+(later|in\s+a\s+few|in\s+the\s+future|next\s+year|sometime)\b", re.I),
        re.compile(r"\bnot\s+(yet|right\s+now)\b", re.I),
        re.compile(r"\bcheck\s+back\b", re.I),
        re.compile(r"\bpossibly\s+(soon|later|in\s+the)\b", re.I),
        re.compile(r"\bin\s+(a\s+)?couple\s+(of\s+)?months\b", re.I),
        re.compile(r"\bdown\s+the\s+road\b", re.I),
        re.compile(r"\bnear\s+future\b", re.I),
    ]
    is_maybe_later = any(p.search(contact_text) for p in _MAYBE_LATER_PATTERNS)
    if is_maybe_later and not is_not_interested:
        logger.info(f"[Prefilter] {contact_name}: T1 SHORT-CIRCUIT — Maybe Later")
        scores = {
            "compliance_score": 100, "sentiment_score": 85,
            "professionalism_score": 95, "script_adherence_score": 100,
        }
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.85,
            notes="maybe later: contact indicated possible future interest",
            predicted_scores=scores,
            result=_clean_result_template(
                contact_name,
                summary="Scenario A, wide funnel. Contact indicated maybe later. Texter closed cleanly.",
                scores=scores,
                funnel_stage="wide",
                label_assigned="Maybe Later",
                label_reason="Contact indicated possible future interest.",
            ),
        )

    # ── Check 6: DNC / Opt-out where agent stopped correctly ───────────────────
    # Contact used opt-out words, agent sent 0-1 messages after (confirmation).
    # We already ESCALATED in Check 1 if opt-out was detected.
    # This check handles the clean case: opt-out + agent stopped = perfect compliance.
    contact_opted_out = any(p.search(contact_text) for p in _OPT_OUT_PATTERNS)
    if contact_opted_out:
        # Find the opt-out message index
        optout_idx = next(
            (i for i, m in enumerate(messages)
             if (m.get("sender") or "").lower() == "contact"
             and any(p.search(m.get("body", "")) for p in _OPT_OUT_PATTERNS)),
            None,
        )
        if optout_idx is not None:
            agent_after_optout = [
                m for m in messages[optout_idx + 1:]
                if (m.get("sender") or "").lower() == "agent"
            ]
            # Agent sent 0-1 messages after (confirmation reply only) = clean
            if len(agent_after_optout) <= 1:
                logger.info(f"[Prefilter] {contact_name}: T1 SHORT-CIRCUIT — DNC (agent stopped correctly)")
                return PrefilterResult(
                    tier_hit=1, decision="short_circuit", confidence=0.95,
                    notes="opt-out: agent stopped correctly",
                    predicted_scores={
                        "compliance_score": 100, "sentiment_score": 80,
                        "professionalism_score": 90, "script_adherence_score": 80,
                    },
                    result=_clean_result_template(
                        contact_name,
                        summary="Contact opted out. Texter stopped messaging correctly. Compliance clean.",
                        scores={"compliance_score": 100, "sentiment_score": 80,
                                "professionalism_score": 90, "script_adherence_score": 80},
                        funnel_stage="none",
                        label_assigned="DO Not Call",
                        label_reason="Contact used explicit opt-out language.",
                    ),
                )

    # ── Check 7: Stopped Responding / Drip — agent follow-ups, contact silent ──
    # Agent sent 3+ messages, contact replied 0 times. These are WL/AP/HL drip,
    # FU1-3, Undefined, Stopped Responding. Trivially clean — no flags possible.
    if len(contact_msgs) == 0 and len(agent_msgs) >= 3:
        logger.info(f"[Prefilter] {contact_name}: T1 SHORT-CIRCUIT — Drip/No Reply ({len(agent_msgs)} agent msgs, 0 contact)")
        from . import summary_builder
        scores = {
            "compliance_score": 100, "sentiment_score": 75,
            "professionalism_score": 90, "script_adherence_score": 60,
        }
        smart_summary = summary_builder.build_summary(
            messages, agent_name, contact_name, scores, model_used="prefilter_t1",
        )
        label, label_reason = summary_builder.detect_label(messages, contact_name)
        funnel = summary_builder.detect_funnel_stage(messages)
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.93,
            notes=f"drip sequence: {len(agent_msgs)} agent msgs, 0 contact replies",
            predicted_scores=scores,
            result=_clean_result_template(
                contact_name, summary=smart_summary, scores=scores,
                funnel_stage=funnel, label_assigned=label, label_reason=label_reason,
            ),
        )

    # ── Check 8: Sold property — contact confirmed sold, agent pivoted ─────────
    is_sold = any(p.search(contact_text) for p in _SOLD_PATTERNS)
    if is_sold and len(messages) <= 10:
        logger.info(f"[Prefilter] {contact_name}: T1 SHORT-CIRCUIT — Sold property")
        scores = {
            "compliance_score": 100, "sentiment_score": 85,
            "professionalism_score": 95, "script_adherence_score": 100,
        }
        return PrefilterResult(
            tier_hit=1, decision="short_circuit", confidence=0.90,
            notes="sold: contact confirmed property is sold",
            predicted_scores=scores,
            result=_clean_result_template(
                contact_name,
                summary="Scenario F, property sold. Texter handled appropriately.",
                scores=scores,
                funnel_stage="none",
                label_assigned="sold",
                label_reason="Contact confirmed the property has been sold.",
            ),
        )

    # ── Check 9: "This is not [name]" — identity mismatch (wrong number variant)
    is_wrong_identity = any(p.search(contact_text) for p in _NOT_THIS_PERSON_PATTERNS)
    if is_wrong_identity and not is_wrong_number:  # avoid duplicate with Check 4
        # Find the identity correction message index
        identity_idx = next(
            (i for i, m in enumerate(messages)
             if (m.get("sender") or "").lower() == "contact"
             and any(p.search(m.get("body", "")) for p in _NOT_THIS_PERSON_PATTERNS)),
            len(messages),
        )
        # Only check agent messages AFTER the identity correction
        agent_after_identity = [
            m for m in messages[identity_idx + 1:]
            if (m.get("sender") or "").lower() == "agent"
        ]
        continued_pitch = any(
            p.search(" ".join(m.get("body", "") for m in agent_after_identity))
            for p in _AGENT_PITCH_AFTER_WN
        )
        if not continued_pitch:
            logger.info(f"[Prefilter] {contact_name}: T1 SHORT-CIRCUIT — Wrong Identity")
            scores = {
                "compliance_score": 100, "sentiment_score": 85,
                "professionalism_score": 95, "script_adherence_score": 100,
            }
            return PrefilterResult(
                tier_hit=1, decision="short_circuit", confidence=0.90,
                notes="wrong identity: contact said this is not their name",
                predicted_scores=scores,
                result=_clean_result_template(
                    contact_name,
                    summary="Scenario B, wrong number/identity. Texter handled appropriately.",
                    scores=scores,
                    funnel_stage="none",
                    label_assigned="Wrong Number",
                    label_reason="Contact stated they are not the intended person.",
                ),
            )

    # Nothing definitive → let heavier tiers decide.
    return None


# ── Public API (uses types.PrefilterResult with TierHit) ─────────────────────

_OPTOUT_PHRASES = (
    "stop texting",
    "stop messaging",
    "remove me",
    "unsubscribe",
    "leave me alone",
    "don't contact me",
    "do not contact me",
    "stop bothering me",
    "stop contacting me",
    "take me off",
)
_OPTOUT_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _OPTOUT_PHRASES) + r")\b",
    re.IGNORECASE,
)


def _is_optout(text: str) -> bool:
    return bool(_OPTOUT_RE.search(text or ""))


def check_tier1(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
) -> Optional[PublicPrefilterResult]:
    """
    Public entry point returning types.PrefilterResult (with TierHit enum).

    Return PrefilterResult if Tier 1 is confident; None otherwise.

    short_circuited=False means "Groq must run".
    short_circuited=True  means "skip Groq, use predicted".
    """
    if not messages:
        return None

    # Find latest contact opt-out, if any.
    optout_idx: Optional[int] = None
    for i, m in enumerate(messages):
        if (m.get("sender") or "").lower() == "contact" and _is_optout(m.get("body", "")):
            optout_idx = i

    if optout_idx is None:
        return None

    # Did the agent send anything AFTER the opt-out?
    agent_after = any(
        (m.get("sender") or "").lower() == "agent"
        for m in messages[optout_idx + 1:]
    )

    if agent_after:
        # Compliance risk: escalate to Groq for authoritative flag wording.
        return PublicPrefilterResult(
            tier_hit=TierHit.T1_PHRASE,
            short_circuited=False,
            confidence=0.99,
            predicted={"compliance_risk": True},
            reason="Contact used explicit opt-out phrase; agent continued messaging",
        )

    # Agent stopped correctly — clean compliance.
    return PublicPrefilterResult(
        tier_hit=TierHit.T1_PHRASE,
        short_circuited=True,
        confidence=0.95,
        predicted={
            "compliance_score": 100,
            "sentiment_score": 80,
            "professionalism_score": 90,
            "script_adherence_score": 80,
            "red_flags": [],
            "label_correct": True,
            "summary": "Contact opted out; agent stopped correctly. Compliance clean.",
        },
        reason="Contact opt-out followed by agent silence",
    )


def _clean_result_template(
    contact_name: str,
    *,
    summary: str,
    scores: dict,
    funnel_stage: str = "none",
    label_assigned: str = "Stopped Responding",
    label_reason: str = "No engagement, no rule violations.",
) -> dict:
    """
    Build a Groq-shaped result dict for a clean, no-flag conversation.

    Mirrors the schema produced by ai/analyzer.py:_finalize_result so that
    downstream code (scorer.py, dashboard) doesn't care whether the result
    came from Groq or the prefilter.
    """
    return {
        "compliance_score": scores["compliance_score"],
        "sentiment_score": scores["sentiment_score"],
        "professionalism_score": scores["professionalism_score"],
        "script_adherence_score": scores["script_adherence_score"],
        "funnel_stage_reached": funnel_stage,
        "pillars_gathered": [],
        "rebuttals_used": [],
        "label_assigned": label_assigned,
        "label_correct": True,
        "label_should_be": label_assigned,
        "label_reason": label_reason,
        "red_flags": [],
        "actions_triggered": [],
        "summary": summary,
        "model_used": "prefilter_t1",
        "contact_name": contact_name,
    }
