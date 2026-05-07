# ML Prefilter Training Plan

> **Purpose:** Repeatable workflow to train and validate T1/T2 prefilter accuracy at any scale.
> **Last validated:** 2026-04-29 — 50 conversations, T1 reached 100% accuracy (30/30 correct, 0 FP).

---

## Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        TRAINING PIPELINE                            │
│                                                                     │
│  Step 1: Extract Conversations (N = 50 / 100 / 500)                │
│      ↓                                                              │
│  Step 2: AI Expert Analysis (Claude/Sonnet — NOT Groq)              │
│      ↓                                                              │
│  Step 3: Save Baseline (eval_baseline.json)                         │
│      ↓                                                              │
│  Step 4: Run T1 + T2 Against Baseline                               │
│      ↓                                                              │
│  Step 5: Analyze Mismatches & Fix Patterns                          │
│      ↓                                                              │
│  Step 6: Re-run Until 100% Match                                    │
│      ↓                                                              │
│  Step 7: Rebuild FAISS Index (T2)                                   │
│      ↓                                                              │
│  Step 8: Final Validation & Deploy                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Step 1: Extract Conversations

### What
Pull N conversations from the database into a JSON file for offline analysis.

### How

```bash
# From project root, run:
.venv\Scripts\python.exe scripts\extract_conversations.py --count 500 --output scripts/eval_500_conversations.json
```

If no extraction script exists yet, use this SQL to pull conversations:

```sql
SELECT
    c.id AS conversation_id,
    c.contact_name,
    c.account_name,
    c.texter_name,
    c.assigned_labels,
    c.funnel_tier,
    json_agg(
        json_build_object(
            'sender', m.sender,
            'body', m.body,
            'sent_at', m.sent_at
        ) ORDER BY m.sent_at
    ) AS messages
FROM conversations c
JOIN messages m ON m.conversation_id = c.id
WHERE c.created_at >= NOW() - INTERVAL '30 days'
GROUP BY c.id
ORDER BY c.id DESC
LIMIT 500;
```

### Output Format

Save as `scripts/eval_{N}_conversations.json`:

```json
[
  {
    "conversation_id": 2465,
    "contact_name": "Kathryn Barrows",
    "account_name": "LHB",
    "texter_name": "Jack",
    "assigned_labels": ["FU1", "WL drip"],
    "funnel_tier": "MF",
    "messages": [
      {"sender": "agent", "body": "Hey Kathryn...", "sent_at": "2026-04-01T10:00:00Z"},
      {"sender": "contact", "body": "Sure, what's the offer?", "sent_at": "2026-04-01T10:05:00Z"}
    ]
  }
]
```

### Batch Sizes

| Scale | Batch Strategy | Estimated Time |
|-------|---------------|----------------|
| 50 | 20 / 20 / 10 | ~30 min |
| 100 | 25 / 25 / 25 / 25 | ~1 hr |
| 500 | 50 x 10 batches | ~4 hrs |

---

## Step 2: AI Expert Analysis (Generate Baseline)

### What
Use Claude (Sonnet or Opus) — NOT Groq — to analyze each conversation and classify the outcome. This produces the "ground truth" that T1/T2 will be measured against.

### Why Not Groq?
Groq is the production model we're trying to replace/supplement. Using it as the judge would be circular. Claude provides an independent, high-accuracy expert opinion.

### The Evaluator Script

The evaluator lives at `scripts/eval_baseline.py`. It applies these rules:

#### Classification Priority Order

```
1. opt_out       → Contact explicitly asked to stop (DNC)
2. wrong_number  → Contact said wrong person/number/address
3. sold          → Contact said property already sold
4. strong_ni     → Unambiguous rejection ("nothing interests me", "Disliked")
5. abv_mv        → Contact stated price far above buyer range
6. interested    → Contact showed positive buying/selling signals
7. maybe         → Contact said maybe later / check back
8. not_interested→ Soft "no" without DNC demand
9. neutral       → No clear signal (e.g., ".." or vague reply)
10. silent       → Contact never replied
```

#### Red Flag Detection

| Flag | Description |
|------|-------------|
| F1 | Agent continued texting 2+ times after explicit opt-out |
| F4 | Agent gave up after first "no" with zero rebuttal |
| F5 | Agent pitched original property again after wrong number |

#### Label Equivalence Groups

```python
DRIP = {"wl drip", "ap drip", "hl drip", "fu1", "fu2", "fu3", ...}
NI   = {"not interested", "verified", "verified, not interested"}
DNC  = {"do not call", "dnc"}
LEAD = {"lead", "new lead", "lead, pushed", "pushed to client"}
WN   = {"wrong number"}
SOLD = {"sold", "f-sold"}
ABV  = {"abv mv", "above market value"}
```

### Running the Evaluator

```bash
# Edit DATA_PATH in eval_baseline.py to point to your file:
# DATA_PATH = Path("scripts/eval_500_conversations.json")

.venv\Scripts\python.exe scripts/eval_baseline.py
```

### Expected Output

```
Evaluating 500 conversations...

   1. [2465] Kathryn Barrows   | outcome=interested     | label=FU1, WL drip  | OK
   2. [2473] Amy Leo           | outcome=not_interested | label=Not interested | OK
   ...
TOTAL: 485/500 correct (97%)
Baseline saved to scripts/eval_baseline.json
```

---

## Step 3: Review & Fix Baseline Mismatches

### What
The baseline evaluator will flag mismatches between its classification and the human-assigned labels. Review each mismatch manually.

### Decision Framework

For each mismatch, determine:

| Question | If YES | If NO |
|----------|--------|-------|
| Is the human label correct? | Fix the baseline evaluator rules | Keep baseline, human label is wrong |
| Is this a new pattern we haven't seen? | Add pattern to evaluator | Skip — edge case |
| Would this pattern appear in future data? | Add pattern to T1 | Skip — one-off |

### Common Mismatch Types

1. **Wrong Number variants** — "Do your home work", "where is located?", vCard auto-replies
2. **Opt-out variants** — "take us off the list", "I said no", "do not suffer"
3. **Abv MV misclassified as NI** — Contact stated a price but also said "no"
4. **Maybe vs NI** — "not yet" is Maybe, not NI
5. **Interested vs NI** — Skeptical buyer who asks for proof (Kevin Foley pattern)

### Iterate Until Baseline Accuracy ≥ 98%

```bash
# Edit eval_baseline.py → fix patterns → re-run:
.venv\Scripts\python.exe scripts/eval_baseline.py

# Repeat until TOTAL accuracy ≥ 98%
```

---

## Step 4: Run T1 + T2 Against Baseline

### What
Run the actual prefilter tiers against the same conversations and compare their decisions against the saved baseline.

### How

```bash
# Edit DATA_PATH and BASELINE_PATH in eval_tier_test.py:
# DATA_PATH     = Path("scripts/eval_500_conversations.json")
# BASELINE_PATH = Path("scripts/eval_baseline.json")

.venv\Scripts\python.exe scripts/eval_tier_test.py
```

### Key Metrics to Track

| Metric | Target | Description |
|--------|--------|-------------|
| **False Positives** | **0** | T1 said "short-circuit" but baseline says different category |
| **Coverage** | **≥ 55%** | % of conversations T1 successfully short-circuited |
| **Correct SC** | **100%** | Of short-circuits, how many matched baseline |
| **T1 Misses** | **Minimize** | Conversations T1 passed that it could have caught |

### Reading the Output

```
T1 SUMMARY
  Short-circuited : 30/50  (60%)     ← Coverage
  Correct SC      : 30/30  (100%)    ← Accuracy
  False positives : 0                ← MUST be 0
  Passed through  : 20               ← Sent to Groq

POTENTIAL T1 MISSES:
  [2458] Delsa Evans | baseline=maybe  ← Check if T1 should catch this
```

---

## Step 5: Analyze Mismatches & Fix T1 Patterns

### What
For every T1 false positive or catchable miss, determine root cause and fix.

### Pattern Fix Checklist

```
For each mismatch:
  1. Pull the conversation text:
     - What did the contact say?
     - What pattern should have matched?

  2. Check which T1 check fired or failed:
     - Check 1: Opt-out escalation
     - Check 4: Wrong Number
     - Check 5: Not Interested (with Abv MV guard)
     - Check 5b: Maybe Later
     - Check 6: DNC clean
     - Check 8: Sold
     - Check 9: Wrong Identity

  3. Fix the pattern in ai/prefilter/tier1_phrases.py:
     - Add new regex to the appropriate pattern list
     - Test the regex in isolation first
     - Watch for word boundary (\b) issues with plurals (YEARS vs year)
     - Watch for case sensitivity ([A-Z] vs [A-Za-z])

  4. Re-run eval_tier_test.py to verify:
     - Fix didn't break any previously correct results
     - New pattern catches the missed conversation
     - Zero false positives maintained
```

### Pattern File Locations

| File | What It Contains |
|------|------------------|
| `ai/prefilter/tier1_phrases.py` | All T1 regex patterns and check logic |
| `ai/prefilter/tier2_embedding.py` | T2 kNN logic and similarity threshold |
| `ai/prefilter/flag_triggers.py` | Pre-flight flag routing (bypasses ML entirely) |
| `scripts/eval_baseline.py` | Baseline evaluator rules |
| `scripts/eval_tier_test.py` | T1/T2 comparison harness |

### Known Pattern Pitfalls

| Pitfall | Example | Fix |
|---------|---------|-----|
| `\b` fails on plurals | `year\b` doesn't match "YEARS" | Remove trailing `\b` or use `years?` |
| `[A-Z]` misses lowercase | `Not tom` | Use `[A-Za-z]` |
| `no\.?` misses comma | `No,` | Use `no[.,!]?` |
| Greedy `\bno\b` matches inside | "No brokers" → NI false positive | Add Abv MV guard |
| `.{0,20}` too short | Long phrases between keywords | Increase to `.{0,30}` or `.{0,50}` |

---

## Step 6: Re-run Until 100% Match

### Iteration Loop

```
WHILE false_positives > 0 OR coverage < target:
    1. Run eval_tier_test.py
    2. If false_positives > 0:
       → Fix pattern in tier1_phrases.py (too aggressive)
    3. If coverage < target:
       → Add patterns in tier1_phrases.py (too conservative)
    4. Re-run eval_tier_test.py
    5. Verify no regressions (all previous correct still correct)
```

### Acceptance Criteria

| Criteria | Threshold |
|----------|-----------|
| T1 False Positives | **0** (non-negotiable) |
| T1 Correct SC rate | **100%** |
| T1 Coverage | **≥ 55%** (aspirational: 65%) |
| Baseline accuracy | **≥ 98%** |

---

## Step 7: Rebuild FAISS Index (T2)

### What
After T1 patterns are finalized, rebuild the T2 embedding index so it can catch similar conversations by semantic similarity.

### How

```bash
.venv\Scripts\python.exe -m ai.prefilter.index_builder
```

This:
1. Pulls all scored conversations from the database
2. Embeds them with sentence-transformers
3. Saves `prefilter_index.faiss` + `prefilter_index_meta.json`

### T2 Tuning

| Setting | Default | Description |
|---------|---------|-------------|
| `PREFILTER_T2_SIM_THRESHOLD` | 0.92 | Min cosine similarity to short-circuit |
| `PREFILTER_T2_TOP_K` | 5 | Number of nearest neighbors to check |
| `PREFILTER_T2_MIN_CLEAN` | 4 | Min clean neighbors required (out of K) |

Lower the threshold to increase T2 coverage, raise it for safety.

---

## Step 8: Final Validation & Deploy

### Validation Checklist

- [ ] `eval_baseline.py` — baseline accuracy ≥ 98%
- [ ] `eval_tier_test.py` — T1 false positives = 0
- [ ] `eval_tier_test.py` — T1 correct SC = 100%
- [ ] `eval_tier_test.py` — T1 coverage ≥ 55%
- [ ] T2 index rebuilt with latest data
- [ ] T2 false positives = 0

### Deploy Settings

```env
# .env — Production settings after validation
PREFILTER_ENABLED=true
PREFILTER_SHADOW_MODE=false        # Set to true for shadow-mode testing first
PREFILTER_T1_LIVE=true
PREFILTER_T2_LIVE=true             # Enable after FAISS index is built
PREFILTER_T3_LIVE=false            # Future: XGBoost classifier
PREFILTER_FLAG_ROUTING_ENABLED=true
```

### Shadow Mode (Recommended First)

Run in shadow mode for 24-48 hours before going live:

```env
PREFILTER_SHADOW_MODE=true    # Records decisions but ALWAYS sends to Groq
PREFILTER_T1_LIVE=true
PREFILTER_T2_LIVE=true
```

Then compare `prefilter_decisions` table against Groq results to validate.

---

## Quick Reference: Full Training Run Commands

```bash
# 1. Extract conversations
.venv\Scripts\python.exe scripts\extract_conversations.py --count 500 --output scripts/eval_500_conversations.json

# 2. Generate baseline (edit DATA_PATH first)
.venv\Scripts\python.exe scripts/eval_baseline.py

# 3. Run T1+T2 against baseline
.venv\Scripts\python.exe scripts/eval_tier_test.py

# 4. Fix patterns, repeat step 3 until 100%

# 5. Rebuild FAISS index
.venv\Scripts\python.exe -m ai.prefilter.index_builder

# 6. Final validation
.venv\Scripts\python.exe scripts/eval_tier_test.py

# 7. Deploy
# Edit .env → PREFILTER_SHADOW_MODE=false
```

---

## Training History

| Date | Conversations | Baseline Accuracy | T1 Coverage | T1 FP | Patterns Added |
|------|--------------|-------------------|-------------|-------|----------------|
| 2026-04-29 | 50 | 49/50 (98%) | 30/50 (60%) | 0 | 9 new patterns |
| | | | | | |
| | | | | | |

> Add a row each time you run a training cycle to track improvement over time.
