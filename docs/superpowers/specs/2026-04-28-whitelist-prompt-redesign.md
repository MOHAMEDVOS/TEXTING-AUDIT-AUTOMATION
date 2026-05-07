# Prompt Redesign — Whitelist-Only Flagging

**Date:** 2026-04-28  
**Status:** Approved for implementation  
**Replaces:** `ai/prompts.py` SYSTEM_PROMPT (390 lines → ~157 lines)

---

## Problem

Current `SYSTEM_PROMPT` (~10,800 tokens) generates hallucinated flags because:
1. PART 8 has 50+ "NEVER flag X" rules — AI still invents variations
2. No hard list of permitted flags — AI free-forms anything that looks bad
3. Defensive prose creates contradictions the AI resolves unpredictably

---

## Solution

Whitelist-only approach: `red_flags` array may only contain flags matching one of 12 named items. Anything not on the list is forbidden. The constraint is stated twice (top of prompt + inside flag section).

---

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Funnel tier system (PART 15/16) | **Keep** | Per-account overrides still needed |
| Learned rules injection (PART 14) | **Keep mechanism, reset data** | Start clean; re-accumulate from new prompt |
| `script_adherence_score` | **Driven by flags only** | No separate independent scoring |
| PART 8 "NEVER flag" wall | **Delete entirely** | Whitelist replaces it structurally |
| `learned_rules.json` | **Reset to empty** | 35+ old rules were patching old prompt bugs |

---

## New Prompt Structure

| Section | Content | Est. Lines |
|---|---|---|
| 1 | Role + Whitelist Declaration (stated twice) | 10 |
| 2 | Pre-Audit Checklist (5 questions, compressed) | 6 |
| 3 | Context Facts (5 immutable operation rules) | 5 |
| 4 | Tone Detection (hand raise / confusion / disinterest / sarcasm) | 12 |
| 5 | Scenarios A–G (compressed, D+F = 1 line each) | 10 |
| 6 | Funnel Stages + Pillars + Rebuttals | 18 |
| 7 | Scoring Rules (4 scores; script_adherence = flag-driven) | 8 |
| 8 | Red Flags — Whitelist Enforcement (12 flags, dedup rule, texter-only rule) | 45 |
| 9 | Label Audit (valid labels, semantic equivalence groups, wrong-label triggers) | 22 |
| 10 | Output Format + Writing Style | 28 |
| — | PART 15 funnel tier (NF/MF/WF) — unchanged, injected dynamically | existing |
| — | PART 16 account guidelines — unchanged, injected dynamically | existing |
| — | PART 14 learned rules — mechanism kept, data reset to `[]` | existing |
| **Total** | | **~164 lines / ~4,200 tokens** |

---

## The 12 Whitelist Flags

Each flag has: trigger condition, hard boundary (do-not-fire case), one-line output text.

| # | Name | Trigger | Key Boundary |
|---|---|---|---|
| 1 | Continued After Opt-Out | Explicit opt-out → agent sends another message | One confirmation reply allowed |
| 2 | Aggressive or Deceptive Language | Threat / profanity / false factual claim | Truthful urgency language = no flag |
| 3 | Stated Firm Dollar Offer | Single specific dollar amount framed as purchase offer | $1k referral, ranges, "I'll get you a number" = no flag |
| 4 | Gave Up After First No | Soft no → agent sends ZERO follow-up | ANY message after no = clears flag |
| 5 | Wrong Number But Kept Selling | Wrong number confirmed → agent continues property pitch | Referral pivot after wrong number = no flag |
| 6 | Scheduled Call With No Info | Lead asks to call → agent agrees with zero qualifying questions | Even 1 qualifying question anywhere = no flag; Scenario E exempt |
| 7 | Revealed or Promised Over 6 Months | Agent volunteers 6+ month timeline | Lead sets timeline, agent agrees = no flag |
| 8 | Incoherent or Wrong Name | Unreadable message OR wrong name used | Typos, casual abbreviations = no flag |
| 9 | Ignored Clear Interest | Lead shows clear interest → agent ends chat | Agent continues (even poorly) = no flag |
| 10 | Tried to Close With Zero Info | Agent pushes call/offer with zero property info from lead | Any single piece of info from lead = no flag; Scenario E exempt |
| 11 | Did Not Escalate After Full Info | All 4 pillars from lead → agent makes no escalation attempt | Missing even 1 pillar = no flag; Scenarios B/E exempt |
| 12 | No Referral Close After High Price | Above-market rejection → convo ends without $1k referral | Referral line in final message = no flag |

---

## Deduplication Rules

- One mistake = one flag maximum
- Flags 9+10 cannot both fire on the same message (same error, write Flag 10 only)
- Flags 4+11 cannot both fire on same conversation (write Flag 11 — more specific)
- Flag 6 exempt for Scenario E (Realtor/Investor)
- Flags 10+11 exempt for Scenarios B (Wrong Number) and E (Realtor)

---

## script_adherence_score Formula

```
flags_fired = count of red_flags in output
score = max(0, 100 - (flags_fired * 20))
```

0 flags → 100. 1 flag → 80. 2 flags → 60. 3 flags → 40. 4+ flags → 20 or 0.

---

## Files Changed

| File | Change |
|---|---|
| `ai/prompts.py` | Rewrite `SYSTEM_PROMPT` — new skeleton replaces old. All other functions unchanged. |
| `ai/learned_rules.json` | Reset `rules` array to `[]`, update `last_updated` |
| `ai/prompts_BACKUP_2026-04-28.py` | Created — full backup of old prompt (read-only restore point) |

---

## What Does NOT Change

- `BATCH_OUTPUT_FORMAT` and `_swap_output_format()` — unchanged
- `FUNNEL_TIER_RULES` dict (PART 15 NF/MF/WF blocks) — unchanged  
- `format_account_guidelines()` (PART 16 builder) — unchanged
- `get_system_prompt()` function signature and injection order — unchanged
- `format_for_analysis()` — unchanged
- All DB schema, scorer, analyzer, dashboard code — unchanged

---

## Validation Plan

After implementation:
1. Run 5 known-good conversations (no violations) — confirm `red_flags: []`
2. Run 3 conversations with known Flag 1 violations — confirm only Flag 1 fires
3. Run 1 conversation with $1k referral close — confirm Flag 3 does NOT fire
4. Run 1 NF conversation with all 4 pillars + agent booked call — confirm Flag 11 does NOT fire
5. Run 1 Realtor conversation — confirm Flag 6 does NOT fire
