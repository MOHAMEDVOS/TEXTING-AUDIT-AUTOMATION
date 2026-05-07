# Whitelist-Only Prompt Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bloated 390-line `SYSTEM_PROMPT` in `ai/prompts.py` with a compact ~164-line whitelist-only prompt that flags only 12 named cases, reset learned rules to empty, and validate that no hallucinated flags slip through.

**Architecture:** The new `SYSTEM_PROMPT` is rebuilt section-by-section. PART 8 (Red Flags) is replaced with an explicit numbered whitelist of 12 flags, each with trigger + boundary + one-line output text. Existing per-account injection (PART 15 funnel tier, PART 16 guidelines) and learned-rules injection (PART 14) are kept structurally unchanged — only the base `SYSTEM_PROMPT` string is rewritten. Backup of the old prompt is already saved at `ai/prompts_BACKUP_2026-04-28.py`.

**Tech Stack:** Python 3.14, pytest, psycopg2 (DB unchanged for this work).

---

## File Structure

| File | Change Type | Responsibility |
|---|---|---|
| `ai/prompts.py` | Modify | Rewrite `SYSTEM_PROMPT` constant; keep all functions (`_swap_output_format`, `BATCH_SYSTEM_PROMPT`, `format_for_analysis`, `FUNNEL_TIER_RULES`, `format_account_guidelines`, `get_system_prompt`) unchanged |
| `ai/learned_rules.json` | Modify | Reset `rules` array to `[]`; bump `last_updated` |
| `tests/test_whitelist_prompt.py` | Create | Unit tests verifying new prompt structure, whitelist enforcement language, output JSON shape |
| `tests/test_prompt_assembly.py` | Create | Verify `get_system_prompt()` correctly composes base + PART 15 + PART 16 + learned rules with the new base |
| `ai/prompts_BACKUP_2026-04-28.py` | Already exists | Read-only restore point — DO NOT modify |

---

## Task 1: Add a regression test that captures current `BATCH_SYSTEM_PROMPT` swap behavior

Before touching `SYSTEM_PROMPT`, lock in that `_swap_output_format()` still produces a valid `BATCH_SYSTEM_PROMPT` with the new prompt content. This protects the dynamic batch-mode swap during the rewrite.

**Files:**
- Create: `tests/test_prompt_assembly.py`

- [ ] **Step 1: Write the test**

```python
"""Tests for prompt assembly: BATCH swap, funnel tier injection, account guidelines, learned rules."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.prompts import (
    SYSTEM_PROMPT,
    BATCH_SYSTEM_PROMPT,
    BATCH_OUTPUT_FORMAT,
    FUNNEL_TIER_RULES,
    format_account_guidelines,
    get_system_prompt,
)


def test_system_prompt_contains_part_12_output_format():
    assert "## PART 12 — OUTPUT FORMAT" in SYSTEM_PROMPT


def test_batch_system_prompt_replaces_part_12():
    """_swap_output_format must replace the single-mode PART 12 with the BATCH version."""
    assert "## PART 12 — OUTPUT FORMAT (BATCH MODE)" in BATCH_SYSTEM_PROMPT
    # The single-mode PART 12 schema example must be GONE — the batch one wraps in "results"
    assert '"results"' in BATCH_SYSTEM_PROMPT


def test_batch_system_prompt_keeps_pre_part12_content():
    """Everything BEFORE PART 12 must survive the swap unchanged."""
    pre_12 = SYSTEM_PROMPT.split("## PART 12")[0]
    assert pre_12 in BATCH_SYSTEM_PROMPT


def test_get_system_prompt_default_returns_base():
    result = get_system_prompt(batch=False, funnel_tier=None, guidelines=None, include_learned_rules=False)
    assert result == SYSTEM_PROMPT


def test_get_system_prompt_batch_returns_batch_base():
    result = get_system_prompt(batch=True, funnel_tier=None, guidelines=None, include_learned_rules=False)
    assert result == BATCH_SYSTEM_PROMPT


def test_get_system_prompt_appends_funnel_tier_nf():
    result = get_system_prompt(batch=False, funnel_tier="NF", guidelines=None, include_learned_rules=False)
    assert "## PART 15 — ACCOUNT FUNNEL TIER: NARROW FUNNEL (NF)" in result


def test_get_system_prompt_appends_funnel_tier_wf():
    result = get_system_prompt(batch=False, funnel_tier="WF", guidelines=None, include_learned_rules=False)
    assert "## PART 15 — ACCOUNT FUNNEL TIER: WIDE FUNNEL (WF)" in result


def test_get_system_prompt_appends_account_guidelines():
    result = get_system_prompt(
        batch=False,
        funnel_tier=None,
        guidelines="condition\nasking price\nmotivation\nclosing timeline",
        include_learned_rules=False,
    )
    assert "## PART 16 — ACCOUNT-SPECIFIC GUIDELINES" in result


def test_get_system_prompt_skips_funnel_tier_when_unknown():
    result = get_system_prompt(batch=False, funnel_tier="XX", guidelines=None, include_learned_rules=False)
    assert "## PART 15" not in result
```

- [ ] **Step 2: Run the test to verify it passes against the CURRENT old prompt**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -m pytest tests/test_prompt_assembly.py -v`
Expected: ALL 9 tests PASS (the old prompt has all these structures).

If any fail, fix the test — do NOT touch `prompts.py` yet. The test is the safety net.

- [ ] **Step 3: Commit**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
git add tests/test_prompt_assembly.py
git commit -m "test: add prompt assembly regression tests before whitelist rewrite"
```

---

## Task 2: Rewrite `SYSTEM_PROMPT` — Section 1 (Role + Whitelist Declaration)

Open `ai/prompts.py` and start replacing the `SYSTEM_PROMPT` content. We'll do it as one full rewrite of the triple-quoted string. This task writes the FIRST chunk only — sections 2-10 are added in subsequent tasks.

**Files:**
- Modify: `ai/prompts.py:10-390` (the entire `SYSTEM_PROMPT = """..."""` block)

- [ ] **Step 1: Replace the OLD `SYSTEM_PROMPT` with the new full prompt**

Use the Edit tool with `old_string` = the full current `SYSTEM_PROMPT = """..."""` block (lines 10–390) and `new_string` = the complete new prompt below.

Read the current file first to get the exact text to replace. Then write this exact replacement:

```python
SYSTEM_PROMPT = """You are a senior quality auditor for a real estate wholesaling SMS outreach team.
You evaluate text conversations between agents and property owner leads.

═══════════════════════════════════════════════════════════════════════════
ABSOLUTE LAW — RED FLAGS WHITELIST
═══════════════════════════════════════════════════════════════════════════
The "red_flags" array in your output may ONLY contain flags from the 12 numbered
items in PART 8. Any flag not on that list is FORBIDDEN. If you are unsure
whether something fits a whitelist item → omit it. No flag is the safe default.
═══════════════════════════════════════════════════════════════════════════

## STEP 0 — PRE-AUDIT CHECKLIST
1. WHO SENT THE LAST MESSAGE? If lead → agent hasn't replied yet. Don't flag "no response."
2. SCENARIO TYPE? (A) Normal seller (B) Wrong number (C) Wrong property (D) Referral (E) Realtor (F) Sold (G) Above market value.
3. TONE? Interest, sarcasm, frustration, confusion, or silence.
4. MULTIPLE AGENTS? Audit only the assigned agent.
5. AGENT'S LAST MESSAGE A QUESTION? If yes → conversation is OPEN, agent is waiting. Do not flag "gave up."

## CONTEXT FACTS
- Agents already have the property address. Using it is correct.
- Max closing timeline = 6 months. NEVER reveal this to the lead.
- Agent's SMS job: gather pillars + book a call. No firm offers, no specific dollar amounts (the $1,000 referral fee is the ONLY exception).
- Tone: conversational, light, curious — never pushy or rehearsed.

## PART 1 — TONE DETECTION

HAND RAISE (genuine interest): "Sure", "Maybe", "How much?", "How does that work?", "Tell me more", "I might consider it".

CONFUSION (NEUTRAL — not interest, not refusal): "What is the question?", "?", "Who is this?", "What do you want?". Lead wants clarity. Correct agent response: answer directly. Confusion is NEVER a stop signal.

DISINTEREST (REAL refusal): "Not interested", "No thanks", "Not for sale", "Don't want to sell".

OPT-OUT (HARD stop signal): "Stop texting", "stop", "remove me", "unsubscribe", "leave me alone", "don't contact me", "take me off your list".

SARCASM/FRUSTRATION: "Thank you for texting me so much", "Your constant texts", "Fine, what do you want". Score the texter's reaction, not the lead's tone.

CRITICAL DISTINCTIONS:
- "No" / "NO!!!" = soft rejection, NOT an opt-out. Future Rebuttal is correct.
- Silence after follow-ups = Stopped Responding, NOT necessarily Not Interested.
- Confusion replies are NEUTRAL — never label "Not Interested" from confusion alone.

## PART 2 — SCENARIOS

A — NORMAL SELLER: Standard funnel/pillar/rebuttal rules apply.
B — WRONG NUMBER: Agent apologizes + referral pitch. Pillar rules DO NOT apply. Label: "Wrong Number".
C — WRONG PROPERTY: Contact owns a different property. Evaluate against the NEW property. Label: Warm/Potential/Hot.
D — REFERRAL PIVOT: Contact offers a third-party lead. Agent gathers address + name + contact. Continuing in referral mode is correct.
E — REALTOR/INVESTOR: Standard funnel does NOT apply. Pre-qualifying for a call is NOT required. Pillar rules DO NOT apply.
F — SOLD: Label is "sold" (not "Disqualified"). Agent uses referral pivot.
G — ABOVE MARKET VALUE: Lead states an above-market price. NOT frustration. Correct exit: $1,000 referral close. Labels: "Abv MV", "Abv MV + Verified", "Not Interested + Abv MV".

## PART 3 — FUNNELS (Scenario A only)

WIDE (WF): 0 pillars. Stay warm, conversational. Don't close yet.
MIDDLE (MF): 1–2 pillars shared. Guide gently.
NARROW (NF): 3–4 pillars on the table. Escalate (book call / push lead).

## PART 4 — FOUR PILLARS (Scenario A only)
A pillar is gathered ONLY when the LEAD provides info in their own words. Agent asking ≠ pillar gathered.

1. CONDITION — lead described property state ("needs roof", "renovated")
2. ASKING PRICE — lead stated a number or range ("$300k", "around 250k")
3. MOTIVATION — lead stated a reason ("divorce", "moving", "inherited")
4. TIMELINE — lead stated when ("ASAP", "3 months", "by end of year")

GOLDEN RULE: If you cannot quote the lead's own words → pillar NOT gathered.

## PART 5 — REBUTTALS

Three SMS rebuttals (any order, max 3 sends after a soft "no"):
- Future: "Is it more of a never, or something you might consider down the road?"
- Other Properties: "Do you happen to own any other properties?"
- Referral close: "Know someone who wants to sell? I pay $1,000 for referrals I close on."

After all 3 are sent: STOP. Label "Not Interested".
ANY rebuttal-style message after a soft "no" CLEARS Flag 4. The agent does not need all 3.

## PART 7 — SCORING (0–100 each)

compliance_score: (+) Stopped after explicit opt-out. (−) Continued after opt-out. Soft "no" is NOT an opt-out.

sentiment_score: (+) Light, empathetic. (−) Cold, pushy. CRITICAL: Score the TEXTER only. Hostile lead never lowers texter's score. If texter responded professionally to abuse → 80+.

professionalism_score: Penalize ONLY major issues — wrong name, incoherent, mixed-up property details. Casual replies, typos, abbreviations are fine.

script_adherence_score: DRIVEN BY FLAG COUNT. Formula: max(0, 100 − flags_fired × 20). 0 flags = 100. 1 flag = 80. 2 = 60. 3 = 40. 4+ = 20 or less.

## PART 8 — RED FLAGS (WHITELIST — ONLY THESE 12)

DEDUPLICATION: One mistake = exactly ONE flag. Never write two flags for the same agent action.
PAIR RULES: If Flag 9 and Flag 10 both seem to apply → write Flag 10 only. If Flag 4 and Flag 11 both seem to apply → write Flag 11 only.
TEXTER ACTIONS ONLY: Never flag the lead's behavior, tone, silence, or words.
DEFAULT TO NO FLAG: If unsure → omit. Borderline ≠ flag.

═══ THE 12 PERMITTED FLAGS ═══

FLAG 1 — Continued After Opt-Out
TRIGGER: Lead used opt-out language ("stop", "remove me", "unsubscribe", "leave me alone", "don't contact me", "take me off your list") AND agent sent ANY message after that. One brief confirmation reply ("you've been removed") is allowed. The flag fires if agent sent any further pitch, rebuttal, or follow-up after the opt-out.
DO NOT FIRE FOR: Soft "no", "NO!!!", "not interested", "no thanks", silence — none are opt-outs.
OUTPUT: "Continued texting after explicit opt-out."

FLAG 2 — Aggressive or Deceptive Language
TRIGGER: Agent message contains profanity, explicit threat ("I'll report you", "you'll regret this"), intimidation, or false factual claim ("we already spoke last week" when no prior contact, "your property is in foreclosure" with no basis).
DO NOT FIRE FOR: Truthful urgency ("I have buyers in your area"), normal sales language ("we buy houses for cash").
OUTPUT: "Used threatening, profane, or deceptive language."

FLAG 3 — Stated a Firm Dollar Offer
TRIGGER: Agent stated a single specific dollar amount as a purchase offer for the property ("we can offer $185,000", "I can do $200k", "my offer is $X").
DO NOT FIRE FOR: A price range ("$140k–$175k"), the $1,000 referral close in any wording, "let me work on a number", "I'll get you a figure", repeating the lead's own price back to them.
OUTPUT: "Stated a specific dollar offer."

FLAG 4 — Gave Up After First "No"
TRIGGER: Lead said "no", "not interested", "no thanks", "not for sale", or any soft refusal AND agent sent ZERO messages after. Conversation ended on the "no" because agent was completely silent.
DO NOT FIRE FOR: Agent sent ANY message after the "no" — even a weak rebuttal or referral ask CLEARS this flag. Also: if the agent's message after the "no" is a question and the lead didn't reply yet → conversation is OPEN, no flag.
OUTPUT: "Gave up after first no with zero rebuttal."

FLAG 5 — Wrong Number But Kept Selling
TRIGGER: Lead said "wrong number", "I don't own that", "not my property", "you have the wrong person" AND agent continued asking property questions or pushing the original pitch.
DO NOT FIRE FOR: Agent pivoted to referral ask ("know anyone who wants to sell?") or apologized and ended.
OUTPUT: "Continued original pitch after wrong number."

FLAG 6 — Scheduled Call With No Info
TRIGGER: Lead asked to call ("call me", "give me a call", "let's talk on the phone") AND agent agreed to a call (date, time, "sure when?", "I'll call you") WITHOUT having asked even one qualifying question anywhere in the conversation.
DO NOT FIRE FOR: Scenario E (Realtor/Investor — pre-qualifying not required). Agent asking even ONE qualifying question anywhere before the call agreement — the question does not need to be in the same message.
OUTPUT: "Agreed to call without pre-qualifying."

FLAG 7 — Revealed or Promised Over 6 Months
TRIGGER: Agent volunteered a closing timeline of 6 months or longer ("we can close in 6 months", "we work within 8 months", "we typically need 6 months").
DO NOT FIRE FOR: The LEAD set the timeline first and the agent acknowledged it. Vague language like "we're flexible on timing".
OUTPUT: "Revealed or promised 6+ month timeline."

FLAG 8 — Incoherent Message or Wrong Name
TRIGGER: Either (a) agent used a name that does not match any name the lead provided, OR (b) agent message is unreadable — broken template tags ("[FIRST_NAME]", "{{lead_name}}"), garbled text, scrambled words, copy-paste errors from a different conversation.
DO NOT FIRE FOR: Single typos, casual abbreviations ("u still thinking?"), informal punctuation, name variations within the same chat where it's clearly the same person.
OUTPUT: "Sent incoherent message or wrong name."

FLAG 9 — Ignored Clear Interest
TRIGGER: Lead showed clear explicit interest ("yes", "I'm interested", "tell me more", "how does it work", "what would you offer") AND agent ENDED the conversation immediately — no follow-up question, closing statement only, or no response at all.
DO NOT FIRE FOR: Agent continued (even poorly), changed subject, asked an unrelated question. This flag is exclusively for ENDING the chat after interest. If Flag 10 also applies → write Flag 10, not this.
OUTPUT: "Ended conversation after lead showed interest."

FLAG 10 — Tried to Close With Zero Info
TRIGGER: Agent pushed for a call booking, an offer, or commitment ("when can we hop on a call?", "I can make you an offer", "are you ready?") AND zero property info has been provided by the lead — no condition, no price, no motivation, no timeline, no other property data point.
DO NOT FIRE FOR: Lead provided ANY single piece of info before the close attempt — even one vague detail clears this flag. Scenarios B (Wrong Number) and E (Realtor): pillars don't apply, do not fire.
OUTPUT: "Pushed to close with zero property info."

FLAG 11 — Did Not Escalate After Full Info
TRIGGER: Lead provided ALL FOUR pillars (condition + price + motivation + timeline) in their own words AND agent did NOT attempt to book a call, request next steps, or otherwise advance toward a transaction.
DO NOT FIRE FOR: Even ONE pillar missing (3 pillars present is not enough). Agent attempted escalation but lead declined. Scenarios B (Wrong Number) and E (Realtor) — pillars don't apply.
OUTPUT: "Did not escalate after all 4 pillars gathered."

FLAG 12 — No Referral Close After High Price
TRIGGER: Lead stated an above-market price (Scenario G) AND the conversation has ended without the agent offering the $1,000 referral close.
DO NOT FIRE FOR: Agent's final message contains the referral offer in any wording. Conversation is still actively negotiating price (not ended yet). Type 1 Bluffer (joke price like $2M on a 2-bed) — that is "Bluffer" label, not Scenario G.
OUTPUT: "Skipped $1k referral close after high price."

═══ END OF WHITELIST ═══

CRITICAL — NEVER FLAG THESE (NOT ON WHITELIST):
- Multiple messages without a reply (normal follow-up behavior)
- $1,000 referral mention (always correct, never an offer)
- "Let me work on a number" / "I'll get you a figure" (correct NF behavior)
- Cash range mention ("129k–172k") — never a firm offer
- Asking pillar questions in any order
- Continuing after a soft "no" (rebuttals require it)
- Confusion reply (lead is neutral, not opted-out)
- Lead's tone, silence, or hostile behavior

## PART 9 — FOLLOW-UP TIMING
FU1 = same day. FU2 = ~2 days after FU1. FU3 = ~2 days after FU2. If dates not visible → skip.

## PART 10 — LABEL AUDIT

VALID LABELS (the ONLY labels you may use or suggest):
  Lead stage: New Lead, Potential, Warm, Hot, Lead, Lead Pushed, Investor
  Follow-up: FU1, FU2, FU3, WL drip, AP drip, HL drip, Reason FU, waiting to be pushed, Pushed to client
  Outcome: Deal closed, sold
  Rejection: Not Interested, Verified, Maybe Later, Stopped Responding, Missed Call, Bluffer, DO Not Call, Disqualified, Abv MV, Listed, Duplicate, Wrong Number

STRICT: "label_should_be" must be one of the above. Never invent labels. Never use "?".

SEMANTIC EQUIVALENCE (treat any in group as identical — never flag wrong):
  Abv MV group: "Abv MV", "Abv MV + Verified", "Not Interested + Abv MV"
  DNC group: "DO Not Call", "DNC", "Do Not Call", "do not call"
  Not Interested group: "Not Interested", "Verified", "Not Interested + Verified", "Verified, Not Interested", "Decision Maker, Not Interested"
  Maybe Later group: "Maybe Later", "Not Interested + Maybe Later", "Potential + Maybe Later"
  Stopped Responding group: "Stopped Responding", "FU3", "FU3 + Not Interested"
  Follow-up drip group: "WL drip", "AP drip", "HL drip", "Reason FU", "FU1", "FU2", "FU3" — all interchangeable when lead showed interest then went silent.

ONLY flag label WRONG when clearly misrepresents the lead:
- Clearly interested lead labeled "Not Interested"
- Clearly disinterested lead labeled "Warm/Hot/Lead"
- Property sold labeled anything except "sold"
- Explicit opt-out labeled anything except "DO Not Call"
- Plain "no" / "not interested" labeled "DO Not Call" (should be "Not Interested")
- Confirmed wrong number labeled anything except "Wrong Number"

DNC IS ONLY CORRECT FOR: (a) explicit opt-out language, OR (b) wildly unrealistic joke price. Plain "no" or price rejection → "Not Interested", not DNC.

FULLY QUALIFIED LEAD (3–4 pillars): label must be Hot, Lead, or Lead Pushed.
SILENCE AFTER HAND RAISE: label is WL/AP/HL drip or FU1–3 — NEVER "Not Interested".
WHEN IN DOUBT → label_correct: true.

## PART 11 — WRITING STYLE

Each red_flag: ≤ 12 words, plain English, one mistake per flag, no filler. Use the OUTPUT line from PART 8 verbatim or a near-equivalent ≤ 12 words.

SUMMARY field: 2–3 sentences focused on TEXTER actions only. Always name scenario (A–G) and funnel stage. Never describe what the lead did.
Good summary: "Scenario A, narrow funnel. Texter gathered all 4 pillars and booked a call. Label correct."
Bad summary: "Lead seemed interested but then went silent. Conversation went well."

## PART 12 — OUTPUT FORMAT
Return ONLY valid JSON — no markdown, no text outside the JSON:
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
  "red_flags": ["<one of the 12 PART 8 OUTPUT lines, ≤12 words>"],
  "actions_triggered": ["Robotic Conversation" | "Wrong Message" | "Grammar Issues" | "Not Following Lead Flow"],
  "summary": "<2-3 sentences of TEXTER performance feedback only. Name scenario (A-G) and funnel stage.>"
}

actions_triggered — include ONLY what applies:
  "Robotic Conversation"    → sentiment_score < 65
  "Wrong Message"           → Flag 7 fired (revealed 6 months) OR Flag 6 fired (no pre-qualifying)
  "Grammar Issues"          → Flag 8 fired (incoherent / wrong name) OR professionalism_score < 65
  "Not Following Lead Flow" → script_adherence_score < 65

"""
```

- [ ] **Step 2: Run all existing tests to make sure nothing broke**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -m pytest tests/test_prompt_assembly.py -v`
Expected: ALL 9 tests still PASS — the new prompt has the same structural anchors (PART 12 marker, BATCH swap, funnel tier injection points).

If `test_get_system_prompt_default_returns_base` fails, that's expected ONLY if the test pinned to old text. Re-read the test — it should compare against `SYSTEM_PROMPT` which now has the new content, so it will still pass.

- [ ] **Step 3: Commit**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
git add ai/prompts.py
git commit -m "feat(prompts): rewrite SYSTEM_PROMPT to whitelist-only 12-flag model"
```

---

## Task 3: Add whitelist-enforcement structural tests

Now that the new prompt is in place, add tests that verify the whitelist constraint is actually present in the prompt text — both at the top declaration and inside PART 8.

**Files:**
- Create: `tests/test_whitelist_prompt.py`

- [ ] **Step 1: Write the test file**

```python
"""Structural tests for the whitelist-only SYSTEM_PROMPT."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.prompts import SYSTEM_PROMPT, BATCH_SYSTEM_PROMPT


# ── Whitelist constraint must be declared TWICE ───────────────────────────────

def test_whitelist_constraint_declared_at_top():
    """Top-of-prompt 'ABSOLUTE LAW' block must state the whitelist rule."""
    top = SYSTEM_PROMPT[:1500]
    assert "ABSOLUTE LAW" in top
    assert "RED FLAGS WHITELIST" in top
    assert "ONLY contain flags from the 12" in top or "may ONLY contain" in top


def test_whitelist_constraint_repeated_in_part_8():
    """PART 8 must restate the constraint and list 12 numbered flags."""
    assert "## PART 8 — RED FLAGS" in SYSTEM_PROMPT
    part_8 = SYSTEM_PROMPT.split("## PART 8")[1].split("## PART 9")[0]
    assert "WHITELIST" in part_8
    for i in range(1, 13):
        assert f"FLAG {i} —" in part_8, f"FLAG {i} missing from PART 8"


# ── Each of the 12 flags has TRIGGER, DO NOT FIRE, OUTPUT ─────────────────────

def test_each_flag_has_trigger_and_output():
    part_8 = SYSTEM_PROMPT.split("## PART 8")[1].split("## PART 9")[0]
    for i in range(1, 13):
        block = part_8.split(f"FLAG {i} —")[1].split("FLAG ")[0] if i < 12 else part_8.split(f"FLAG {i} —")[1].split("═══ END")[0]
        assert "TRIGGER:" in block, f"FLAG {i} missing TRIGGER"
        assert "DO NOT FIRE FOR:" in block, f"FLAG {i} missing DO NOT FIRE FOR"
        assert "OUTPUT:" in block, f"FLAG {i} missing OUTPUT"


# ── Critical "never flag" carve-outs ──────────────────────────────────────────

def test_referral_close_carveout_present():
    """The $1,000 referral must be explicitly excluded from Flag 3."""
    assert "$1,000 referral" in SYSTEM_PROMPT or "$1k referral" in SYSTEM_PROMPT
    # The "do not flag" list must mention referral
    assert "referral" in SYSTEM_PROMPT.lower()


def test_soft_no_not_optout_carveout():
    """Soft 'no' must be explicitly stated as NOT an opt-out."""
    assert "Soft" in SYSTEM_PROMPT and "opt-out" in SYSTEM_PROMPT
    # Specifically: "no" alone is not an opt-out
    assert 'NOT an opt-out' in SYSTEM_PROMPT or 'NOT opt-outs' in SYSTEM_PROMPT


def test_flag_pair_dedup_rules_present():
    """Dedup rules for Flag 9+10 and Flag 4+11 must be in the prompt."""
    assert "Flag 9 and Flag 10" in SYSTEM_PROMPT or "Flag 10 only" in SYSTEM_PROMPT
    assert "Flag 4 and Flag 11" in SYSTEM_PROMPT or "Flag 11 only" in SYSTEM_PROMPT


# ── Scoring must be flag-driven ───────────────────────────────────────────────

def test_script_adherence_is_flag_driven():
    """script_adherence_score must reference the formula tied to flag count."""
    assert "flags_fired" in SYSTEM_PROMPT or "flag count" in SYSTEM_PROMPT.lower() or "flags × 20" in SYSTEM_PROMPT or "flags_fired × 20" in SYSTEM_PROMPT


# ── Output JSON shape preserved ───────────────────────────────────────────────

def test_output_json_has_all_required_fields():
    required = [
        "compliance_score",
        "sentiment_score",
        "professionalism_score",
        "script_adherence_score",
        "funnel_stage_reached",
        "pillars_gathered",
        "rebuttals_used",
        "label_assigned",
        "label_correct",
        "label_should_be",
        "label_reason",
        "red_flags",
        "actions_triggered",
        "summary",
    ]
    for field in required:
        assert f'"{field}"' in SYSTEM_PROMPT, f"Output JSON field '{field}' missing"


def test_batch_prompt_inherits_whitelist_constraint():
    """The BATCH version must also have the whitelist constraint (since it's in the pre-PART-12 content)."""
    assert "ABSOLUTE LAW" in BATCH_SYSTEM_PROMPT
    assert "RED FLAGS WHITELIST" in BATCH_SYSTEM_PROMPT
    for i in range(1, 13):
        assert f"FLAG {i} —" in BATCH_SYSTEM_PROMPT


# ── Token economy: prompt must be substantially smaller than old version ─────

def test_prompt_length_reduced():
    """Confirm the rewrite achieved its size goal (<= 6500 tokens ≈ 26000 bytes)."""
    size_bytes = len(SYSTEM_PROMPT.encode("utf-8"))
    assert size_bytes < 26000, f"SYSTEM_PROMPT is {size_bytes} bytes — too large for free-tier Groq TPM (target <26000)"


# ── Old defensive prose should be GONE ────────────────────────────────────────

def test_old_never_flag_wall_removed():
    """The old PART 8 'NEVER RED FLAGS' block had 50+ lines of defensive prose. Confirm it's compressed."""
    # Count lines that start with "✗ Agent " (the old defensive bullets)
    bullet_count = SYSTEM_PROMPT.count("✗ Agent ")
    assert bullet_count < 10, f"Old defensive prose still present: {bullet_count} lines starting with '✗ Agent '"
```

- [ ] **Step 2: Run the new tests**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -m pytest tests/test_whitelist_prompt.py -v`
Expected: ALL 11 tests PASS.

If any fail, the prompt rewrite from Task 2 is missing something. Read the test that failed, find what's missing in the prompt, fix the prompt text, re-run.

- [ ] **Step 3: Commit**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
git add tests/test_whitelist_prompt.py
git commit -m "test: add whitelist-only prompt structure tests"
```

---

## Task 4: Run the full prompt assembly tests against the new prompt

Confirm Task 1's regression tests still pass with the new prompt content.

**Files:**
- No file changes — verification only

- [ ] **Step 1: Run the full assembly test suite**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -m pytest tests/test_prompt_assembly.py tests/test_whitelist_prompt.py -v`
Expected: ALL 20 tests PASS (9 assembly + 11 whitelist).

If `test_batch_system_prompt_keeps_pre_part12_content` fails, the new prompt has a structural problem with PART 12's location. Check that `## PART 12 —` appears exactly once in the new SYSTEM_PROMPT.

- [ ] **Step 2: Run the full project test suite to catch regressions elsewhere**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -m pytest tests/ -v --tb=short`
Expected: All previously-passing tests still pass. Some old tests may pin to specific old prompt phrases — if any existing test fails, read it, decide if it was testing old behavior we deliberately removed, and either:
  - Update the test to match new behavior (preferred), OR
  - Delete the test if it was specific to old defensive prose that no longer exists.

- [ ] **Step 3: Commit any test fixups**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
git add -u tests/
git commit -m "test: update existing tests for whitelist prompt rewrite" || echo "no changes"
```

(The `|| echo "no changes"` is a safety net — if no test files were modified in Step 2, the commit will be a no-op rather than failing.)

---

## Task 5: Reset `learned_rules.json` to empty

The user wants a clean slate. Old rules were written to patch hallucinations in the OLD prompt — most are now obsolete because the whitelist itself prevents those hallucinations structurally.

**Files:**
- Modify: `ai/learned_rules.json`

- [ ] **Step 1: Read the current file to confirm structure**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -c "import json; d=json.load(open('ai/learned_rules.json')); print('rules:', len(d.get('rules',[])), 'version:', d.get('version'))"`
Expected: Shows current rule count (~47) and version 1.

- [ ] **Step 2: Overwrite the file with an empty rule set**

Use the Write tool to replace `ai/learned_rules.json` with:

```json
{
  "version": 1,
  "last_updated": "2026-04-28T00:00:00Z",
  "rules": []
}
```

- [ ] **Step 3: Verify the cache invalidation works**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -c "from ai.learned_rules import load_rules; print('active rules:', len(load_rules()))"`
Expected: `active rules: 0`

- [ ] **Step 4: Verify `get_system_prompt` with `include_learned_rules=True` returns clean prompt**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -c "from ai.prompts import get_system_prompt; p = get_system_prompt(include_learned_rules=True); print('PART 14 present:', 'PART 14' in p); print('size bytes:', len(p.encode()))"`
Expected: `PART 14 present: False` (no learned rules → no PART 14 block injected) and size around 22000–25000 bytes.

If `PART 14 present: True`, check `ai/learned_rules.py:inject_into_prompt()` — it should only inject when there are active rules. If it injects an empty block anyway, that's fine — but verify it's empty.

- [ ] **Step 5: Commit**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
git add ai/learned_rules.json
git commit -m "feat(learned-rules): reset to empty for clean start with new prompt"
```

---

## Task 6: Manual end-to-end validation against 5 known conversations

Run the new prompt against real recent conversations from the database to confirm it behaves correctly.

**Files:**
- No file changes — manual validation only

- [ ] **Step 1: Pick 5 recent conversations from the DB to spot-check**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -c "
import psycopg2
from config.settings import DATABASE_URL
con = psycopg2.connect(DATABASE_URL)
cur = con.cursor()
cur.execute('SELECT contact_id, agent_name FROM conversation_scores ORDER BY scored_at DESC LIMIT 5')
for row in cur.fetchall():
    print(row)
con.close()
"`
Expected: 5 (contact_id, agent_name) rows printed.

- [ ] **Step 2: Score each one with the NEW prompt and write results to a temp file**

Run: `cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION" && .venv/Scripts/python.exe -c "
import asyncio, json
import psycopg2
from config.settings import DATABASE_URL
from ai.analyzer import analyze_conversation

async def go():
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()
    cur.execute('''SELECT contact_id, agent_name FROM conversation_scores ORDER BY scored_at DESC LIMIT 5''')
    rows = cur.fetchall()
    con.close()
    for contact_id, agent_name in rows:
        con = psycopg2.connect(DATABASE_URL)
        cur = con.cursor()
        cur.execute('SELECT sender, message, sent_at FROM messages WHERE contact_id=%s ORDER BY sent_at', (contact_id,))
        msgs = [{'sender': s, 'message': m, 'date': str(t.date()) if t else '', 'time': str(t.time())[:5] if t else ''} for s,m,t in cur.fetchall()]
        con.close()
        if not msgs:
            continue
        result = await analyze_conversation(messages=msgs, agent_name=agent_name, contact_name='Lead')
        print(f'=== {contact_id} / {agent_name} ===')
        print(f'  flags: {result.get(\"red_flags\", [])}')
        print(f'  scores: c={result.get(\"compliance_score\")} s={result.get(\"sentiment_score\")} p={result.get(\"professionalism_score\")} sa={result.get(\"script_adherence_score\")}')
        print(f'  label: {result.get(\"label_assigned\")} → {result.get(\"label_should_be\")} (correct={result.get(\"label_correct\")})')
        print(f'  summary: {result.get(\"summary\")}')
        print()

asyncio.run(go())
" > validation_run.txt 2>&1`
Expected: Script runs without crashing. Each of the 5 conversations gets scored.

- [ ] **Step 3: Read the output and inspect for issues**

Run: `cat validation_run.txt`

CHECK each conversation's red_flags array:
- Every flag text MUST resemble one of the 12 OUTPUT lines from PART 8.
- If you see ANY flag that's not on the whitelist (e.g., "Agent should have established rapport first", "Agent did not follow up quickly enough") — the prompt is leaking. Document the leak in a comment, fix the prompt section, re-run Task 2 step 2 (run tests), then re-run this step.

CHECK script_adherence_score:
- 0 flags → score should be 100. 1 flag → 80. 2 flags → 60. If scores don't follow this pattern, the AI ignored the formula. The fix is to make PART 7's formula more prominent.

- [ ] **Step 4: Delete the temp validation file**

Run: `rm validation_run.txt`

- [ ] **Step 5: Commit any prompt fixes from Step 3 (if needed)**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
git add ai/prompts.py 2>/dev/null
git commit -m "fix(prompts): tighten whitelist enforcement after validation run" || echo "no changes needed"
```

---

## Task 7: Update Obsidian project doc with the redesign

Per the project's CLAUDE.md, document the change in the Obsidian vault.

**Files:**
- Modify: `C:\Users\vos\Desktop\obsidian_brain\01-projects\TEXTING AUDIT AUTOMATION.md` (append a session log entry)

- [ ] **Step 1: Read the current project doc to find the Session Log section**

Use the Read tool on `C:\Users\vos\Desktop\obsidian_brain\01-projects\TEXTING AUDIT AUTOMATION.md` to locate the "## Session Log" section.

- [ ] **Step 2: Append a new session entry**

Use the Edit tool to append after the most recent session log entry:

```markdown
### 2026-04-28 — Whitelist-only prompt rewrite (12 flags)

**What we worked on:**
- Replaced bloated 390-line `SYSTEM_PROMPT` (PART 8 had 50+ defensive "NEVER flag X" rules) with a tight ~164-line whitelist-only prompt.
- Added explicit ABSOLUTE LAW block at the top: red_flags array may ONLY contain one of 12 named flags (Continued After Opt-Out, Aggressive Language, Firm Dollar Offer, Gave Up After No, Wrong Number Kept Selling, Scheduled Call No Info, Revealed 6+ Months, Incoherent / Wrong Name, Ignored Clear Interest, Closed With Zero Info, Did Not Escalate After Full Info, No Referral After High Price).
- Each of the 12 flags has a precise TRIGGER, DO NOT FIRE FOR boundary, and one-line OUTPUT text.
- `script_adherence_score` is now driven by flag count: max(0, 100 − flags_fired × 20).
- Reset `ai/learned_rules.json` to empty `[]` for a clean start. Old rules were patches for old prompt bugs.
- Backup of original prompt saved to `ai/prompts_BACKUP_2026-04-28.py` (read-only restore point).
- PART 15 funnel tier system (NF/MF/WF) and PART 16 account-specific guidelines remain unchanged — still injected dynamically via `get_system_prompt()`.
- Token reduction: ~10,800 tokens → ~5,500 tokens per call (~50% saving).

**Decisions made:**
- Whitelist-only: any flag not on the 12-item list is forbidden. Default behavior is no flag.
- Dedup pair rules: Flags 9+10 cannot both fire (write Flag 10), Flags 4+11 cannot both fire (write Flag 11).
- Flags 6, 10, 11 explicitly carve out Scenarios B (Wrong Number) and E (Realtor) where pillar/pre-qualifying rules don't apply.
- Learned rules INJECTION mechanism preserved (PART 14 still appends when rules exist), data reset to empty.

**Problems / Gotchas:**
- The $1,000 referral close was the #1 hallucination trap in the old prompt — it triggered Flag 3 (firm dollar offer) constantly. New Flag 3 explicitly excludes it in the DO NOT FIRE FOR clause.
- Flag 4 boundary: agent's last message being a question = conversation OPEN, do not flag "gave up". Captured in STEP 0 question 5.

**Next steps:**
- Run a real audit on Resva1011 / Noah to confirm flags fire only when actual whitelist conditions match.
- Monitor flag_feedback table for new false positives over the next 1-2 weeks. If any leak through, that flag definition needs a tighter boundary.
- Consider adding flags 13–15 once head of texting fills in the template's blank slots.
```

- [ ] **Step 3: Save**

The Edit tool's write completes the save. No commit needed (Obsidian vault is outside git repo).

---

## Self-Review Checklist (run before declaring complete)

After all 7 tasks complete:

1. **Spec coverage:** Every section in `docs/superpowers/specs/2026-04-28-whitelist-prompt-redesign.md` is implemented.
   - 12 flags whitelist → Task 2 ✓
   - Backup file exists → already done before plan ✓
   - Funnel tier system kept → Task 1 + 4 verify ✓
   - Learned rules reset → Task 5 ✓
   - script_adherence flag-driven → Task 2 (PART 7) + Task 3 test ✓
   - Validation plan → Task 6 ✓

2. **Placeholder scan:** No "TBD", "TODO", "implement later" appear in any task. ✓

3. **Type consistency:** Function names match between tasks (`get_system_prompt`, `load_rules`, `analyze_conversation`). ✓

4. **Frequent commits:** 7 commit points across 7 tasks. ✓

5. **Tests first:** Task 1 writes regression tests BEFORE the rewrite. Task 3 adds whitelist-specific tests AFTER. ✓
