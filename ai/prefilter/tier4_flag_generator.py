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
    REFERRAL_RE,
    agent_continued_after_opt_out,
    agent_replied_after_first_soft_no,
    contact_reengaged_after_wn,
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
from ai.prefilter.pillar_detection import detect_gathered_pillars

logger = logging.getLogger(__name__)


# ── Additional patterns specific to T4 flag detection ────────────────────────

_INTEREST_PATTERNS = [
    re.compile(r"\b(interested|tell\s+me\s+more|what.?s\s+the\s+offer|how\s+much|send\s+me\s+info)\b", re.I),
    # NOTE: we do NOT include generic affirmatives (yeah, yes, ok) — too many false positives
]

# Third-party referral framing — "my neighbor is interested" means the CONTACT
# is NOT interested themselves; it is a referral (Scenario D), not a hand-raise.
_REFERRAL_INTEREST_RE = re.compile(
    r"\b(neighbor|friend|someone\s+else|my\s+(brother|sister|mom|dad|mother|father|"
    r"son|daughter|cousin|uncle|aunt|partner|wife|husband|buddy|co-?worker))\b",
    re.I,
)
# Negated interest — "not interested", "isn't interested" — the token is NOT a hand-raise.
_NEGATED_INTEREST_RE = re.compile(
    r"\b(not|never|no)\b(?:\s+\w+){0,2}\s+interested\b|\bisn'?t\s+interested\b",
    re.I,
)


def _contact_showed_genuine_interest(contact_msgs: list[dict]) -> bool:
    """
    True only when the CONTACT expresses interest in selling THEIR OWN property.

    Excludes third-party referrals ("my neighbor is interested") and negated
    interest ("not interested") — both falsely matched the bare `interested`
    keyword and produced invalid F9 abandonment flags.
    """
    for m in contact_msgs:
        body = m.get("message") or m.get("body") or ""
        if not any(p.search(body) for p in _INTEREST_PATTERNS):
            continue
        if _REFERRAL_INTEREST_RE.search(body):
            continue  # referral — not the contact's own interest
        if _NEGATED_INTEREST_RE.search(body):
            continue  # "not interested"
        return True
    return False

# Handoff / escalation phrasing — ending an engaged lead with a handoff is the
# CORRECT close, not abandonment. Used to suppress FLAG 9 false positives.
_HANDOFF_RE = re.compile(
    r"\b(partner|team|colleague|manager|specialist|someone)\b.{0,60}"
    r"\b(touch\s+base|reach\s+out|be\s+in\s+touch|contact\s+you|call\s+you|connect|go\s+over)\b"
    r"|\bgo\s+over\s+(the\s+)?next\s+steps\b"
    r"|\b(pass(ing)?|hand(ing)?)\b.{0,30}\b(to\s+(my|our|the)|over|along)\b",
    re.I,
)

_WRONG_NAME_RE = re.compile(r"\b(wrong\s+name|that.?s\s+not\s+my\s+name|who\s+is\s+\w+)\b", re.I)

# ── Address denial after engagement ──────────────────────────────────────────
# Contact gave property details (pillars) but then said they don't know the address.
# This is NOT a Bluffer — the agent should have asked clarifying questions.
_ADDRESS_DENIAL_RE = re.compile(
    r"\b("
    r"don'?t\s+know\s+(that|the|this|your)?\s*(address|location|place|property|house|home)"
    r"|not\s+my\s+address"
    r"|wrong\s+address"
    r"|that'?s\s+not\s+(my|our|the)\s+(address|place|property|house|home)"
    r"|no\s+i\s+don'?t\s+know\s+(that|the)"
    r"|i\s+don'?t\s+(own|have)\s+(a\s+property|that\s+(house|home|property|place))"
    r"|never\s+(heard|seen)\s+of\s+(that|this)\s+(address|place|property|street)"
    r"|that\s+(address|location)\s+(is\s+)?(not|isn'?t)\s+(mine|ours|familiar)"
    r")\b",
    re.I,
)

_INCOHERENT_RE = re.compile(
    r"(asdf|qwerty|jkl|lorem|test\s+test|placeholder|xxx)", re.I
)

# F6 — contact agreed to a call OR agent confirmed a scheduled call (not a mere offer).
_CONTACT_CALL_AGREE_RE = re.compile(
    r"\b(call\s+me|you\s+can\s+call|go\s+ahead\s+and\s+call|phone\s+me|ring\s+me)\b"
    r"|\b(yes|sure|ok|okay|sounds\s+good|that\s+works|perfect|fine|great)\b.{0,60}\b(call|phone)\b"
    r"|\b(call|phone)\b.{0,40}\b(yes|sure|ok|okay|works|fine|good|great)\b",
    re.I,
)
_AGENT_CALL_BOOKING_RE = re.compile(
    r"\b(i.?ll\s+call\s+(?:you\s+)?(?:at|on|tomorrow|today|tonight|this\s+(?:afternoon|evening|morning))"
    r"|speak\s+(?:with\s+you\s+)?(?:at|on)\b"
    r"|scheduled\s+(?:for|a)\s+call)\b",
    re.I,
)


def _contact_body(m: dict) -> str:
    return (m.get("message") or m.get("body") or "").strip()


def _sender(m: dict) -> str:
    return (m.get("sender") or "").strip().lower()


def _is_contact(m: dict) -> bool:
    return _sender(m) in ("contact", "lead")


def _find_call_booking_index(messages: list[dict]) -> int | None:
    """
    Index where a call is booked/agreed — not where the agent merely offers a callback.
    """
    for i, m in enumerate(messages):
        body = _contact_body(m)
        if _is_contact(m) and _CONTACT_CALL_AGREE_RE.search(body):
            return i
        if not _is_contact(m) and _AGENT_CALL_BOOKING_RE.search(body):
            lookback = " ".join(
                _contact_body(messages[j])
                for j in range(max(0, i - 5), i)
                if _is_contact(messages[j])
            )
            if _CONTACT_CALL_AGREE_RE.search(lookback) or re.search(
                r"\b(yes|sure|ok|okay|sounds\s+good|that\s+works|perfect)\b", lookback, re.I
            ):
                return i
    return None


def _should_flag_call_without_prequal(messages: list[dict]) -> bool:
    """
    F6: call confirmed/booked with zero lead-supplied pillars beforehand.
    Agent offers like "I can give you a call later" do NOT qualify.
    """
    booking_idx = _find_call_booking_index(messages)
    if booking_idx is None:
        return False
    pillars = detect_gathered_pillars(messages[: booking_idx + 1])
    return len(pillars) == 0


# FLAG 13 patterns ─────────────────────────────────────────────────────────
# Contact stating a specific asking price (number, dollar amount, or range)
_CONTACT_PRICE_STATED_RE = re.compile(
    r"(\$\s?\d[\d,]*(?:\s*(?:k|K|thousand|hundred))?)"
    r"|(\b\d[\d,]*\s*(?:k|K|thousand)\b)"
    r"|\b(?:want|asking|ask|need|looking\s+for|take|accept)\s+\$?\s?\d",
    re.I,
)

# Sunk-cost context: dollar amount is a past expense/renovation, NOT an asking price.
# "I just spent $30k redoing the bathrooms" = renovation cost ≠ asking price.
_SUNK_COST_RE = re.compile(
    r"\b(spent|spend|spending|put\s+in(to)?|invest(ed|ing)?|cost(s|ed)?|paid|pay(ing)?"
    r"|redo(ing)?|renovate[sd]?|renovating|remodel(ed|ing)?|repair(ed|ing)?|fix(ed|ing)?"
    r"|upgrad(ed|ing)?|install(ed|ing)?|built?|add(ed|ing)?|replac(ed|ing)?)\b",
    re.I,
)

# Rental-income framing around a price ("$75k and I collect $900/mo rent").
# A price stated alongside rental yield is a DEAL signal, not an above-market quote —
# it must never trigger the above-market referral-exit flag.
_RENT_CONTEXT_RE = re.compile(
    r"\b(rent|rental|tenant|leas(e|ed|ing)|per\s+month|a\s+month|/\s?mo\b|monthly\s+income|cash\s+flow)\b",
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
    # Suppress F5 entirely if the contact re-engaged after the wrong-number
    # message — the agent is then expected to switch into funnel mode.
    if wrong_number_idx is not None and not contact_reengaged_after_wn(messages, wrong_number_idx):
        # Check if agent continued pitching after wrong number
        for later in messages[wrong_number_idx + 1:]:
            sender = (later.get("sender") or "").strip().lower()
            body = (later.get("message") or later.get("body") or "").strip().lower()
            if sender not in ("contact", "lead"):
                # Only flag if agent continued pitch keywords AND is NOT doing a referral close
                pitch_keywords = ["sell", "offer", "property", "home", "house", "price", "cash"]
                has_pitch = any(w in body for w in pitch_keywords)
                has_referral = REFERRAL_RE.search(body) or "someone" in body or "know" in body
                if has_pitch and not has_referral:
                    raw_flags.append("Continued original pitch after wrong number.")
                    break

    # ── FLAG 6: Agreed to call without pre-qualifying ────────────────
    if _should_flag_call_without_prequal(messages):
        raw_flags.append("Agreed to call without pre-qualifying.")

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
    # GUARD: a handoff/escalation is the CORRECT way to close an engaged lead —
    # not abandonment. Suppress F9 if the agent handed the lead off anywhere in
    # the thread, or the conversation is labeled as a successful push.
    agent_handed_off = any(
        _HANDOFF_RE.search(m.get("message") or m.get("body") or "")
        for m in agent_msgs
    )
    label_is_push = any(
        ("push" in l.lower() or "deal closed" in l.lower() or l.strip().lower() == "sold")
        for l in assigned_labels
    )
    contact_interested = _contact_showed_genuine_interest(contact_msgs)
    if contact_interested and messages and not agent_handed_off and not label_is_push:
        last_sender = (messages[-1].get("sender") or "").strip().lower()
        # Agent was last speaker AND no follow-up questions => ended prematurely
        if last_sender not in ("contact", "lead"):
            last_body = (messages[-1].get("message") or messages[-1].get("body") or "").strip().lower()
            # If agent's last message is short and doesn't continue the conversation
            if len(last_body) < 30 and not any(w in last_body for w in ["?", "call", "schedule", "when"]):
                raw_flags.append("Ended conversation after lead showed interest.")

    # ── Shared vars (used by FLAG 10, 11, 12, and result builder) ─────
    agent_text = " ".join(
        (m.get("message") or m.get("body") or "") for m in agent_msgs
    ).lower()
    # Pillars count only when the LEAD positively answered them — never when
    # the agent merely asked. Counting topic keywords across the whole
    # transcript used to inflate pillar_count and fire a false "all 4 pillars
    # gathered" flag.
    gathered_pillars = detect_gathered_pillars(messages)
    pillar_count = len(gathered_pillars)

    # ── FLAG 10: Pushed to close with zero property info ─────────────
    # Only flag on explicit contract/signing language — 'deal' and 'close' are too
    # common in normal real-estate texting to be reliable close-push indicators.
    has_close_push = any(w in agent_text for w in ["contract", "sign", "agreement", "closing date"])
    if has_close_push and pillar_count < 1:
        raw_flags.append("Pushed to close with zero property info.")

    # ── FLAG 11: Did not escalate after all 4 pillars gathered ───────
    _address_denied = any(
        _ADDRESS_DENIAL_RE.search((m.get("message") or m.get("body") or ""))
        for m in contact_msgs
    )
    if pillar_count >= 4 and not _address_denied:
        # Check if agent escalated (mentioned manager, appointment, etc.)
        escalation_re = re.compile(
            r"\b(manager|supervisor|appointment|schedule|set\s+up|escalat)\b", re.I
        )
        if not escalation_re.search(agent_text):
            raw_flags.append("Did not escalate after all 4 pillars gathered.")

    # ── FLAG 14: Address denial after pillar engagement ───────────────
    # Contact answered property questions (condition, repairs, etc.) but then
    # denied knowing the address when confirmed. Agent should have asked:
    # "Do you have the parcel number?" or "Which address is yours?"
    # Labeling this as Bluffer is WRONG — the contact may own a different property.
    if _address_denied and pillar_count >= 2:
        raw_flags.append(
            "Contact denied knowing the address after providing property details. "
            "Agent should have asked clarifying questions (parcel number, correct address) "
            "instead of closing the conversation. Label should be Potential or Undefined, not Bluffer."
        )


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
            # Skip if the amount is a sunk cost (renovation, repair, expense), not an asking price.
            # e.g. "I spent $30k redoing the bathrooms" must not count as a stated price.
            if _SUNK_COST_RE.search(body):
                continue
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


    # ── Classify agent messages (used for rebuttals_used field) ──────────────
    _msg_cls = classify_agent_messages(messages)

    # ── FLAG 15: Agent kept pushing after above-market price ──────────────────
    # Script: if price is above market, do the $1k referral close and END.
    # If the agent kept pushing (timeline/motivation/etc.) instead → violation.
    #
    # GUARD: T4 is the conservative tier and has NO comp/market-value data — it
    # cannot judge whether an ordinary price (e.g. $75k) is above market. Only
    # the unambiguous Scenario-G case (joke-tier $1M+ quote) is safe to flag here;
    # all other above-market judgments are deferred to Groq. Also suppress when
    # the contact framed the number around rental income — that is a yield/deal
    # signal, the price is almost certainly below market.
    _abv = detect_abv_mv_response(messages)
    if _abv["contact_stated_price"] and _abv["agent_kept_pushing"] and not _abv["agent_did_referral_close"]:
        _price = _abv["price_amount"] or 0
        _price_msg = ""
        if _abv["price_msg_index"] is not None:
            _pm = messages[_abv["price_msg_index"]]
            _price_msg = _pm.get("message") or _pm.get("body") or ""
        _rent_framed = bool(_RENT_CONTEXT_RE.search(_price_msg))
        if _price >= 1_000_000 and not _rent_framed:
            raw_flags.append(
                f"Agent kept pushing after above-market price (${_price:,.0f}) "
                "instead of doing the $1k referral close per script."
            )

    # ── Normalize through the whitelist ──────────────────────────────
    final_flags = normalize_red_flags(raw_flags)

    # ── C.7: drop flags reviewers have repeatedly marked invalid ──────
    # The dream worker writes `suppresses_t4_flags` onto learned rules when a
    # deterministic flag pattern is consistently rejected. Masking it here gives
    # the deterministic tier parity with the Groq learned-rules path. Zero-cost
    # when no such rules exist; never blocks generation on error.
    try:
        from ai.learned_rules import get_t4_suppressed_flags
        from ai.prefilter._guards import canon_flag_text
        _suppressed = get_t4_suppressed_flags()
        if _suppressed:
            before = len(final_flags)
            final_flags = [
                f for f in final_flags if canon_flag_text(f) not in _suppressed
            ]
            if len(final_flags) < before:
                logger.info(
                    f"[T4] {contact_name}: suppressed {before - len(final_flags)} "
                    f"flag(s) via learned feedback rules"
                )
    except Exception as e:
        logger.debug("[T4] learned-flag suppression skipped: %r", e)

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
        "pillars_gathered": sorted(gathered_pillars),
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
