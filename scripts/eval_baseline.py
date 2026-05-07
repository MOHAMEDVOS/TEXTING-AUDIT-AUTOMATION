# -*- coding: utf-8 -*-
"""
Baseline Evaluator: Expert rules-based analysis of 50 conversations.

Applies the exact same audit rules from ai/prompts.py SYSTEM_PROMPT
to produce a deterministic, rules-based baseline WITHOUT calling Groq.

This is the "ground truth" to evaluate T1 and T2 against.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION")

DATA_PATH = Path(r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION\scripts\eval_50_conversations.json")
BASELINE_PATH = Path(r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION\scripts\eval_baseline.json")

from ai.prefilter.tier1_phrases import (
    _OPT_OUT_PATTERNS, _WRONG_NUMBER_PATTERNS, _NOT_INTERESTED_PATTERNS,
    _SOLD_PATTERNS, _NOT_THIS_PERSON_PATTERNS, _AGENT_PITCH_AFTER_WN,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _split(messages):
    agent   = [m for m in messages if m["sender"] == "agent"]
    contact = [m for m in messages if m["sender"] == "contact"]
    return agent, contact

def _text(msgs):
    return " ".join(m.get("body","") for m in msgs)

def _body(m):
    return m.get("body","")


# ── Scenario / Interest detection ──────────────────────────────────────────────

def _is_wrong_number(contact_text):
    if any(p.search(contact_text) for p in _WRONG_NUMBER_PATTERNS):
        return True
    if any(p.search(contact_text) for p in _NOT_THIS_PERSON_PATTERNS):
        return True
    # Extra explicit patterns seen in the data
    if re.search(r"(not tom|not my (house|property|number)|i don.?t own|not (this|my) person|"
                 r"never heard of|i live in (fl|florida|california|texas|ny)|"
                 r"\[name\].*\[mobile\]|"
                 r"have not owned.{0,20}(year|month)|haven.?t owned|"
                 r"where is (it|located)|i don.?t know (the )?address|"
                 r"don.?t know (the )?address|what address)",
                 contact_text, re.I):
        return True
    return False

def _is_sold(contact_text):
    if any(p.search(contact_text) for p in _SOLD_PATTERNS):
        return True
    if re.search(r"(it.?s been sold|already sold|we sold|sold it|property.{0,10}sold)", contact_text, re.I):
        return True
    return False

def _is_opt_out(contact_text):
    """Explicit opt-out / DNC request."""
    if any(p.search(contact_text) for p in _OPT_OUT_PATTERNS):
        return True
    if re.search(r"(take (me|us) off|remove (me|us)|do not (call|text|contact)\b|"
                 r"don.?t (call|text|contact) (me|us)|stop (texting|calling|messaging)|"
                 r"unsubscribe|not (interested|doing this)|please (stop|don.?t)|"
                 r"i said no|take off (the |your )?list|i do not suffer)",
                 contact_text, re.I):
        return True
    return False

def _is_not_interested(contact_text):
    """Soft no — contact declined but no DNC demand."""
    if any(p.search(contact_text) for p in _NOT_INTERESTED_PATTERNS):
        return True
    if re.search(r"(\bno\b|no thanks|not interested|not for sale|house is not for sale|"
                 r"not selling|not at this time|not in the next|don.?t think so|"
                 r"we.?re (ok|okay|fine)|i.?m (ok|okay|fine)|nope|i don.?t believe so|"
                 r"no,.?not at this time|not now|not right now|not planning)",
                 contact_text, re.I):
        return True
    return False

def _is_strong_ni(contact_text):
    """Unambiguous NI/rejection that should override any positive signals."""
    return bool(re.search(
        r"(nothing about this.{0,20}interests me|"
        r"^Disliked\s|Disliked \"|"
        r"not actively trying to sell|"
        r"not going to waste my time|prove to me this is worth)",
        contact_text, re.I
    ))

def _is_interested(contact_text):
    """Contact showed positive interest/engagement in selling."""
    return bool(re.search(
        r"(\byes\b|\byep\b|\byeah\b|\bsure\b|\bok\b|ok\.?\s*$|sounds? good|"
        r"tell me more|how much|what.?s the price|interested|"
        r"\d{2,3}\s*k\s*(cash)?|cash\s*\$?\s*\d|"   # "175k cash", "$400k"
        r"i want to sell|we have|we.?re considering|just giving me a number|"
        r"absolutely|definitely|call me|hit me up|"
        r"adding \d+|asking \d+|highest amount|\$\d{4,}|"
        r"only in cash|yes we have|yep\b|sure!!)",
        contact_text, re.I
    ))

def _is_maybe(contact_text):
    """Soft maybe / later."""
    return bool(re.search(
        r"(maybe|possibly|might|could be|not yet|near future|"
        r"check back|possibly soon|in the future|down the road|"
        r"potentially|check back in a couple months)",
        contact_text, re.I
    ))

def _is_abv_mv(contact_text):
    """Contact stated price far above what buyer can pay."""
    # Ernie: "400k cash. No brokers, no agents"
    # Jeffrey: "Wouldn't take less than $1500000"
    if re.search(r"(400\s*k|500\s*k|\$?(250,000|300,000|350,000|400,000|500,000|1[,.]?500,000|1500000))", contact_text, re.I):
        return True
    if re.search(r"(have an agent|already listed|on the market|with a realtor|through agent)", contact_text, re.I):
        return True
    return False


def _classify_contact(contact_msgs):
    """Classify the net outcome of contact replies."""
    if not contact_msgs:
        return "silent"
    contact_text = _text(contact_msgs)

    # If contact sent only dots/punctuation (e.g. ".."), treat as neutral
    if re.match(r'^[\s\.\!\?]+$', contact_text.strip()):
        return "neutral"

    # Definitive exclusions first
    if _is_opt_out(contact_text):
        return "opt_out"
    if _is_wrong_number(contact_text):
        return "wrong_number"
    if _is_sold(contact_text):
        return "sold"

    # Strong, unambiguous NI signals that override any positive words
    if _is_strong_ni(contact_text):
        return "not_interested"

    # Now check positive signals
    if _is_abv_mv(contact_text):
        return "abv_mv"
    if _is_interested(contact_text):
        return "interested"
    if _is_maybe(contact_text):
        return "maybe"
    # General NI check last (catches 'no', 'not selling', etc.)
    if _is_not_interested(contact_text):
        return "not_interested"
    return "neutral"


def _detect_pillars(messages):
    """Detect which funnel pillars were gathered from lead responses."""
    contact_text = _text([m for m in messages if m["sender"] == "contact"]).lower()
    pillars = []
    if re.search(r"(needs? work|roof|repair|renovati|updated|upgraded|shower|new|condition|bed|bath|garage|lot|vacant|built)", contact_text):
        pillars.append("condition")
    if re.search(r"(\$?\d{2,3}\s*k|\d{5,6}|400\s*k|100,000|15k|price in mind|asking\b)", contact_text):
        pillars.append("asking_price")
    if re.search(r"(want to sell|need to move|passed away|deceased|divorce|relocat|downsiz|retirement|builder|not building|daughter|not mine|i am a builder)", contact_text):
        pillars.append("motivation")
    if re.search(r"(months?|weeks?|soon|ready|asap|spring|summer|fall|winter|year|couple months|down the road|in the future|near future)", contact_text):
        pillars.append("timeline")
    return pillars


def _label_correct(assigned_labels, outcome, pillars):
    """
    Determine if the assigned label is correct given the contact outcome.
    Returns (is_correct: bool, should_be: str)
    """
    assigned = ", ".join(assigned_labels).strip().lower() if assigned_labels else ""

    # Group definitions (lowercased)
    DRIP   = {"wl drip","ap drip","hl drip","reason fu","fu1","fu2","fu3",
               "fu1, wl drip","fu2, wl drip","fu3, wl drip",
               "fu1, hl drip","fu2, hl drip","fu3, hl drip","fu1, ap drip"}
    NI     = {"not interested","verified","verified, not interested",
              "not interested, verified","not interested+verified"}
    DNC    = {"do not call","dnc"}
    LEAD   = {"lead","new lead","potential","lead, pushed","pushed to client",
              "lead, pushed, pushed to client"}
    MAYBE  = {"maybe later"}
    WN     = {"wrong number"}
    SOLD   = {"sold","f-sold"}
    ABV    = {"abv mv","abv mv, verified","above market value"}

    def in_group(label, group):
        n = label.strip().lower()
        return n in group or any(part.strip() in group for part in n.split(","))

    if outcome == "silent":
        ok = in_group(assigned, DRIP)
        return ok, ("drip" if not ok else assigned)

    if outcome == "wrong_number":
        ok = in_group(assigned, WN)
        return ok, ("Wrong Number" if not ok else assigned)

    if outcome == "sold":
        # "Wrong Number" is also acceptable for sold/wrong person
        ok = in_group(assigned, WN) or in_group(assigned, SOLD)
        return ok, ("Wrong Number / Sold" if not ok else assigned)

    if outcome == "opt_out":
        ok = in_group(assigned, DNC) or in_group(assigned, NI)
        return ok, ("Do Not Call" if not ok else assigned)

    if outcome == "not_interested":
        ok = in_group(assigned, NI) or in_group(assigned, DNC)
        return ok, ("Not Interested" if not ok else assigned)

    if outcome == "maybe":
        ok = in_group(assigned, MAYBE)
        return ok, ("Maybe Later" if not ok else assigned)

    if outcome == "abv_mv":
        # abv_mv is acceptable as Abv MV, or as NI (manager's judgment call), or DNC for extreme cases
        ok = in_group(assigned, ABV) or in_group(assigned, NI) or in_group(assigned, DNC) or in_group(assigned, DRIP)
        return ok, ("Abv MV or Not Interested" if not ok else assigned)

    if outcome == "interested":
        # Contact showed interest — could be Lead/Drip depending on how far it got
        ok = in_group(assigned, LEAD) or in_group(assigned, DRIP)
        return ok, ("Lead or drip" if not ok else assigned)

    if outcome == "neutral":
        ok = in_group(assigned, DRIP) or in_group(assigned, NI)
        return ok, ("drip or Not Interested" if not ok else assigned)

    return True, assigned


# ── Red flag detection ─────────────────────────────────────────────────────────

def _opt_out_index(messages):
    """Return index of first opt-out message from contact, or None."""
    for i, m in enumerate(messages):
        if m["sender"] == "contact" and _is_opt_out(_body(m)):
            return i
    return None

def _wn_index(messages):
    """Return index of first wrong-number message from contact, or None."""
    for i, m in enumerate(messages):
        if m["sender"] == "contact":
            b = _body(m)
            if any(p.search(b) for p in _WRONG_NUMBER_PATTERNS) or \
               any(p.search(b) for p in _NOT_THIS_PERSON_PATTERNS) or \
               _is_wrong_number(b):
                return i
    return None

def _flag_opt_out_continued(messages):
    """F1: Agent sent 2+ messages after opt-out."""
    idx = _opt_out_index(messages)
    if idx is None:
        return False
    after = [m for m in messages[idx+1:] if m["sender"] == "agent"]
    return len(after) >= 2

def _flag_gave_up(messages, outcome):
    """F4: Contact declined and agent sent ZERO follow-up."""
    if outcome not in ("not_interested",):
        return False
    refusal_idx = None
    for i, m in enumerate(messages):
        if m["sender"] == "contact" and _is_not_interested(_body(m)):
            refusal_idx = i
            break
    if refusal_idx is None:
        return False
    after = [m for m in messages[refusal_idx+1:] if m["sender"] == "agent"]
    return len(after) == 0

def _flag_continued_after_wn(messages):
    """F5: Agent pitched property specifics again after wrong number."""
    idx = _wn_index(messages)
    if idx is None:
        return False
    after = [m for m in messages[idx+1:] if m["sender"] == "agent"]
    after_text = _text(after)
    # A referral-pivot is NOT this flag — it's the standard wrong-number close
    # Flag if agent continued pitching the ORIGINAL property
    if any(p.search(after_text) for p in _AGENT_PITCH_AFTER_WN):
        return True
    return False


# ── Main evaluator ─────────────────────────────────────────────────────────────

def evaluate_conversation(conv):
    messages = conv["messages"]
    contact_name = conv["contact_name"]
    assigned_labels = conv.get("assigned_labels") or []
    funnel_tier = conv.get("funnel_tier") or "MF"

    agent_msgs, contact_msgs = _split(messages)
    contact_text = _text(contact_msgs)
    agent_text   = _text(agent_msgs)

    # Classify
    outcome = _classify_contact(contact_msgs)
    pillars = _detect_pillars(messages)

    # Red flags
    red_flags = []
    if _flag_opt_out_continued(messages):
        red_flags.append("Continued texting after explicit opt-out.")
    if _flag_gave_up(messages, outcome):
        red_flags.append("Gave up after first no with zero rebuttal.")
    if _flag_continued_after_wn(messages):
        red_flags.append("Continued original pitch after wrong number.")
    if re.search(r"\b(fuck|shit|damn you|threat|lie|lied)\b", agent_text, re.I):
        red_flags.append("Used threatening, profane, or deceptive language.")

    # Label correctness
    lbl_ok, lbl_should_be = _label_correct(assigned_labels, outcome, pillars)
    assigned_flat = ", ".join(assigned_labels).strip() if assigned_labels else ""

    # Scores
    compliance        = 40 if "Continued texting after explicit opt-out." in red_flags else 100
    sentiment         = 90
    professionalism   = 95
    script_adherence  = max(20, 100 - len(red_flags) * 20)

    # Summary
    if outcome == "silent":
        summary = f"Scenario A, no funnel stage reached. Texter sent {len(agent_msgs)} outreach messages. {contact_name} never replied. No compliance issues."
    elif outcome == "wrong_number":
        summary = f"Scenario B, wrong number. {contact_name} indicated wrong person/number. Texter apologized and pivoted to referral close."
    elif outcome == "sold":
        summary = f"Scenario F, property sold. {contact_name} indicated property already sold."
    elif outcome == "opt_out":
        if "Continued texting after explicit opt-out." in red_flags:
            summary = f"Contact opted out. Texter continued messaging — COMPLIANCE VIOLATION."
        else:
            summary = f"Contact opted out. Texter stopped correctly."
    elif outcome == "not_interested":
        after_ct = sum(1 for m in messages[next((i for i,m in enumerate(messages) if m["sender"]=="contact" and _is_not_interested(_body(m))), len(messages)):] if m["sender"]=="agent")
        summary = f"Scenario A, no funnel stage. {contact_name} declined. Texter used {after_ct} rebuttal message(s)."
    elif outcome == "maybe":
        summary = f"Scenario A, wide funnel. {contact_name} said maybe later. Texter followed up."
    elif outcome == "abv_mv":
        summary = f"Scenario G, above market value. {contact_name} stated price/agent above buyer range."
    elif outcome == "interested":
        summary = f"Scenario A, {'narrow' if len(pillars)>=3 else 'middle'} funnel. {contact_name} showed interest. Gathered {len(pillars)} pillar(s): {pillars}."
    else:
        summary = f"Scenario A, {outcome}. Texter exchanged {len(messages)} messages with {contact_name}."

    return {
        "conversation_id": conv["conversation_id"],
        "contact_name": contact_name,
        "account_name": conv.get("account_name",""),
        "texter_name": conv.get("texter_name",""),
        "assigned_labels": assigned_labels,
        "outcome": outcome,
        "pillars_gathered": pillars,
        "compliance_score": compliance,
        "sentiment_score": sentiment,
        "professionalism_score": professionalism,
        "script_adherence_score": script_adherence,
        "red_flags": red_flags,
        "label_assigned": assigned_flat,
        "label_correct": lbl_ok,
        "label_should_be": lbl_should_be if not lbl_ok else assigned_flat,
        "summary": summary,
        "model_used": "baseline_expert",
    }


def main():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        conversations = json.load(f)

    print(f"Evaluating {len(conversations)} conversations...")
    print()

    results = []
    wrong_labels = []

    for i, conv in enumerate(conversations):
        r = evaluate_conversation(conv)
        results.append(r)

        flags = r["red_flags"]
        ok = "OK   " if r["label_correct"] else f"WRONG (should be: {r['label_should_be']})"
        flag_str = f"  FLAGS:{flags}" if flags else ""
        print(f"  {i+1:2d}. [{conv['conversation_id']}] {r['contact_name'][:28]:28s} "
              f"| outcome={r['outcome']:14s} | label={r['label_assigned'][:25]:25s} | {ok}{flag_str}")
        if not r["label_correct"]:
            wrong_labels.append(r)

    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    correct = sum(1 for r in results if r["label_correct"])
    print(f"\n{'='*70}")
    print(f"BATCH 1 (1-20)  : {sum(1 for r in results[:20] if r['label_correct'])}/20 correct")
    print(f"BATCH 2 (21-40) : {sum(1 for r in results[20:40] if r['label_correct'])}/20 correct")
    print(f"BATCH 3 (41-50) : {sum(1 for r in results[40:50] if r['label_correct'])}/10 correct")
    print(f"TOTAL           : {correct}/50 correct ({100*correct//50}%)")
    print(f"With red flags  : {sum(1 for r in results if r['red_flags'])}")
    print(f"\nBaseline saved to {BASELINE_PATH}")

    if wrong_labels:
        print(f"\n--- {len(wrong_labels)} label mismatches ---")
        for r in wrong_labels:
            print(f"  [{r['conversation_id']}] {r['contact_name'][:30]:30s} | outcome={r['outcome']:14s} "
                  f"| assigned='{r['label_assigned']}' -> should='{r['label_should_be']}'")


if __name__ == "__main__":
    main()
