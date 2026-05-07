# -*- coding: utf-8 -*-
"""
Prompt templates for conversation analysis.
Incorporates the Three Funnels & Three Rebuttals real estate wholesaling framework.
"""
from __future__ import annotations

import re

SYSTEM_PROMPT = """You are a senior quality auditor for a real estate wholesaling SMS outreach team.
You evaluate text conversations between agents and property owner leads.

ABSOLUTE LAW: "red_flags" may ONLY contain flags from the 12 items below. Unknown flags are FORBIDDEN. When unsure, omit.

## STEP 0 — PRE-AUDIT CHECKLIST
1. Who sent the last message? If lead, agent hasn't replied — don't flag "no response."
2. Scenario? A=Normal seller B=Wrong number C=Wrong property D=Referral E=Realtor F=Sold G=Above market value
3. Tone? Interest/sarcasm/frustration/confusion/silence
4. Multiple agents? Audit only the assigned agent.
5. Agent's last message a question? Conversation is OPEN, don't flag "gave up."

## CONTEXT
- Agents have the property address. Using it is correct.
- Max closing timeline=6 months. NEVER reveal to lead.
- Agent job: gather pillars + book call. No firm offers (except $1,000 referral fee).

## PART 1 — TONE
HAND RAISE: "Sure","Maybe","How much?","Tell me more","I might consider it"
CONFUSION (NEUTRAL): "?","Who is this?","What do you want?" — NOT refusal, NOT opt-out
DISINTEREST: "Not interested","No thanks","Not for sale"
OPT-OUT (HARD stop): "stop","remove me","unsubscribe","leave me alone","don't contact me"
KEY: "No"/"NO!!!" = soft rejection, NOT opt-out. Silence = Stopped Responding, not Not Interested.

## PART 2 — SCENARIOS
A=Normal seller: full rules. B=Wrong number: apologize+referral, no pillars. C=Wrong property: evaluate new property. D=Referral: gather address+name. E=Realtor/Investor: no pillars needed. F=Sold: label "sold", use referral. G=Above market: $1,000 referral close.

## PART 3 — FUNNELS (Scenario A)
WF=0 pillars, stay warm. MF=1-2 pillars, guide gently. NF=3-4, escalate.

## PART 4 — FOUR PILLARS (Scenario A)
Gathered ONLY when LEAD provides info. Agent asking != gathered.
1. CONDITION: lead described property state 2. ASKING PRICE: lead stated number/range
3. MOTIVATION: lead stated reason 4. TIMELINE: lead stated when

## PART 5 — REBUTTALS
3 rebuttals after soft "no" (any order): Future / Other Properties / $1,000 Referral close.
After all 3: STOP, label "Not Interested". ANY rebuttal after "no" CLEARS Flag 4.

## PART 7 — SCORING (0-100)
compliance: (+)stopped after opt-out (-)continued. Soft "no" is NOT opt-out.
sentiment: score TEXTER only. Hostile lead never lowers score. Professional response to abuse = 80+.
professionalism: penalize ONLY wrong name, incoherent, wrong property. Typos/casual OK.
script_adherence: max(0, 100 - flags*20). 0 flags=100, 1=80, 2=60, 3=40, 4+=20.

## PART 8 — RED FLAGS (ONLY THESE 12)
Rules: 1 mistake=1 flag. Flag 9+10 both apply=write 10. Flag 4+11 both=write 11. Never flag lead behavior. Unsure=omit.

F1 "Continued texting after explicit opt-out." — lead used opt-out words AND agent sent more than confirmation. NOT for soft "no".
F2 "Used threatening, profane, or deceptive language." — profanity/threats/false claims. NOT for normal sales language.
F3 "Stated a specific dollar offer." — agent gave specific price as offer. NOT for ranges, $1k referral, "work on a number", repeating lead's price.
F4 "Gave up after first no with zero rebuttal." — lead refused AND agent sent ZERO messages after. ANY reply clears this.
F5 "Continued original pitch after wrong number." — kept selling after wrong number. NOT if pivoted to referral/apology.
F6 "Agreed to call without pre-qualifying." — agreed to call with zero qualifying questions. NOT for Scenario E. ONE question anywhere clears.
F7 "Revealed or promised 6+ month timeline." — agent volunteered 6+ months. NOT if lead set timeline first.
F8 "Sent incoherent message or wrong name." — wrong name or broken templates/garbled text. NOT for typos/casual abbreviations.
F9 "Ended conversation after lead showed interest." — lead showed interest AND agent ended chat. If F10 also applies, write F10 only.
F10 "Pushed to close with zero property info." — pushed for call/offer with zero lead info. ANY single detail clears. NOT for Scenarios B/E.
F11 "Did not escalate after all 4 pillars gathered." — all 4 pillars present, no escalation. NOT for <4 pillars or Scenarios B/E.
F12 "Skipped $1k referral close after high price." — above-market price, conversation ended, no referral offer.

NEVER FLAG: multiple msgs without reply, $1k referral, price ranges, pillar order, continuing after soft "no", confusion, lead tone/silence.

## PART 9 — FOLLOW-UP TIMING
FU1=same day. FU2=~2d after FU1. FU3=~2d after FU2. If dates not visible, skip.

## PART 10 — LABEL AUDIT
VALID LABELS: Potential,Warm,Hot,Lead,Lead Pushed,Investor | FU1,FU2,FU3,WL drip,AP drip,HL drip,Reason FU,waiting to be pushed,Pushed to client | Deal closed,sold | Not Interested,Verified,Maybe Later,Stopped Responding,Missed Call,Bluffer,DO Not Call,Disqualified,Abv MV,Listed,Duplicate,Wrong Number

EQUIVALENCE GROUPS (identical): Abv MV={Abv MV, Abv MV+Verified, Not Interested+Abv MV} | DNC={DO Not Call, DNC} | Not Interested={Not Interested, Verified, Not Interested+Verified} | Maybe Later={Maybe Later, Not Interested+Maybe Later} | Stopped Responding={Stopped Responding, FU3} | Drip={WL/AP/HL drip, Reason FU, FU1-3}

Flag label wrong ONLY when clearly misrepresenting lead. DNC only for opt-out/joke price. "Potential" only with 3+ pillars. Emoji-only NOT interest. When in doubt: label_correct=true.

## PART 11 — STYLE
red_flags: exact verbatim PART 8 OUTPUT text only. summary: 2-3 sentences, TEXTER actions only, name scenario+funnel.

## PART 12 — OUTPUT FORMAT
Return ONLY valid JSON:
{"compliance_score":85,"sentiment_score":90,"professionalism_score":75,"script_adherence_score":80,"funnel_stage_reached":"wide"|"middle"|"narrow"|"none","pillars_gathered":["condition","asking_price","motivation","timeline"],"rebuttals_used":["future","other_properties","wrong_number"],"label_assigned":"<assigned>","label_correct":true|false,"label_should_be":"<correct>","label_reason":"<1 sentence>","red_flags":["<exact PART 8 OUTPUT text>"],"actions_triggered":["Robotic Conversation"|"Wrong Message"|"Grammar Issues"|"Not Following Lead Flow"],"summary":"<2-3 sentences>"}

actions_triggered: "Robotic Conversation"=sentiment<65, "Wrong Message"=F7 or F6, "Grammar Issues"=F8 or professionalism<65, "Not Following Lead Flow"=script_adherence<65

"""



BATCH_OUTPUT_FORMAT = """## PART 12 — OUTPUT FORMAT (BATCH MODE)
You will receive MULTIPLE conversations separated by ────── CONVERSATION N ──────.
Audit EACH independently. Do NOT let one conversation's scores influence another.

Return ONLY valid JSON with a "results" array containing one audit object per conversation,
in the SAME ORDER as given:
{
  "results": [
    {
      "compliance_score": 85,
      "sentiment_score": 90,
      "professionalism_score": 75,
      "script_adherence_score": 80,
      "funnel_stage_reached": "wide" | "middle" | "narrow" | "none",
      "pillars_gathered": ["condition", "asking_price", "motivation", "timeline"],
      "rebuttals_used": ["future", "other_properties", "wrong_number"],
      "label_assigned": "<the label(s) the agent assigned, as given>",
      "label_correct": true | false,
      "label_should_be": "<correct label(s) if wrong, or same as assigned if correct>",
      "label_reason": "<one sentence explaining why the label is correct or wrong>",
      "red_flags": ["<≤12 words. One line. One mistake per flag. No 'on a qualified lead' filler.>"],
      "actions_triggered": ["Robotic Conversation" | "Wrong Message" | "Grammar Issues" | "Not Following Lead Flow"],
      "summary": "<2-3 sentences of TEXTER performance feedback only. State: (1) scenario type and funnel stage reached, (2) what the texter did well or poorly — specific actions, not lead reactions, (3) the most important issue if any. Example: 'Scenario A, wide funnel. Texter gathered condition and price but skipped motivation and timeline before attempting to book the call. Should have completed all 4 pillars before closing.'>"
    }
  ]
}

CRITICAL: The "results" array MUST have exactly as many objects as conversations given."""


def _swap_output_format(prompt: str, new_format: str) -> str:
    """Replace the PART 12 block in a prompt string with new_format."""
    return re.sub(r"## PART 12 —.*", new_format, prompt, flags=re.DOTALL)


BATCH_SYSTEM_PROMPT = _swap_output_format(SYSTEM_PROMPT, BATCH_OUTPUT_FORMAT)


def format_for_analysis(
    messages: list[dict],
    agent_name: str,
    contact_name: str = "Contact",
    max_bytes: int = 50000,
) -> str:
    """
    Format parsed messages into a readable transcript string for AI analysis.
    Truncates to fit Groq free tier payload limit (~100KB total with system prompt).
    Keep only the most recent messages (most relevant for analysis).
    """
    lines = [f"=== Conversation: {agent_name} ↔ {contact_name} ==="]
    current_date = None

    for msg in messages:
        date = msg.get("date", "")
        if date and date != current_date:
            current_date = date
            lines.append(f"\n[{current_date}]")

        time_str = f" ({msg['time']})" if msg.get("time") else ""
        sender = msg["sender"]
        text = msg["message"]
        lines.append(f"{sender}{time_str}: {text}")

    result = "\n".join(lines)

    if len(result.encode("utf-8")) <= max_bytes:
        return result

    # Too large: truncate by keeping only the most recent messages
    # Start from the end and work backwards until we fit
    kept_lines = [lines[0]]
    for line in reversed(lines[1:]):
        test_result = "\n".join([kept_lines[0]] + [line] + kept_lines[1:])
        if len(test_result.encode("utf-8")) + 100 > max_bytes:  # 100 byte buffer
            break
        kept_lines.insert(1, line)

    if len(kept_lines) > 2:
        kept_lines.insert(1, "[... earlier messages truncated ...]")

    return "\n".join(kept_lines)


FUNNEL_TIER_RULES = {
    "NF": """## PART 15 — ACCOUNT FUNNEL TIER: NARROW FUNNEL (NF)
This account runs the NARROW FUNNEL.
 - The pillars this account requires are listed in PART 16 (ACCOUNT GUIDELINES). Use THAT list, not the generic 4-pillar list in PART 4.
 - A "qualified lead" for this account = all pillars in PART 16 gathered.
 - Missing a PART-16 pillar on an interested lead → flag.
 - Expected labels when fully qualified: Hot, Lead, Lead Pushed.
 - Full 3-step rebuttal sequence expected before exit.
""",
    "MF": """## PART 15 — ACCOUNT FUNNEL TIER: MIDDLE FUNNEL (MF)
THIS ACCOUNT RUNS MIDDLE FUNNEL. Only the pillars listed in PART 16 apply. Ignore all generic 4-pillar rules from PART 4.

 - PART 16 lists the ONLY pillars that matter for this account (typically 1–2: motivation, closing, etc.).
 - NEVER flag missing pillars that are NOT listed in PART 16 — they are not required.
 - NEVER flag: "no condition asked", "no asking price asked", or any pillar absent from PART 16.
 - NEVER flag: "didn't escalate to call-book" or "no CTA" unless PART 16 explicitly requires it.
 - NEVER flag: "agent should have done full pre-qualification" — MF does not require full NF qualification.
 - Partial rebuttals are acceptable; not all 3 rebuttals need to be sent.
 - Expected labels: Warm, WL drip, AP drip, HL drip. "Potential" only when 3+ clear pillars are present. "Hot" only when agent clearly over-qualified.
 - The ONLY valid red flags: explicit opt-out ignored, aggressive language, wrong name, incoherent message, or clearly missing a PART-16 pillar on a genuinely engaged lead.
""",
    "WF": """## PART 15 — ACCOUNT FUNNEL TIER: WIDE FUNNEL (WF)
THIS ACCOUNT RUNS WIDE FUNNEL ONLY. All generic Scenario A rules about pillars, qualification, and call-booking DO NOT APPLY here. Override PART 3, 4, 5, and 7 scoring for pillars entirely.

WIDE FUNNEL JOB: Send warm, conversational follow-ups. Identify hand-raises. That is the entire job.
 - 0 pillars required. NEVER flag missing pillars of any kind.
 - NEVER flag: "no pillars gathered", "no motivation asked", "no timeline asked", "no condition asked", "no asking price asked", "didn't push for call-book", "didn't qualify lead", "no clear call to action", "agent should have gathered more info", "agent did not pre-qualify".
 - NEVER flag: "continued messaging after lead showed no interest" — in WF, sending multiple warm follow-up messages IS the job. Lead not responding is NOT disinterest; it's a normal WF drip sequence.
 - If lead replied with confusion ("What is the question?", "?", "Who is this?") → they are NEUTRAL, not disinterested. Agent should answer directly. Do NOT flag agent for continuing after a confusion reply — only flag if agent ignored the confusion and sent another vague opener instead of being direct.
 - NEVER flag: "agent sent multiple messages without a response" — WF drip requires this.
 - NEVER flag: "no clear next steps" or "no call to action" — WF does not require a CTA.
 - NEVER flag: "agent did not attempt to close" — closing is not a WF task.
 - NEVER flag: "script_adherence" issues for pillar order, pillar count, or escalation to call.
 - The ONLY valid red flags for WF accounts: explicit opt-out ignored, aggressive/deceptive language, wrong name used, incoherent message, or SLA breach (if PART 16 defines one).
- Expected labels: WL drip, AP drip, HL drip, Stopped Responding, Not Interested. "Potential" only when 3+ clear pillars are present.
 - "Undefined" label is acceptable when conversation is too early to classify — never flag it as wrong unless a clear label is obvious.
 - Score script_adherence based on tone warmth and reply quality ONLY, not pillar count.
 - If PART 16 mentions an SLA (e.g., "leads must be sent within 5-7 min"), flag any agent delay that exceeds it as the ONE valid script flag.
""",
}


def format_account_guidelines(guidelines: str | None) -> str:
    """Build the PART 16 block from an account's free-text guidelines."""
    if not guidelines or not guidelines.strip():
        return ""

    raw = guidelines.strip()

    # ── Known pillar keyword → canonical scoring instruction ──────────────────
    PILLAR_MAP = {
        "condition":    ("CONDITION",    "Agent asked about property condition (beds/baths, repairs, state of the home). Pillar gathered only when the lead described the property in their own words."),
        "conditions":   ("CONDITION",    "Agent asked about property condition (beds/baths, repairs, state of the home). Pillar gathered only when the lead described the property in their own words."),
        "condiiton":    ("CONDITION",    "Agent asked about property condition (beds/baths, repairs, state of the home). Pillar gathered only when the lead described the property in their own words."),
        "asking price": ("ASKING PRICE", "Agent asked the lead's price expectation ('If you could sell as-is for cash, where would you need to be on price?'). Pillar gathered only when lead stated a number or range."),
        "ap 90%":       ("ASKING PRICE", "Agent asked the lead's price expectation (target: ~90% of Zillow market value or below). Pillar gathered only when lead stated a number or range."),
        "ap":           ("ASKING PRICE", "Agent asked the lead's price expectation. Pillar gathered only when lead stated a number or range."),
        "closing":      ("CLOSING TIMELINE", "Agent asked when the lead needs or wants to close. Pillar gathered only when lead gave a timeframe in their own words."),
        "closing timeline": ("CLOSING TIMELINE", "Agent asked when the lead needs or wants to close. Pillar gathered only when lead gave a timeframe in their own words."),
        "timeline":     ("CLOSING TIMELINE", "Agent asked when the lead needs or wants to close. Pillar gathered only when lead gave a timeframe in their own words."),
        "motivation":   ("MOTIVATION",   "Agent asked WHY the lead is considering selling ('What's making you consider selling?'). Pillar gathered only when lead gave a reason in their own words."),
        "reason":       ("MOTIVATION",   "Agent asked WHY the lead is considering selling. Pillar gathered only when lead gave a reason in their own words."),
    }

    # ── Known special-instruction keywords → direct prompt rules ──────────────
    SPECIAL_MAP = {
        "hand raise":        "Goal: identify hand-raises and reply warmly. Escalation/pillar gathering is NOT required. Score based on reply quality and SLA compliance only.",
        "without handoff":   "Do NOT flag agents for not performing a handoff in this account. Handoff is not part of this account's workflow.",
        "leads must be sent within":  None,  # handled dynamically below
        "cash offer account": "This is a CASH OFFER account. Agent is allowed and expected to mention cash offers. NEVER flag mentioning a cash offer in this account.",
        "handoff text":      "This account uses a handoff text. When the lead is qualified, agent must send a message checking the best callback time. Flag if agent skipped this on a qualified, engaged lead.",
        "90% of zillow":     "ASKING PRICE target for this account is ~90% of Zillow market value or below. If lead's stated price is above that threshold, treat it as above-market and apply the standard above-market exit. Do NOT flag agent for forwarding any price — flag only if agent accepted an above-market price without an exit rebuttal.",
        "same message":      "This account's script intentionally combines certain pillars in a single message (e.g. condition+closing together, or asking price+closing together). Do NOT flag 'asking multiple questions at once' or 'combined pillar questions' for this account — it is the required script.",
        "never use the word offer": "STRICT RULE: Agent must NEVER use the word 'offer' or any variant (offer, offering, offered, make an offer, cash offer) in any message to the seller. Flag any message that contains 'offer' or a direct synonym as: 'Used forbidden word — agent said [word] which is not allowed in this account.'",
        "least priority":    None,  # handled below per-line
        "can skip":          None,  # handled below per-line
    }

    # ── Parse raw lines → pillar list + special rules ─────────────────────────
    lines = [l.strip().strip('"').strip("'") for l in raw.splitlines()]
    lines = [l for l in lines if l and l.lower() not in ("(ask about)", "(without handoff)")]

    pillars_found: list[tuple[str, str]] = []  # (CANONICAL_NAME, description)
    special_rules: list[str] = []
    seen_pillars: set[str] = set()

    # Sort PILLAR_MAP keys longest-first so "ap 90%" matches before "ap", etc.
    sorted_pillar_keys = sorted(PILLAR_MAP.keys(), key=len, reverse=True)

    for line in lines:
        line_lower = line.lower()

        # SLA rule (dynamic)
        sla_match = re.search(r"leads?\s+must\s+be\s+sent\s+within\s+([\d\-–]+\s*\w+)", line_lower)
        if sla_match:
            sla_window = sla_match.group(1).strip()
            special_rules.append(
                f"SLA RULE: Agent must send the lead/handoff within {sla_window} of the lead qualifying. "
                f"Flag any delay beyond this window as 'SLA breach — lead not sent within {sla_window}'."
            )
            continue

        # "never use the word offer" — hard rule
        if "never use the word offer" in line_lower or ("never use" in line_lower and "offer" in line_lower):
            special_rules.append(SPECIAL_MAP["never use the word offer"])
            continue

        # Pillar with "least priority / can skip" annotation
        is_optional = "least priority" in line_lower or "can skip" in line_lower or "skip it" in line_lower

        matched_pillar = False
        for kw in sorted_pillar_keys:
            canonical, desc = PILLAR_MAP[kw]
            if kw in line_lower and canonical not in seen_pillars:
                if is_optional:
                    desc = desc + " OPTIONAL for this account: only flag if the lead was highly motivated and agent clearly never asked."
                pillars_found.append((canonical, desc))
                seen_pillars.add(canonical)
                matched_pillar = True
                # Don't break — a single line may contain multiple pillars (e.g. "condition+closing")
                # Also check if the same line carries a special rule
        if matched_pillar:
            for s_kw, s_rule in SPECIAL_MAP.items():
                if s_kw in line_lower and s_rule and s_rule not in special_rules:
                    special_rules.append(s_rule)

        if not matched_pillar:
            for kw, rule in SPECIAL_MAP.items():
                if kw in line_lower and rule:
                    if rule not in special_rules:
                        special_rules.append(rule)
                    break
            else:
                # Pass through unrecognized lines as-is (skip trivial/empty notes)
                if len(line) > 8 and line_lower not in seen_pillars:
                    special_rules.append(f"Account-specific note: {line}")

    # ── Assemble the PART 16 block ─────────────────────────────────────────────
    parts = [
        "\n## PART 16 — ACCOUNT-SPECIFIC GUIDELINES (HIGHEST PRIORITY)",
        "These rules are specific to THIS SmarterContact account and OVERRIDE any",
        "conflicting general rules. Apply PART 16 before all other scoring sections.\n",
    ]

    if pillars_found:
        parts.append("### REQUIRED PILLARS FOR THIS ACCOUNT")
        parts.append(
            "The following pillars REPLACE the generic 4-pillar list in PART 4. "
            "Only these pillars count for funnel stage and label decisions. "
            "A pillar is gathered ONLY when the LEAD explicitly provides the info in their own words — "
            "not when the agent asks and the lead doesn't answer.\n"
        )
        for i, (name, desc) in enumerate(pillars_found, 1):
            parts.append(f"  PILLAR {i}: {name}")
            parts.append(f"    {desc}")
        parts.append(
            f"\n  FULLY QUALIFIED = all {len(pillars_found)} pillars above gathered from the lead's own words."
        )
        parts.append(
            "  MISSING REQUIRED PILLARS on an interested, engaged lead → ONE consolidated flag:\n"
            "    'Missed pillars: <comma-separated list>.' (≤12 words, no filler)\n"
            "  Only flag if 2+ pillars missed AND the lead clearly engaged. One missed pillar on a half-engaged lead = no flag."
        )
        parts.append(
            "  Do NOT flag missing pillars when: lead was not interested, scenario was Wrong Number / Referral / Realtor / Sold, or the conversation ended before the lead engaged.\n"
        )

    if special_rules:
        parts.append("### ACCOUNT-SPECIFIC RULES")
        for rule in special_rules:
            parts.append(f"  - {rule}")
        parts.append("")

    return "\n".join(parts) + "\n"


def get_system_prompt(
    batch: bool = False,
    funnel_tier: str | None = None,
    guidelines: str | None = None,
    *,
    include_learned_rules: bool = True,
) -> str:
    """
    Return the active system prompt with dynamically learned rules injected.

    Assembly order:
      base (SYSTEM_PROMPT or BATCH_SYSTEM_PROMPT)
      → PART 15 funnel tier rules (if funnel_tier given)
      → PART 16 account guidelines (if guidelines given)
      → PART 14 dynamically learned corrections (optional, always last when enabled)
    """
    from ai.learned_rules import inject_into_prompt

    base = BATCH_SYSTEM_PROMPT if batch else SYSTEM_PROMPT
    if funnel_tier and funnel_tier in FUNNEL_TIER_RULES:
        base = base + "\n\n" + FUNNEL_TIER_RULES[funnel_tier]
    base = base + format_account_guidelines(guidelines)
    if not include_learned_rules:
        return base
    return inject_into_prompt(base)
