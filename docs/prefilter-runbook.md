# ML Pre-Filter — Promotion Runbook

> **Goal**: Reduce Groq API calls by ~20% while keeping FALSE-CLEAN ≤5%.

---

## How It Works (30-Second Summary)

The prefilter runs **before** Groq on every conversation. It has 3 tiers:

| Tier | Method | What It Does |
|------|--------|--------------|
| T1 | Phrase matching | Catches trivially clean convos (contact silent, ≤2 agent msgs) |
| T2 | kNN embedding | Matches against past clean conversations via FAISS index |
| T3 | Classifier | Logistic regression predicts P(flag) + 4 audit scores |

If a tier is confident the conversation is clean → **skip Groq** and use the local prediction.  
If uncertain or risky → **escalate to Groq** (default safe behavior).

**Flag routing** (always on): opt-outs, profanity, dollar offers, wrong-number signals always bypass ML → Groq.

---

## Current State

```
PREFILTER_ENABLED=true          ← prefilter runs
PREFILTER_SHADOW_MODE=true      ← decisions recorded, but Groq still scores everything
PREFILTER_T1_LIVE=true          ← ready to promote
PREFILTER_T2_LIVE=false         ← waiting
PREFILTER_T3_LIVE=false         ← waiting
PREFILTER_FLAG_ROUTING_ENABLED=true  ← always keep true
```

---

## Promotion Gates

A tier is promoted from shadow → live only when **both** conditions pass on **2 consecutive weekly evals**:

| Tier | FALSE-CLEAN Gate | Groq Savings Gate |
|------|-----------------|-------------------|
| T1 live | ≤5% | ≥5% |
| T2 live (T1+T2 combined) | ≤5% | ≥10% |
| T3 live (T1+T2+T3 combined) | ≤5% | ≥20% |

- **FALSE-CLEAN** = prefilter said clean, but Groq would have flagged. This is the dangerous metric.
- **Groq Savings** = % of conversations that would skip Groq.

---

## Weekly Eval Routine

### Step 1: Run the eval

```bash
python scripts/eval_prefilter.py --limit 500
```

Or with a date filter:

```bash
python scripts/eval_prefilter.py --limit 500 --since 2026-04-21
```

### Step 2: Save the report

```bash
python scripts/eval_prefilter.py --limit 500 --output-md docs/eval_results/eval_2026-04-28.md
```

### Step 3: Check the gate printout

The script prints **PASS** or **FAIL** for each tier gate. Example:

```
Gate Check — Tier 1:  FALSE-CLEAN 1.2% ≤ 5.0% → PASS   |  Savings 8.3% ≥ 5.0% → PASS
Gate Check — Tier 2:  FALSE-CLEAN 2.1% ≤ 5.0% → PASS   |  Savings 14.5% ≥ 10.0% → PASS
Gate Check — Tier 3:  FALSE-CLEAN 3.8% ≤ 5.0% → PASS   |  Savings 22.1% ≥ 20.0% → PASS
```

### Step 4: Promote (if gates pass 2 weeks in a row)

```bash
python scripts/promote_prefilter.py
```

This script reads current `.env`, runs the eval, checks gates, and updates `.env` if safe.

---

## Promotion Order

**Always promote in this order — never skip a tier:**

1. `PREFILTER_SHADOW_MODE=false` + `PREFILTER_T1_LIVE=true`
2. `PREFILTER_T2_LIVE=true`
3. `PREFILTER_T3_LIVE=true`

---

## Retraining Schedule

### Tier 2 — Rebuild kNN Index

**When**: Every +200 new Groq-scored conversations, or weekly.

```bash
python -m ai.prefilter.index_builder --rebuild
```

### Tier 3 — Retrain Classifier

**When**: Weekly, or when FALSE-CLEAN rises above gate threshold.

```bash
python -m ai.prefilter.train --test-split 0.2
```

Both commands write metadata to `ai/prefilter/artifacts/manifest.json` for reproducibility.

---

## Rollback (Emergency)

If something goes wrong, flip one variable:

```bash
# In .env:
PREFILTER_SHADOW_MODE=true
```

This immediately reverts ALL tiers to shadow mode — Groq scores everything, prefilter only records.

No data loss. No code changes needed. Restart the dashboard/main process to pick up the change.

---

## Artifact Files

All ML artifacts live in `ai/prefilter/artifacts/`:

| File | Source | Size |
|------|--------|------|
| `knn_index.faiss` | `index_builder.py` | ~1.4 MB |
| `knn_index_meta.json` | `index_builder.py` | ~157 KB |
| `classifier.joblib` | `train.py` | ~11 KB |
| `manifest.json` | Both builders | ~1 KB |

---

## Guardrails (Always Active)

1. **Flag routing**: opt-outs, profanity, offers, wrong-number → always Groq
2. **Fail-open**: if artifacts missing or ML inference fails → escalates to Groq
3. **ML never predicts flags**: it only decides "safe to skip Groq" vs "send to Groq"
4. **Shadow mode recording**: decisions always logged to `prefilter_decisions` table
