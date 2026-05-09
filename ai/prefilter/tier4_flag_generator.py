"""
Tier 4 — Deterministic Flag Generator.

The terminal tier in the prefilter pipeline. Produces flag lists,
compliance scores, summaries, and label audits using ONLY deterministic
regex/pattern matching — no ML, no embeddings, no API calls.

When the pipeline reaches T4 (i.e. T1-T3 all said "escalate or low
confidence"), T4 emits a conservative result using guard helpers
extracted into _guards.py plus the summary_builder patterns.

Contract:
    Input  → messages: list[dict], agent_name: str, contact_name: str,
             assigned_labels: list[str]
    Output → dict matching analyzer's result schema:
             compliance_score, sentiment_score, professionalism_score,
             script_adherence_score, red_flags, label_correct,
             label_should_be, label_reason, summary, model_used, …
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ai.prefilter._guards import (
    WHITELIST_FLAG_OUTPUTS,
    OPTOUT_TEXT_RE,
    SOFT_NO_RE,
    DOLLAR_OFFER_RE,
    PROFANITY_RE,
    TIMELINE_RE,
    WRONG_NUMBER_RE,
    agent_continued_after_opt_out,
    agent_replied_after_first_soft_no,
    last_message_from_contact,
    contact_has_explicit_opt_out,
    contact_has_dnc_joke_price,
    apply_label_guards,
    normalize_red_flags,
)
from ai.prefilter.summary_builder import (
    build_summary,
    detect_label,
    detect_funnel_stage,
    classify_agent_messages,
    detect_abv_mv_response,
)

logger = logging.getLogger(__name__)


# ── Additional patterns specific to T4 flag detection ────────────────────────

_INTEREST_PATTERNS = [
    re.compile(r"\b(interested|tell\s+me\s+more|what.?s\s+the\s+offer|how\s+much|send\s+me\s+info)\b", re.I),
    # NOTE: we do NOT include generic affirmatives (yeah, yes, ok) — too many false positives
]

_PILLAR_KEYWORDS = {
    "condition":  re.compile(r"\b(condition|repair|fix|roof|foundation|needs\s+work|fixer)\b", re.I),
    "price":      re.compile(r"\b(price|how\s+much|worth|value|offer|asking)\b", re.I),
    "timeline":   re.compile(r"\b(timeline|when|how\s+soon|ready\s+to|urgency|asap)\b", re.I),
    "motivation": re.compile(r"\b(motivation|why\s+(sell|selling)|reason|situation|circumstance)\b", re.I),
}

_WRONG_NAME_RE = re.compile(r"\b(wrong\s+name|that.?s\s+not\s+my\s+name|who\s+is\s+\w+)\b", re.I)

_INCOHERENT_RE = re.compile(
    r"(asdf|qwerty|jkl|lorem|test\s+test|placeholder|xxx)", re.I
)

_CALL_AGREE_RE = re.compile(
    r"\b(i.?ll\s+call\s+you|let\s+me\s+call|give\s+you\s+a\s+call|i.?ll\s+ring)\b", re.I
)

# FLAG 13 patterns ─────────────────────────────────────────────────────────
# Contact stating a specific asking price (number, dollar amount, or range)
_CONTACT_PRICE_STATED_RE = re.compile(
    r"(\$\s?\d[\d,]*(?:\s*(?:k|K|thousand|hundred))?)"
    r"|(\b\d[\d,]*\s*(?:k|K|thousand)\b)"
    r"|\b(?:want|asking|ask|need|looking\s+for|take|accept)\s+\$?\s?\d",
    re.I,
)

# Agent asking for the price ("price in mind", "what would you want", etc.)
_AGENT_PRICE_ASK_RE = re.compile(
    r"\b(price\s+in\s+mind"
    r"|had\s+a\s+price\s+in\s+mind"
    r"|what.*?price"
    r"|how\s+much.*?(?:ask|want|sell|looking)"
    r"|do\s+you\s+have\s+a\s+price"
    r"|what\s+(?:would|do)\s+you\s+want"
    r"|what.*?would\s+you\s+(?:take|accept)"
    r"|what.*?asking\b)",
    re.I,
)


def generate(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    assigned_labels: list[str] | None = None,
) -> dict:
    """
    Run the deterministic T4 flag generator.

    Returns a complete result dict matching the analyzer schema.
    """
    assigned_labels = assigned_labels or []

    agent_msgs = [
        m for m in messages
        if (m.get("sender") or "").strip().lower() not in ("contact", "lead")
    ]
    contact_msgs = [
        m for m in messages
        if (m.get("sender") or "").strip().lower() in ("contact", "lead")
    ]

    raw_flags: list[str] = []

    # ── FLAG 1: Continued texting after explicit opt-out ──────────────
    if agent_continued_after_opt_out(messages):
        raw_flags.append("Continued texting after explicit opt-out.")

    # ── FLAG 2: Profane / threatening / deceptive language ────────────
    for m in agent_msgs:
        body = (m.get("message") or m.get("body") or "").strip()
        if PROFANITY_RE.search(body):
            raw_flags.append("Used threatening, profane, or deceptive language.")
            break

    # ── FLAG 3: Stated a specific dollar offer ───────────────────────
    for m in agent_msgs:
        body = (m.get("message") or m.get("body") or "").strip()
        if DOLLAR_OFFER_RE.search(body):
            raw_flags.append("Stated a specific dollar offer.")
            break

    # ── FLAG 4: Gave up after first no with zero rebuttal ────────────
    if not agent_replied_after_first_soft_no(messages):
        # Contact said no and agent didn't follow up
        has_soft_no = False
        for m in contact_msgs:
            body = (m.get("message") or m.get("body") or "").strip()
            if SOFT_NO_RE.search(body):
                has_soft_no = True
                break
        if has_soft_no and not last_message_from_contact(messages):
            # Agent sent last message but didn't rebuttal after the no
            # Only flag if there are at least 2 agent messages to have a meaningful conversation
            if len(agent_msgs) >= 2:
                raw_flags.append("Gave up after first no with zero rebuttal.")
    # Also guard: if last message is from contact, remove this flag
    if last_message_from_contact(messages) and "Gave up after first no with zero rebuttal." in raw_flags:
        raw_flags = [f for f in raw_flags if f != "Gave up after first no with zero rebuttal."]

    # ── FLAG 5: Continued original pitch after wrong number ──────────
    wrong_number_idx: int | None = None
    for i, m in enumerate(messages):
        sender = (m.get("sender") or "").strip().lower()
        body = (m.get("message") or m.get("body") or "").strip()
        if sender in ("contact", "lead") and WRONG_NUMBER_RE.search(body):
            wrong_number_idx = i
            break
    if wrong_number_idx is not None:
        # Check if agent continued pitching after wrong number
        for later in messages[wrong_number_idx + 1:]:
            sender = (later.get("sender") or "").strip().lower()
            body = (later.get("message") or later.get("body") or "").strip().lower()
            if sender not in ("contact", "lead"):
                # Check if agent acknowledged wrong number or continued pitch
                if any(w in body for w in ["sell", "offer", "property", "home", "house", "price", "cash"]):
                    raw_flags.append("Continued original pitch after wrong number.")
                    break

    # ── FLAG 6: Agreed to call without pre-qualifying ────────────────
    for m in agent_msgs:
        body = (m.get("message") or m.get("body") or "").strip()
        if _CALL_AGREE_RE.search(body):
            # Check if any pillars were gathered before the call offer
            all_text_before = " ".join(
                (prior.get("message") or prior.get("body") or "")
                for prior in messages
                if messages.index(prior) < messages.index(m)
            ).lower()
            pillars_hit = sum(
                1 for p_re in _PILLAR_KEYWORDS.values()
                if p_re.search(all_text_before)
            )
            if pillars_hit < 2:
                raw_flags.append("Agreed to call without pre-qualifying.")
            break

    # ── FLAG 7: Revealed or promised 6+ month timeline ───────────────
    # Only flag if agent uses reveal/promise language with the 6+ month pattern.
    # Just mentioning "6 months" in a normal conversation is NOT a violation.
    _REVEAL_PROMISE_RE = re.compile(
        r"\b(takes?|will\s+take|expect|looking\s+at|at\s+least|minimum|usually|typically|around|about)\b",
        re.I,
    )
    for m in agent_msgs:
        body = (m.get("message") or m.get("body") or "").strip()
        if TIMELINE_RE.search(body) and _REVEAL_PROMISE_RE.search(body):
            raw_flags.append("Revealed or promised 6+ month timeline.")
            break

    # ── FLAG 8: Sent incoherent message or wrong name ────────────────
    for m in agent_msgs:
        body = (m.get("message") or m.get("body") or "").strip()
        if _INCOHERENT_RE.search(body) or _WRONG_NAME_RE.search(body):
            raw_flags.append("Sent incoherent message or wrong name.")
            break

    # ── FLAG 9: Ended conversation after lead showed interest ────────
    contact_interested = any(
        p.search((m.get("message") or m.get("body") or ""))
        for m in contact_msgs
        for p in _INTEREST_PATTERNS
    )
    if contact_interested and messages:
        last_sender = (messages[-1].get("sender") or "").strip().lower()
        # Agent was last speaker AND no follow-up questions => ended prematurely
        if last_sender not in ("contact", "lead"):
            last_body = (messages[-1].get("message") or messages[-1].get("body") or "").strip().lower()
            # If agent's last message is short and doesn't continue the conversation
            if len(last_body) < 30 and not any(w in last_body for w in ["?", "call", "schedule", "when"]):
                raw_flags.append("Ended conversation after lead showed interest.")

    # ── Shared text vars (used by FLAG 10, 11, 12, and result builder) ─
    all_text = " ".join(
        (m.get("message") or m.get("body") or "") for m in messages
    ).lower()
    agent_text = " ".join(
        (m.get("message") or m.get("body") or "") for m in agent_msgs
    ).lower()
    pillar_count = sum(1 for p_re in _PILLAR_KEYWORDS.values() if p_re.search(all_text))

    # ── FLAG 10: Pushed to close with zero property info ─────────────
    # Only flag on explicit contract/signing language — 'deal' and 'close' are too
    # common in normal real-estate texting to be reliable close-push indicators.
    has_close_push = any(w in agent_text for w in ["contract", "sign", "agreement", "closing date"])
    if has_close_push and pillar_count < 1:
        raw_flags.append("Pushed to close with zero property info.")

    # ── FLAG 11: Did not escalate after all 4 pillars gathered ───────
    if pillar_count >= 4:
        # Check if agent escalated (mentioned manager, appointment, etc.)
        escalation_re = re.compile(
            r"\b(manager|supervisor|appointment|schedule|set\s+up|escalat)\b", re.I
        )
        if not escalation_re.search(agent_text):
            raw_flags.append("Did not escalate after all 4 pillars gathered.")

    # ── FLAG 12: Skipped $1k referral close after high price ─────────
    high_price_re = re.compile(r"\$\s?\d{3,}(,\d{3})*\s*(k|thousand|K)?", re.I)
    contact_mentioned_high = any(
        high_price_re.search((m.get("message") or m.get("body") or ""))
        for m in contact_msgs
    )
    if contact_mentioned_high and not any(
        w in agent_text for w in ["referral", "refer", "$1k", "$1,000", "1000 for"]
    ):
        raw_flags.append("Skipped $1k referral close after high price.")

    # ── FLAG 13: Agent re-asked for asking price after owner stated it ─
    # Fires ONLY when:
    #   1. Contact already said a specific number/dollar amount as their price.
    #   2. Agent LATER sends a message asking for the price again (missed it).
    # Does NOT fire when contact never gave a number (e.g. "Yes, your offer?").
    _price_stated_idx: int | None = None
    for i, m in enumerate(messages):
        sender = (m.get("sender") or "").strip().lower()
        body = (m.get("message") or m.get("body") or "").strip()
        if sender in ("contact", "lead") and _CONTACT_PRICE_STATED_RE.search(body):
            _price_stated_idx = i
            break

    if _price_stated_idx is not None:
        for m in messages[_price_stated_idx + 1:]:
            sender = (m.get("sender") or "").strip().lower()
            body = (m.get("message") or m.get("body") or "").strip()
            if sender not in ("contact", "lead") and _AGENT_PRICE_ASK_RE.search(body):
                raw_flags.append(
                    "Agent re-asked for asking price after owner already stated it."
                )
                break

    # ── FLAG 14: Exceeded 3-rebuttal script maximum ──────────────────────────
    # Script: after 3rd No the texter must stop and mark Not Interested.
    # More than 3 rebuttals = script violation (too persistent / harassment risk).
    _msg_cls = classify_agent_messages(messages)
    if _msg_cls["rebuttal_count_exceeded"]:
        _n_reb = _msg_cls["rebuttals"]
        raw_flags.append(
            f"Exceeded 3-rebuttal script maximum ({_n_reb} rebuttals sent). "
            "Script says: after the 3rd No, stop texting and mark as Not Interested."
        )

    # ── FLAG 15: Agent kept pushing after above-market price ──────────────────
    # Script: if price is above market, do the $1k referral close and END.
    # If the agent kept pushing (timeline/motivation/etc.) instead → violation.
    _abv = detect_abv_mv_response(messages)
    if _abv["contact_stated_price"] and _abv["agent_kept_pushing"] and not _abv["agent_did_referral_close"]:
        _price = _abv["price_amount"]
        raw_flags.append(
            f"Agent kept pushing after above-market price (${_price:,.0f}) "
            "instead of doing the $1k referral close per script."
        )

    # ── Normalize through the whitelist ──────────────────────────────
    final_flags = normalize_red_flags(raw_flags)

    # ── Build scores ────────────────────────────────────────────────
    # Calibrated to match Groq's typical score distribution:
    # Clean conversations: ~90-95 range; flagged: ~70-85 range
    n_flags = len(final_flags)
    compliance = max(70, 95 - (n_flags * 12))
    sentiment = 88
    professionalism = 90
    script = max(75, 95 - (n_flags * 8))

    if not contact_msgs:
        sentiment = 82
        script = 92
        compliance = min(compliance, 95)

    scores = {
        "compliance_score": compliance,
        "sentiment_score": sentiment,
        "professionalism_score": professionalism,
        "script_adherence_score": script,
    }

    # ── Label audit ──────────────────────────────────────────────────
    label_name, label_reason = detect_label(messages, contact_name)
    funnel_stage = detect_funnel_stage(messages)

    # Check assigned label correctness
    label_correct = True
    label_should_be = None
    if assigned_labels:
        assigned_str = ", ".join(assigned_labels)
        # Simple correctness: we only flag DNC mismatches (most common error)
        if contact_has_explicit_opt_out(messages) or contact_has_dnc_joke_price(messages):
            has_dnc = any("not call" in l.lower() or "dnc" in l.lower() for l in assigned_labels)
            if not has_dnc:
                label_correct = False
                label_should_be = "DO Not Call"
                label_reason = "Contact used explicit opt-out language, so the correct label is DO Not Call."

    # ── Summary ──────────────────────────────────────────────────────
    summary = build_summary(
        messages, agent_name, contact_name, scores, model_used="prefilter_t4"
    )

    # ── Assemble result ──────────────────────────────────────────────
    result = {
        "compliance_score": compliance,
        "sentiment_score": sentiment,
        "professionalism_score": professionalism,
        "script_adherence_score": script,
        "red_flags": final_flags,
        "funnel_stage_reached": funnel_stage,
        "pillars_gathered": [
            name for name, p_re in _PILLAR_KEYWORDS.items()
            if p_re.search(all_text)
        ],
        # Accurate rebuttal breakdown from SMS-script classifier
        "rebuttals_used": [
            f"{_msg_cls['rebuttals']} rebuttal(s)",
            f"{_msg_cls['follow_ups']} follow-up(s)",
            f"{_msg_cls['pillar_questions']} pillar question(s)",
        ] if any([
            _msg_cls["rebuttals"], _msg_cls["follow_ups"], _msg_cls["pillar_questions"]
        ]) else [],
        "label_assigned": ", ".join(assigned_labels) if assigned_labels else None,
        "label_correct": label_correct,
        "label_should_be": label_should_be or (", ".join(assigned_labels) if assigned_labels else label_name),
        "label_reason": label_reason,
        "summary": summary,
        "model_used": "prefilter_t4",
        "contact_name": contact_name,
    }

    # Apply deterministic label guards (same as analyzer post-processing)
    apply_label_guards(result, messages)

    logger.debug(
        f"[T4] {contact_name} → flags={len(final_flags)}, "
        f"compliance={compliance}, pillars={result['pillars_gathered']}"
    )

    return result
