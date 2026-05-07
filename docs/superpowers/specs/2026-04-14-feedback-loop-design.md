# Design: Feedback Loop — Suppress Invalid AI Flags
**Date:** 2026-04-14  
**Status:** Approved

---

## Problem

The AI scorer generates false-positive red flags that human reviewers have already marked
as invalid in `flag_feedback` (30 entries as of 2026-04-14). These keep reappearing on every
new audit run because:

1. The AI prompt doesn't know the rules it keeps getting wrong.
2. The scorer never consults `flag_feedback` before saving results.

---

## Goal

Never generate or save the same false-positive flag again. Two layers:
- **Prompt patch** — teach the AI the rules at the source so it stops generating them.
- **Scorer filter** — strip any surviving false positive before writing to the DB.
  This is a self-improving loop: every new flag marked invalid in the dashboard
  automatically feeds the next run's filter.

---

## Files Changed

| File | Change |
|------|--------|
| `ai/prompts.py` | Add PART 13 — LEARNED CORRECTIONS block to `SYSTEM_PROMPT` |
| `ai/scorer.py` | Add `_load_invalid_flag_patterns()` + `_filter_flags()`, call in scoring loop |

**Not changed:** `ai/analyzer.py`, `database/db.py`, `dashboard/app.py`, any config or schema.

---

## Part 1 — Prompt Patch (`ai/prompts.py`)

Add a new section **PART 13 — LEARNED CORRECTIONS** at the end of `SYSTEM_PROMPT`,
before the output format block. This section encodes the 7 rules learned from the 30
human feedback entries.

### Rule 1 — "Verified, Not Interested" is a valid label
Add `"Verified, Not Interested"` to the semantic equivalence group B in Part 10
alongside `"Not Interested"` and `"Not Interested + Verified"`.
Never flag it as wrong and say it should be `"Not Interested"` — they are the same.

### Rule 2 — Referral question after disinterest = correct, never flag
The referral question (`"Do you know anyone who wants to sell? I pay $1,000 for referrals"`)
is the required scripted exit. Asking it after a lead says they're not interested or their
property is not for sale is NOT "continuing after disinterest" — it is the correct closing move.
Never flag it as: "Continued messaging after clear disinterest", "Ignored lead's implicit
disinterest and continued with a referral question", or any variant.

### Rule 3 — Agent stopping immediately after opt-out = correct, never flag
If the agent sent NO further messages after an explicit opt-out (including profanity like
"fuck no") and set the label to "Do Not Call" — that is perfect behavior.
Only flag if the agent actually sent another message AFTER the opt-out.
Never flag: "Ignored explicit opt-out and continued sending messages",
"Agent did not stop contacting the lead after explicit opt-out" when the agent stopped.

### Rule 4 — Cash range ≠ firm offer, never flag a range as a firm offer
A price range like `"129k-172k"` or `"cash range"` or any two-number spread is never
a firm offer. Only flag `"Stated specific dollar amount as FIRM OFFER"` when the agent
states a single specific price as a definitive offer (e.g. "we offer $180k").
Never flag ranges, never flag "Promised no timeline but revealed a cash range."

### Rule 5 — "Missed Call" label is valid when lead's last action was a missed call
When the lead's final message or action in the conversation was a missed call notification,
`"Missed Call"` is a valid disposition. Never flag it as wrong and replace with
`"Stopped Responding"` in this scenario.

### Rule 6 — Agent explaining SMS-only after lead calls ≠ messaging after silence
If a lead is calling the agent and the agent sends a message explaining they cannot
take calls right now (SMS-only workflow), that is NOT spamming a silent lead.
Never flag it as "Continued messaging after lead stopped responding" in this context.

### Rule 7 — "Bluffer" is valid for wildly unrealistic prices
When an owner asks for a price that is obviously a joke or impossibly above market
(e.g. $650 million on a 2-bed house), `"Bluffer"` is the correct disposition.
Never override `"Bluffer"` with `"Not Interested + Abv MV"` for absurd/joke-level prices.

### Rule 8 — Label capitalization/spelling variants are valid
Any reasonable spelling or capitalization of "Do Not Call" is acceptable
(e.g. "Do Not Call", "DO Not Call", "do not call"). Never flag these as wrong labels.
"Lead, Pushed" is a valid label when the lead showed genuine interest and was pushed
to the client — never flag it as should be "Not Interested."

---

## Part 2 — Scorer Filter (`ai/scorer.py`)

### New function: `_load_invalid_flag_patterns(db_path) -> set[str]`

Runs a synchronous SQLite read at the start of `score_agent_conversations()`.
Returns a set of lowercased, stripped `red_flag` strings from the entire
`flag_feedback` table (all agents, all contacts — global patterns).

```python
def _load_invalid_flag_patterns(db_path: str) -> set[str]:
    import sqlite3
    try:
        con = sqlite3.connect(db_path)
        cur = con.execute("SELECT red_flag FROM flag_feedback")
        patterns = {row[0].lower().strip() for row in cur.fetchall() if row[0]}
        con.close()
        return patterns
    except Exception as e:
        logger.warning(f"[Scorer] Could not load invalid flag patterns: {e}")
        return set()
```

### New function: `_filter_flags(flags, patterns) -> list[str]`

For each flag in the list, check two conditions (both lowercased):
- `flag.lower() in pattern` (flag text is a substring of a stored pattern), OR
- `pattern in flag.lower()` (stored pattern is a substring of the flag text)

If either matches any pattern → suppress the flag (exclude from output).
If no pattern matches → keep the flag.

```python
def _filter_flags(flags: list[str], patterns: set[str]) -> list[str]:
    if not patterns:
        return flags
    clean = []
    for flag in flags:
        f = flag.lower().strip()
        suppressed = any(f in p or p in f for p in patterns)
        if not suppressed:
            clean.append(flag)
    return clean
```

### Integration point in `score_agent_conversations()`

1. Load patterns once at the top of the function (before the batch loop):
   ```python
   invalid_patterns = _load_invalid_flag_patterns(str(DB_PATH))
   ```

2. After each batch result comes back (and after the individual fallback), apply filter:
   ```python
   for result in results:
       result["red_flags"] = _filter_flags(result.get("red_flags") or [], invalid_patterns)
   ```

3. After the wrong-label flag injection (lines 184-192), apply filter again:
   ```python
   for r in per_convo:
       r["red_flags"] = _filter_flags(r.get("red_flags") or [], invalid_patterns)
   ```

---

## Self-Improving Loop

```
Human marks flag invalid in dashboard
  → saved to flag_feedback table
  → next audit run: _load_invalid_flag_patterns() picks it up automatically
  → flag suppressed for ALL agents on ALL future runs
  → no code changes needed
```

---

## What Is NOT Changed

- `flag_feedback` table schema — unchanged
- Dashboard's `invalidated_map` display logic — unchanged (still hides flags in UI too)
- `/api/redflag/invalid` endpoint — unchanged
- All scraper, config, and database files — unchanged

---

## Success Criteria

1. Running an audit for Resva1010, Noah, Resva1013, Resva1014 produces zero of the 30 known false-positive flags.
2. A newly marked invalid flag (via dashboard) is automatically suppressed on the very next run with no code change.
3. Legitimate red flags (e.g. actual opt-out violation, real firm offer) are NOT suppressed.
