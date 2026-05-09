# Groq Elimination + Self-Learning ML Pipeline — Unified Master Plan

> **Mission:** Replace Groq entirely with a 4-tier local ML pipeline that **teaches itself** from every audit it performs. The auto-learning cycle is the engine that makes Groq elimination viable: the more conversations the system sees, the more patterns it absorbs, the less it needs Groq — until Groq is never called.

---

## Table of Contents

1. [Strategic Overview](#strategic-overview)
2. [Why Auto-Learning is the Core](#why-auto-learning-is-the-core)
3. [Target Architecture](#target-architecture)
4. [The 3-Phase Roadmap](#the-3-phase-roadmap)
5. [Phase A — Build the Self-Sufficient Pipeline](#phase-a--build-the-self-sufficient-pipeline-shadow-mode)
6. [Phase B — Flip Live with Groq Safety Net](#phase-b--flip-live-with-groq-safety-net)
7. [Phase C — Strip Groq Completely](#phase-c--strip-groq-completely)
8. [Database Schema Changes](#database-schema-changes)
9. [Configuration Reference](#configuration-reference)
10. [File-by-File Changelog](#file-by-file-changelog)
11. [Dashboard Integration](#dashboard-integration)
12. [Testing & Quality Gates](#testing--quality-gates)
13. [Safety Rails](#safety-rails)
14. [Rollback Plan](#rollback-plan)
15. [Open Decisions](#open-decisions)

---

## Strategic Overview

### Current State (May 2026)
- **3 ML tiers** + **Groq fallback**.
- **Tier 1 v2** (regex phrases): 33% coverage, 0 false-positive flags.
- **Tier 2** (FAISS kNN): only short-circuits clean conversations; **escalates on any flag risk**.
- **Tier 3** (logistic regression): only short-circuits clean; **escalates when `flag_prob ≥ 0.35`**.
- **Combined ML coverage**: ~65%. Remaining 35% goes to Groq.
- **Groq does ALL flag generation, ALL summary text, ALL label correction reasoning.**

### End State (Target)
- **4 ML tiers** + **automatic self-learning loop**.
- **Tier 4** is a deterministic flag generator + multi-label classifier — terminal tier, never escalates.
- **Auto-learner** captures every novel high-quality conversation and feeds it back into T2 (FAISS) + T3 (classifier) via dream worker.
- **Coverage**: 100% (T4 always returns a result).
- **Groq path**: deleted from analyzer; `api_keys` table archived.
- **Cost**: $0/month for AI inference. All local CPU.

### Why This Will Work
The system already has:
- A working FAISS index (Tier 2)
- A working multi-output regressor (Tier 3 score predictor)
- A whitelist of exactly **12 red flags** Groq is constrained to (`analyzer.py:1041-1054`)
- Deterministic post-Groq guards (`_apply_label_guards`, `_agent_continued_after_opt_out`, `_normalize_red_flags`) that already do half of T4's job
- A dream worker scheduler that already runs on cron-like cadence
- A `learned_rules` system that already injects feedback into prompts

We're **assembling existing parts**, not inventing new ML.

---

## Why Auto-Learning is the Core

Without auto-learning, eliminating Groq requires hand-curating a training set big enough to cover every conversation pattern. That's months of labeling work. **With auto-learning, every Groq call (during Phase A + B) becomes free training data.**

The capture loop:
```
Groq scores high + clean → embed → "is this novel?" → save candidate
                                                          ↓
                            Dream worker → batch → retrain → T2/T3 absorb pattern
                                                          ↓
                            Next time this pattern appears → ML catches it → no Groq needed
```

By the end of Phase B, the system has trained itself on thousands of real conversations across every funnel tier and agent style. **That's what makes Phase C (deleting Groq) safe.**

---

## Target Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         AUDIT REQUEST                                     │
│                  conversation + agent + labels                            │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      TIER 1 — Phrase Matching                             │
│              ai/prefilter/tier1_phrases_v2.py                             │
│                                                                            │
│  • Funnel-aware regex (WF/MF/NF)                                         │
│  • SHORT_CIRCUIT clean (33% of traffic)                                  │
│  • ESCALATE on flag triggers (opt-out, $, profanity)                    │
│  • Pass-through otherwise                                                │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                  ┌─────────────────┼─────────────────┐
                  │                 │                 │
            short-circuit       pass-through      escalate
                  │                 │                 │
                  ▼                 ▼                 ▼
              [DONE]           T2 search        ┌─────┐
                                    │           │     │
                                    ▼           │     │
┌──────────────────────────────────────────────────────────────────────────┐
│                    TIER 2 — FAISS kNN (UPGRADED)                          │
│              ai/prefilter/tier2_embedding.py                              │
│                                                                            │
│  Top-K nearest neighbors + their stored flags:                            │
│  • All clean + sim ≥ T2_SIM        → SHORT_CIRCUIT clean                  │
│  • All flagged + same flag set     → SHORT_CIRCUIT with those flags  ★NEW│
│  • Mixed clean/flagged or low sim  → pass to T3                          │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                  TIER 3 — Multi-Model Classifier (UPGRADED)               │
│              ai/prefilter/tier3_classifier.py                             │
│                                                                            │
│  flag_clf       (binary):     P(any flag)                                │
│  flag_label_clf (multi-label): which of the 12 whitelist flags  ★NEW    │
│  score_reg      (4-output):   compliance/sentiment/prof/script           │
│                                                                            │
│  • flag_prob < 0.35           → SHORT_CIRCUIT clean + predicted scores   │
│  • flag_prob ≥ 0.35 + label_confidence ≥ 0.7 → SHORT_CIRCUIT with     ★NEW│
│                                                  predicted flags          │
│  • Low confidence on labels   → pass to T4                               │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│             TIER 4 — Deterministic Flag Generator (NEW)                   │
│              ai/prefilter/tier4_flag_generator.py                         │
│                                                                            │
│  Rule-based catch-all. Always returns a result.                           │
│                                                                            │
│  1. Run flag_triggers patterns → map matches to whitelist flag strings   │
│  2. Run existing analyzer.py guards (opt-out, soft-no, joke price)       │
│  3. Copy flags from single nearest flagged FAISS neighbor (if any)       │
│  4. Compute scores: base 90 - 15·n_flags - tier-specific penalties       │
│  5. Apply label guards (reuse _apply_label_guards from analyzer.py)      │
│  6. Build summary via summary_builder.py                                  │
│  7. RETURN — never escalate                                               │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                                [RESULT]
                                    │
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              POST-PROCESSING (existing analyzer.py guards)                │
│                                                                            │
│  • _normalize_red_flags()        — whitelist enforcement                  │
│  • _agent_continued_after_opt_out — deterministic opt-out check          │
│  • _agent_replied_after_first_soft_no                                    │
│  • _apply_label_guards            — DNC label correctness                │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                         INSERT conversation_scores
                                    │
                                    │ (post-score hook)
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              AUTO-LEARNING CAPTURE (NEW)                                  │
│              ai/prefilter/semantic_learner.py                             │
│                                                                            │
│  IF result came from Groq (Phase A/B) OR T4 fallback (Phase C):          │
│    AND scores ≥ SEMANTIC_MIN_SCORE                                       │
│    AND red_flags == []                                                    │
│    AND embedding novelty: top FAISS sim < SEMANTIC_MAX_SIMILARITY        │
│                                                                            │
│  → INSERT INTO semantic_candidates                                        │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ (async, ~4hr cadence)
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              DREAM WORKER (extended)                                      │
│              ai/dream_worker.py + ai/prefilter/auto_promoter.py           │
│                                                                            │
│  Existing: cluster flag_feedback → learned_rules.json                    │
│  NEW:      promote semantic_candidates →                                  │
│              write synthetic_<ts>_auto_training.json                      │
│              run index_builder --rebuild                                  │
│              run train.py                                                 │
│              mark candidates promoted=TRUE                                │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## The 3-Phase Roadmap

| Phase | Duration | Goal | Groq Status |
|---|---|---|---|
| **A — Build** | 1-2 weeks | T4 + multi-label T3 + auto-learner. Shadow mode compares ML to Groq. | Authoritative |
| **B — Flip live** | 2-4 weeks | ML produces final scores. Groq held in reserve as manual override only. Auto-learner harvests every Groq override. | Safety net |
| **C — Strip** | 2 days | Delete Groq code, archive `api_keys`. Auto-learner now feeds on T4 outputs. | Deleted |

**Phase boundaries are gated by quality metrics**, not calendar dates.

---

## Phase A — Build the Self-Sufficient Pipeline (Shadow Mode)

### A.1 Add Tier 4 — Deterministic Flag Generator

**NEW FILE: `ai/prefilter/tier4_flag_generator.py`**

```python
"""
Tier 4 — Terminal flag generator. Always returns a result.

Deterministic logic (no ML required for the basics):
  1. Run flag_triggers regexes → mapped to whitelist flags.
  2. Use guard helpers from analyzer.py (opt-out, soft-no, joke-price).
  3. Look at single nearest flagged FAISS neighbor → copy its flags if highly similar.
  4. Score = 90 - penalties (mirrors train.py:_infer_scores_from_baseline).
  5. Build summary via summary_builder.
  6. Apply label_validator.

Never returns None. Never escalates. This is the bottom of the stack.
"""

def evaluate(messages, agent_name, contact_name, funnel_tier, assigned_labels) -> PipelineResult:
    # 1. Pattern-based flag detection
    detected_flags = []
    detected_flags += _detect_optout_violation(messages)        # uses analyzer._agent_continued_after_opt_out
    detected_flags += _detect_giveup_violation(messages)        # uses analyzer._agent_replied_after_first_soft_no
    detected_flags += _detect_dollar_offer(messages)            # regex on agent messages
    detected_flags += _detect_threatening_language(messages)    # profanity list
    detected_flags += _detect_pillar_failures(messages, funnel_tier)
    # ... (12 whitelist flags total — see analyzer._WHITELIST_FLAG_OUTPUTS)

    # 2. Augment with nearest flagged neighbor flags (if any)
    nearest_flagged = _find_nearest_flagged_neighbor(messages, agent_name, funnel_tier)
    if nearest_flagged and nearest_flagged.similarity >= 0.85:
        detected_flags.extend(nearest_flagged.flags)

    detected_flags = _dedupe_and_normalize(detected_flags)  # reuse _normalize_red_flags

    # 3. Compute scores deterministically
    scores = _compute_scores(detected_flags, funnel_tier, messages)

    # 4. Apply label guards
    label = (assigned_labels or [""])[0]
    label_check = label_validator.validate_label(messages, label)

    # 5. Build summary
    summary = summary_builder.build_summary(messages, agent_name, contact_name, scores, model_used="prefilter_t4")

    return PipelineResult(
        tier_hit=4,
        decision="short_circuit",
        confidence=0.6,  # deterministic but coarse
        result={
            **scores,
            "red_flags": detected_flags,
            "label_assigned": label,
            "label_correct": label_check["label_correct"],
            "label_should_be": label_check["label_should_be"],
            "label_reason": label_check["label_reason"],
            "summary": summary,
            "model_used": "prefilter_t4",
            "contact_name": contact_name,
            "funnel_stage_reached": summary_builder.detect_funnel_stage(messages),
            "pillars_gathered": [],
            "rebuttals_used": [],
            "actions_triggered": [],
        }
    )
```

**Key design points:**
- **Reuses everything**: `analyzer._WHITELIST_FLAG_OUTPUTS`, `_OPTOUT_TEXT_RE`, `_SOFT_NO_RE`, `_DNC_JOKE_PRICE_RE`, `_apply_label_guards`, `summary_builder.*`, `label_validator.*`. Move shared helpers from `analyzer.py` into a new `ai/prefilter/_guards.py` so both can import without circular imports.
- **Score formula** mirrors `train.py:_infer_scores_from_baseline` (lines 80-128).
- **Confidence is intentionally low (0.6)** so logging makes T4 hits visible — they're the ones to watch during Phase B.

### A.2 Upgrade Tier 3 — Multi-Label Flag Classifier

**MODIFY: `ai/prefilter/train.py`**

Add a third model alongside `flag_clf` and `score_reg`:

```python
from sklearn.multiclass import OneVsRestClassifier

# Build multi-label target: y_labels[i, j] = 1 if conversation i has flag j
WHITELIST_FLAGS = [...]  # import from analyzer._WHITELIST_FLAG_OUTPUTS

y_labels = np.zeros((len(rows), len(WHITELIST_FLAGS)), dtype=np.int32)
for i, row in enumerate(rows):
    for flag_text in row["red_flags"] or []:
        if flag_text in WHITELIST_FLAGS:
            y_labels[i, WHITELIST_FLAGS.index(flag_text)] = 1

# Train multi-label classifier
flag_label_clf = OneVsRestClassifier(LogisticRegression(max_iter=1000, class_weight="balanced"))
flag_label_clf.fit(X_tr, y_labels[train_idx])

# Save in same bundle
bundle["flag_label_clf"] = flag_label_clf
bundle["whitelist_flags"] = WHITELIST_FLAGS
```

**MODIFY: `ai/prefilter/tier3_classifier.py`**

Change escalation logic — instead of returning None when `flag_prob ≥ threshold`, predict the flags:

```python
flag_prob = float(flag_clf.predict_proba(query)[0, 1])

if flag_prob < settings.PREFILTER_T3_MAX_FLAG_PROB:
    # Confidently clean — short-circuit clean (existing path)
    ...
else:
    # Predict which flags
    label_probs = _classifier_bundle["flag_label_clf"].predict_proba(query)[0]
    confident_labels = [
        WHITELIST_FLAGS[i]
        for i, p in enumerate(label_probs)
        if p >= settings.PREFILTER_T3_LABEL_CONFIDENCE  # default 0.7
    ]

    if not confident_labels:
        # Sure something's wrong but unsure what → drop to T4
        return None

    # Confident about which flags — short-circuit with them
    return PipelineResult(
        tier_hit=3,
        decision="short_circuit",
        confidence=min(label_probs[label_probs >= 0.7].mean(), 1.0),
        result=_build_result_with_flags(contact_name, scores_pred, confident_labels, ...)
    )
```

### A.3 Upgrade Tier 2 — Flag Copy from Neighbors

**MODIFY: `ai/prefilter/tier2_embedding.py`**

Currently lines 159-169 escalate on any flagged close neighbor. Change to:

```python
flagged_close = [n for n in close_neighbors if not n["is_clean"]]
clean_close = [n for n in close_neighbors if n["is_clean"]]

# Existing path: all clean → short-circuit clean
if len(clean_close) >= settings.PREFILTER_T2_MIN_NEIGHBORS and not flagged_close:
    return _short_circuit_clean(...)

# NEW path: all flagged with same flag set → short-circuit with flags
if len(flagged_close) >= settings.PREFILTER_T2_MIN_NEIGHBORS and not clean_close:
    common_flags = _intersect_flag_sets([n["red_flags"] for n in flagged_close])
    if common_flags:
        avg_scores = _average_scores(flagged_close)
        return PipelineResult(
            tier_hit=2,
            decision="short_circuit",
            confidence=top_sim,
            result=_build_result_with_flags(contact_name, avg_scores, common_flags, ...)
        )

# Mixed or no clear majority → escalate to T3
return None
```

This requires storing red flags in the FAISS index metadata. **MODIFY: `ai/prefilter/index_builder.py`**:

```python
meta.append({
    "conversation_id": int(r["conversation_id"]),
    "funnel_tier": r.get("funnel_tier", "NF"),
    "is_clean": is_clean(r["red_flags"], invalid_patterns),
    "red_flags": _normalize_flags(r["red_flags"], invalid_patterns),  # ★ NEW
    "scores": {...},
})
```

### A.4 Add Auto-Learning — Capture Half

**NEW FILE: `ai/prefilter/semantic_learner.py`**

(Full spec already in [SEMANTIC_AUTO_LEARNING_PLAN.md](SEMANTIC_AUTO_LEARNING_PLAN.md). Recap:)

```python
async def evaluate_novelty(messages, agent_name, contact_name, funnel_tier,
                            conversation_id, scores, red_flags, db_pool) -> bool:
    if not settings.SEMANTIC_LEARNING_ENABLED: return False
    if any(scores[k] < settings.SEMANTIC_MIN_SCORE for k in 4 keys): return False
    if red_flags: return False

    text = embedder.conversation_to_text(messages, agent_name)
    text = f"[{funnel_tier}]\n{text}"
    h = embedder.text_hash(text)
    vec = embedder.embed(text)

    top_sim, nearest_id = _search_top_neighbor(vec)
    if top_sim >= settings.SEMANTIC_MAX_SIMILARITY: return False

    distinctive = _extract_distinctive_phrases(messages, top_neighbors=5)
    await db.insert_semantic_candidate(conversation_id, funnel_tier, h, top_sim, ...)
    return True
```

### A.5 Add Auto-Learning — Promote Half

**NEW FILE: `ai/prefilter/auto_promoter.py`**

```python
def promote_pending_candidates(force=False) -> dict:
    candidates = _fetch_unpromoted(limit=settings.SEMANTIC_MAX_PER_RUN)
    if len(candidates) < settings.SEMANTIC_MIN_PROMOTE and not force:
        return {"promoted_count": 0}

    payload = _build_synthetic_training_payload(candidates)
    path = scripts_dir / f"synthetic_{ts}_auto_training.json"
    json.dump(payload, open(path, "w"))

    subprocess.run([sys.executable, "-m", "ai.prefilter.index_builder", "--rebuild"],
                   timeout=settings.SEMANTIC_REBUILD_TIMEOUT, check=True)
    subprocess.run([sys.executable, "-m", "ai.prefilter.train"],
                   timeout=settings.SEMANTIC_TRAIN_TIMEOUT, check=True)

    _mark_promoted([c.id for c in candidates])
    _update_dream_state(...)
    return {"promoted_count": len(candidates), "synthetic_file": str(path), ...}
```

### A.6 Wire Hooks

**MODIFY: `ai/scorer.py`** — after `conversation_scores` insert:

```python
if (
    settings.SEMANTIC_LEARNING_ENABLED
    and not result.get("red_flags")
    and result.get("compliance_score", 0) >= settings.SEMANTIC_MIN_SCORE
):
    asyncio.create_task(
        semantic_learner.evaluate_novelty(messages, agent_name, contact_name,
                                            funnel_tier, conv_id, scores, [], db_pool)
    )
```

**MODIFY: `ai/dream_worker.py`** — add new step in run cycle:

```python
if settings.SEMANTIC_LEARNING_ENABLED:
    try:
        result = auto_promoter.promote_pending_candidates(force=False)
        if result["promoted_count"] > 0:
            logger.info(f"[DreamWorker] Auto-learned from {result['promoted_count']} candidates")
    except Exception as e:
        logger.error(f"[DreamWorker] Auto-promote failed: {e}")
```

### A.7 Pipeline Integration

**MODIFY: `ai/prefilter/pipeline.py`**

```python
def _run_tiers(messages, agent_name, contact_name, funnel_tier, assigned_labels):
    # T1
    t1 = tier1_phrases.evaluate(...)
    if t1 and t1.decision == "short_circuit": return t1

    # T2 (now can short-circuit with flags too)
    if settings.PREFILTER_T2_LIVE or settings.PREFILTER_SHADOW_MODE:
        t2 = tier2_embedding.evaluate(...)
        if t2 and t2.decision == "short_circuit": return t2

    # T3 (now can short-circuit with flags too)
    if settings.PREFILTER_T3_LIVE or settings.PREFILTER_SHADOW_MODE:
        t3 = tier3_classifier.evaluate(...)
        if t3 and t3.decision == "short_circuit": return t3

    # T4 — terminal, always returns
    if settings.PREFILTER_T4_LIVE or settings.PREFILTER_SHADOW_MODE:
        t4 = tier4_flag_generator.evaluate(...)
        return t4  # never None

    # Phase A only: if all tiers off, fall back to escalate (Groq path)
    return PrefilterResult(tier_hit=5, decision="escalate", notes="all tiers disabled")
```

**Remove the bypass routes that force Groq escalation:**
- `pipeline.py:62-77` (label_requires_ai bypass) — let the ML tiers handle these
- `pipeline.py:79-96` (flag trigger bypass) — T4 already handles flag triggers deterministically

### A.8 Shadow-Mode Comparison Harness

**NEW FILE: `scripts/compare_ml_vs_groq.py`**

During Phase A, run both pipelines on every conversation and store both results:

```python
"""
Shadow comparison: for every conversation, run ML-only and Groq.
Compare and write disagreements to a CSV for review.

Run nightly:
    python scripts/compare_ml_vs_groq.py --since "yesterday"
"""

# Pull all prefilter_decisions rows where shadow_mode=True since cutoff
# Compare ML predicted_scores + flags vs Groq's actual conversation_scores
# Output:
#   reports/ml_vs_groq_<date>.csv with columns:
#     conv_id, ml_flags, groq_flags, flag_overlap, score_delta_avg, agreed
#   reports/ml_vs_groq_<date>_summary.json:
#     total, agreement_rate, flag_precision, flag_recall, score_mae
```

**Quality gate to advance to Phase B:**
- Flag precision ≥ 0.90 (ML's flags should appear in Groq's flags)
- Flag recall ≥ 0.85 (ML should catch most of Groq's flags)
- Score MAE ≤ 5.0 across all 4 dimensions
- Agreement on label_correct ≥ 0.95

### A.9 Settings Changes (Phase A)

```python
# config/settings.py — Phase A defaults
PREFILTER_SHADOW_MODE     = True   # ML runs but Groq still authoritative
PREFILTER_T1_LIVE         = True
PREFILTER_T2_LIVE         = True
PREFILTER_T3_LIVE         = True
PREFILTER_T4_LIVE         = True   # NEW
PREFILTER_T2_SIM_THRESHOLD = 0.85  # was 0.92 — more aggressive matching
PREFILTER_T3_LABEL_CONFIDENCE = 0.7  # NEW — multi-label predict gate
SEMANTIC_LEARNING_ENABLED = True
```

---

## Phase B — Flip Live with Groq Safety Net

### B.1 Settings Switch

```python
PREFILTER_SHADOW_MODE = False   # ML produces final scores
```

That's it. The pipeline already handles all 4 tiers as terminal-capable.

### B.2 Add Groq Manual-Override Endpoint

**MODIFY: `dashboard/app.py`** — add escape hatch:

```python
@app.post("/api/conversation/{conv_id}/rescore-with-groq")
async def rescore_with_groq(conv_id: int):
    """
    Force a Groq rescore for a conversation when manager disputes the ML score.
    Logs to audit_overrides table for tracking.
    """
    # Pull conversation, force-bypass prefilter, call analyze_conversation with override flag
    # Save result to conversation_scores with source='groq_override'
    # Auto-feed into semantic_candidates so ML learns from the disagreement
```

### B.3 Override Capture Loop

Every Groq override is the highest-quality training signal possible — the manager **explicitly disagreed with ML**. Hook these into `semantic_learner` regardless of novelty:

```python
# In rescore_with_groq endpoint
await semantic_learner.evaluate_novelty(
    ...,
    force_capture=True,  # bypass novelty check — always capture overrides
    capture_reason="manager_override",
)
```

**MODIFY: `database/schema.sql`** — add column:
```sql
ALTER TABLE semantic_candidates ADD COLUMN capture_reason TEXT DEFAULT 'novelty';
-- Values: 'novelty' | 'manager_override' | 'flag_disagreement'
```

### B.4 Quality Monitoring Dashboard

**MODIFY: `dashboard/templates/index.html`** — add panel:

```html
<div class="ml-quality-panel">
    <h3>🎯 ML Quality (last 7 days)</h3>
    <div>T1 hits: <b id="ml-t1">—</b></div>
    <div>T2 hits: <b id="ml-t2">—</b></div>
    <div>T3 hits: <b id="ml-t3">—</b></div>
    <div>T4 hits: <b id="ml-t4">—</b></div>
    <div>Groq overrides: <b id="ml-overrides">—</b></div>
    <div>Avg flag precision: <b id="ml-prec">—</b></div>
</div>
```

### B.5 Quality Gates to Advance to Phase C

After 2 weeks of Phase B operation:
- T4 hit rate < 5% (most traffic absorbed by T2/T3)
- Override rate < 2% (managers rarely disagreeing)
- Auto-learner has promoted ≥ 3 batches successfully
- Score distributions stable (compare 7-day window to 30-day window: drift < 3%)

---

## Phase C — Strip Groq Completely

### C.1 Code Deletions

**DELETE from `ai/analyzer.py`:**
- `KeyPoolManager` class (lines 336-593)
- `PooledKey` dataclass (lines 313-331)
- All `_db_*_groq_key` functions (lines 121-308)
- `_run_with_groq_pool` (lines 818-921)
- `_run_with_pinned_groq_key` (lines 924-1031)
- `_run_with_nim_key` (lines 768-815)
- `_run_batch_with_*` (lines 1383-1616)
- All `_GROQ_CALL_SEMAPHORE` references
- The Groq dispatch block in `analyze_conversation` (lines 696-766)

**KEEP in `ai/analyzer.py`:**
- The post-processing helpers (`_normalize_red_flags`, `_apply_label_guards`, `_agent_continued_after_opt_out`, `_agent_replied_after_first_soft_no`)
- `_WHITELIST_FLAG_OUTPUTS` (T4 imports this)
- All regex constants (T4 reuses)

**SIMPLIFIED `analyze_conversation`:**

```python
def analyze_conversation(messages, agent_name, contact_name="Contact",
                         assigned_labels=None, *, funnel_tier=None,
                         guidelines=None, conversation_id=None, db_pool=None,
                         **kwargs) -> dict:
    """ML-only audit. Always returns a result."""
    if not messages:
        return _empty_result("No messages to analyze", contact_name)

    from ai.prefilter import run_prefilter
    result = run_prefilter(
        messages, agent_name, contact_name,
        conversation_id=conversation_id,
        funnel_tier=funnel_tier or "NF",
        assigned_labels=assigned_labels or [],
        db_pool=db_pool,
    )

    # T4 guarantees a result; this should never trigger
    if result is None:
        return _empty_result("All ML tiers failed unexpectedly", contact_name)

    # Final post-processing guards (already deterministic — keep)
    flags = list(result.get("red_flags") or [])
    if "Continued texting after explicit opt-out." in flags and not _agent_continued_after_opt_out(messages):
        flags.remove("Continued texting after explicit opt-out.")
    if "Gave up after first no with zero rebuttal." in flags and _agent_replied_after_first_soft_no(messages):
        flags.remove("Gave up after first no with zero rebuttal.")
    result["red_flags"] = flags
    _apply_label_guards(result, messages)

    return result
```

### C.2 Provider Code Deletions

**DELETE entire files:**
- `ai/providers/groq_provider.py`
- `ai/providers/nim_provider.py`
- `ai/providers/base.py` (no providers left to inherit it)

### C.3 Database Cleanup

```sql
-- Archive (don't drop) — keep for forensics
ALTER TABLE api_keys RENAME TO api_keys_archived_groq;

-- Drop unused columns
ALTER TABLE conversation_scores ALTER COLUMN model_used DROP NOT NULL;
-- model_used will now always be 'prefilter_t1'..'prefilter_t4'
```

### C.4 Settings Cleanup

**DELETE from `config/settings.py`:**
- `GROQ_MODEL`
- `OLLAMA_URL`, `OLLAMA_MODEL`

**DELETE files:**
- `config/groq_keys.json` (if still exists)
- `config/agent_keys.json`

### C.5 Auto-Learner Adapts

The capture hook now triggers on **T4 outputs** (not Groq). Same novelty check, same dream worker promotion. The system keeps learning from itself.

```python
# ai/scorer.py — Phase C
if (
    settings.SEMANTIC_LEARNING_ENABLED
    and result.get("model_used", "").startswith("prefilter_t4")  # T4 = uncertain ML output
    and not result.get("red_flags")
    and result.get("compliance_score", 0) >= settings.SEMANTIC_MIN_SCORE
):
    asyncio.create_task(semantic_learner.evaluate_novelty(...))
```

**Why this still works:** T4 outputs are deterministic and rule-based. When T4 produces a clean high score, it's because patterns in the conversation matched the rules cleanly. That's a signal worth absorbing into T2/T3 so they handle it earlier next time.

### C.6 Documentation Updates

**MODIFY: `CLAUDE.md`** — replace Groq tech-stack mentions:

```markdown
- **AI Models**: 100% local — sentence-transformers (MiniLM), FAISS, scikit-learn (LogisticRegression, Ridge, OneVsRest)
- **No external AI API dependencies** (was Groq + NIM)
- **Cost**: $0/month
```

**MODIFY: `README.md`** — update architecture diagram.

**Update Obsidian** — `01-projects/texting-audit-automation.md`.

---

## Database Schema Changes

### `database/schema.sql` — Combined Migration

```sql
-- ── semantic_candidates (auto-learning queue) ────────────────────────────────
CREATE TABLE IF NOT EXISTS semantic_candidates (
    id                      SERIAL PRIMARY KEY,
    conversation_id         INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    funnel_tier             TEXT,
    embedding_hash          TEXT NOT NULL,
    top_similarity          DOUBLE PRECISION,
    nearest_conversation_id INTEGER,
    compliance_score        DOUBLE PRECISION,
    sentiment_score         DOUBLE PRECISION,
    professionalism_score   DOUBLE PRECISION,
    script_adherence_score  DOUBLE PRECISION,
    distinctive_phrases     JSONB,
    is_clean                BOOLEAN DEFAULT TRUE,
    promoted                BOOLEAN DEFAULT FALSE,
    promoted_at             TIMESTAMPTZ,
    rejected                BOOLEAN DEFAULT FALSE,
    rejected_reason         TEXT,
    capture_reason          TEXT DEFAULT 'novelty',  -- 'novelty' | 'manager_override' | 'flag_disagreement'
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(embedding_hash)
);
CREATE INDEX idx_sem_cand_promoted ON semantic_candidates(promoted, rejected, created_at);
CREATE INDEX idx_sem_cand_conv     ON semantic_candidates(conversation_id);

-- ── audit_overrides (Phase B+ — tracks manager Groq rescores) ────────────────
CREATE TABLE IF NOT EXISTS audit_overrides (
    id                  SERIAL PRIMARY KEY,
    conversation_id     INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    ml_result           JSONB NOT NULL,
    groq_result         JSONB NOT NULL,
    requested_by        TEXT,
    requested_at        TIMESTAMPTZ DEFAULT NOW(),
    disagreement_summary TEXT
);

-- ── conversation_scores additions ────────────────────────────────────────────
ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS source TEXT;
-- Values: 'groq' (legacy) | 'prefilter_t1' | 'prefilter_t2' | 'prefilter_t3' | 'prefilter_t4' | 'groq_override'
```

---

## Configuration Reference

### Final `config/settings.py` (Post-Phase C)

```python
# ─── ML Pre-Filter Pipeline ─────────────────────────────────
PREFILTER_ENABLED         = True   # Master switch
PREFILTER_SHADOW_MODE     = False  # Phase C: live
PREFILTER_T1_LIVE         = True
PREFILTER_T2_LIVE         = True
PREFILTER_T3_LIVE         = True
PREFILTER_T4_LIVE         = True   # Terminal — always on

# Tier thresholds
PREFILTER_T2_SIM_THRESHOLD     = 0.85
PREFILTER_T2_MIN_NEIGHBORS     = 3
PREFILTER_T3_MAX_FLAG_PROB     = 0.35
PREFILTER_T3_LABEL_CONFIDENCE  = 0.7   # NEW — multi-label gate
PREFILTER_T3_MIN_SCORE         = 75

# Embedding + artifacts
PREFILTER_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
PREFILTER_DIR             = PROJECT_ROOT / "ai" / "prefilter" / "artifacts"
PREFILTER_INDEX_PATH      = PREFILTER_DIR / "knn_index.faiss"
PREFILTER_INDEX_META_PATH = PREFILTER_DIR / "knn_index_meta.json"
PREFILTER_CLASSIFIER_PATH = PREFILTER_DIR / "classifier.joblib"
PREFILTER_REQUIRE_VALIDATION = False

# ─── Semantic Auto-Learning ─────────────────────────────────
SEMANTIC_LEARNING_ENABLED = True
SEMANTIC_MIN_SCORE        = 88.0
SEMANTIC_MAX_SIMILARITY   = 0.75
SEMANTIC_MIN_PROMOTE      = 5
SEMANTIC_MAX_PER_RUN      = 50
SEMANTIC_REBUILD_TIMEOUT  = 300   # seconds
SEMANTIC_TRAIN_TIMEOUT    = 600

# ─── Dream Worker ───────────────────────────────────────────
DREAM_WORKER_MIN_HOURS    = 4
DREAM_WORKER_MIN_SESSIONS = 3
DREAM_WORKER_MAX_RULES    = 5
LEARNED_RULES_PATH        = PROJECT_ROOT / "ai" / "learned_rules.json"
DREAM_STATE_PATH          = PROJECT_ROOT / "ai" / "dream_state.json"
```

**Removed from settings (Phase C):** `GROQ_MODEL`, `OLLAMA_URL`, `OLLAMA_MODEL`, all NIM-related env vars.

---

## File-by-File Changelog

| File | Phase | Action | Description |
|---|---|---|---|
| [database/schema.sql](database/schema.sql) | A | MODIFY | Add `semantic_candidates`, `audit_overrides`, `source` column |
| [database/db.py](database/db.py) | A | MODIFY | Add 6 helper methods (4 candidate, 2 override) |
| [config/settings.py](config/settings.py) | A | MODIFY | Add 8 new constants, change 5 defaults |
| [ai/prefilter/_guards.py](ai/prefilter/_guards.py) | A | NEW | Extract shared regex helpers from analyzer.py |
| [ai/prefilter/tier4_flag_generator.py](ai/prefilter/tier4_flag_generator.py) | A | NEW | Terminal flag generator |
| [ai/prefilter/semantic_learner.py](ai/prefilter/semantic_learner.py) | A | NEW | Capture novel high-quality conversations |
| [ai/prefilter/auto_promoter.py](ai/prefilter/auto_promoter.py) | A | NEW | Promote candidates → retrain |
| [ai/prefilter/__init__.py](ai/prefilter/__init__.py) | A | MODIFY | Export new modules |
| [ai/prefilter/tier2_embedding.py](ai/prefilter/tier2_embedding.py) | A | MODIFY | Short-circuit with flags + expose `search_top_k` |
| [ai/prefilter/tier3_classifier.py](ai/prefilter/tier3_classifier.py) | A | MODIFY | Multi-label flag prediction path |
| [ai/prefilter/train.py](ai/prefilter/train.py) | A | MODIFY | Train `flag_label_clf` multi-label classifier |
| [ai/prefilter/index_builder.py](ai/prefilter/index_builder.py) | A | MODIFY | Store `red_flags` in metadata |
| [ai/prefilter/pipeline.py](ai/prefilter/pipeline.py) | A | MODIFY | T4 in chain; remove bypass routes |
| [ai/scorer.py](ai/scorer.py) | A | MODIFY | Auto-learner capture hook |
| [ai/dream_worker.py](ai/dream_worker.py) | A | MODIFY | Auto-promoter step |
| [scripts/compare_ml_vs_groq.py](scripts/compare_ml_vs_groq.py) | A | NEW | Shadow-mode quality harness |
| [dashboard/app.py](dashboard/app.py) | A+B | MODIFY | 4 learning endpoints + override endpoint |
| [dashboard/templates/index.html](dashboard/templates/index.html) | A+B | MODIFY | Auto-learning + ML-quality panels |
| [ai/analyzer.py](ai/analyzer.py) | C | MODIFY | Strip Groq paths; thin wrapper around prefilter |
| [ai/providers/groq_provider.py](ai/providers/groq_provider.py) | C | DELETE | No longer needed |
| [ai/providers/nim_provider.py](ai/providers/nim_provider.py) | C | DELETE | No longer needed |
| [ai/providers/base.py](ai/providers/base.py) | C | DELETE | No providers left |
| [config/groq_keys.json](config/groq_keys.json) | C | DELETE | Archive to backup, then delete |
| [config/agent_keys.json](config/agent_keys.json) | C | DELETE | Archive to backup, then delete |
| [CLAUDE.md](CLAUDE.md) | C | MODIFY | Update tech stack, remove Groq mentions |
| [README.md](README.md) | C | MODIFY | Update architecture |
| [database/schema.sql](database/schema.sql) | C | MODIFY | Rename `api_keys` → `api_keys_archived_groq` |

**Total:** 7 new files, 14 modified, 5 deleted.

---

## Dashboard Integration

### New Endpoints (Phase A+B)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/learning/candidates` | List pending/promoted/rejected candidates |
| `GET` | `/api/learning/stats` | Cycle health metrics |
| `POST` | `/api/learning/promote-now` | Force-trigger promotion |
| `POST` | `/api/learning/reject/{id}` | Manually reject a candidate |
| `POST` | `/api/conversation/{id}/rescore-with-groq` | Phase B only — manager override |
| `GET` | `/api/ml/quality` | T1/T2/T3/T4 hit rates, override rate, flag precision |

### UI Panels

**Phase A:** Auto-Learning panel (pending/promoted counts, last cycle, manual trigger).

**Phase B:** ML Quality panel (per-tier hit rates, override count, score drift).

**Phase C:** Remove "AI Provider Status" panel from index.html (was showing Groq pool status).

---

## Testing & Quality Gates

### Phase A — Build

| Test | What it Validates |
|---|---|
| Unit: `tier4_flag_generator` on hand-crafted opt-out cases | Pattern detection works |
| Unit: `train.py` with `--test-split 0.2` | Multi-label classifier converges; per-flag F1 ≥ 0.8 |
| Unit: `semantic_learner.evaluate_novelty` thresholds | Settings honored; non-novel skipped |
| Unit: `auto_promoter.promote_pending_candidates(force=True)` | Subprocess success; candidates marked promoted |
| Integration: full audit run in shadow mode | All tiers populate `prefilter_decisions`; T4 always returns |
| **Quality Gate A→B**: shadow comparison on 1000 conversations | Flag precision ≥ 0.90, recall ≥ 0.85, MAE ≤ 5.0 |

### Phase B — Live with Net

| Test | What it Validates |
|---|---|
| Live audit run, 1 week | T4 hit rate < 5% |
| Override count tracking | < 2% of conversations rescored by managers |
| Auto-learner promotion count | ≥ 3 successful batches in 2 weeks |
| Score distribution drift | 7d vs 30d window: < 3% per metric |
| **Quality Gate B→C**: All above must hold for 14 consecutive days |

### Phase C — Strip

| Test | What it Validates |
|---|---|
| Full audit run with no internet | Pipeline still works (no Groq DNS lookups) |
| Code grep for `groq` / `nim` / `_pool` | Zero hits in `ai/` (except docstrings) |
| Dashboard loads without errors | `/api/ai/status` either gone or returns ML-only stats |
| Auto-learner still capturing | T4 outputs producing new candidates as expected |

---

## Safety Rails

| Risk | Mitigation | Phase |
|---|---|---|
| ML produces wrong scores in production | Shadow mode + comparison harness before flip | A |
| T4 generates wrong flags from rules | Reuse existing post-process guards in analyzer.py (already battle-tested) | A |
| Multi-label classifier overfits to rare flags | `class_weight="balanced"` + min positive count check before adding flag to predictions | A |
| Auto-learner pollutes training | Score gate (≥88) + flag gate (=[]) + manager reject endpoint | A |
| Dream worker breaks scoring | All hooks wrapped in try/except; capture failures never block scoring | A |
| Subprocess hangs during retrain | `subprocess.run(timeout=...)` with sensible defaults | A |
| Index rebuild corrupts on crash | Atomic file write (existing — `index_builder.py` writes `.tmp` then renames) | A |
| Phase B managers overwhelmed by overrides | Track override rate; if > 5%, automatic rollback to shadow mode | B |
| Phase C deletes break running audits | Phase C only after 14d of clean Phase B metrics; deploy in maintenance window | C |
| Hidden Groq dependency surfaces post-strip | `git grep -i groq` in CI; fail build if found outside archive paths | C |

---

## Rollback Plan

Each phase is reversible:

### Phase A → no changes (it's just shadow mode)
Toggle `PREFILTER_SHADOW_MODE=True` if anything goes wrong; everything reverts.

### Phase B → Phase A
```bash
# Set shadow mode back on
export PREFILTER_SHADOW_MODE=true
# Restart dashboard + scoring workers
# Groq paths still in code; pipeline naturally re-routes
```

### Phase C → Phase B (harder — requires git revert)
1. `git revert <phase-c-commits>`
2. Restore `api_keys_archived_groq` → `api_keys`
3. Restore `config/groq_keys.json` from backup
4. Restart workers

**Mitigation:** Tag the last Phase B commit (`pre-groq-strip`) so revert is one command.

---

## Open Decisions

These need a call before implementation starts:

1. **Should the auto-learner also capture flagged conversations?**
   - Pro: Better training signal for T2/T3 flag prediction
   - Con: Risk of compounding errors if Groq's flags were wrong
   - **Recommendation:** Capture flagged conversations only when manager validates via `validation_log.status='valid'`. Defer until Phase B.

2. **`PREFILTER_T2_SIM_THRESHOLD`: 0.85 or 0.80?**
   - 0.85: conservative, fewer T2 hits but higher precision
   - 0.80: more T2 hits, may copy flags from less-similar neighbors
   - **Recommendation:** Start at 0.85 in Phase A, monitor agreement rate, drop to 0.80 only if T4 hit rate stays > 10% in Phase B.

3. **When to trigger retrain — every dream cycle or only on threshold?**
   - Every cycle: training stays fresh but uses CPU constantly
   - Only on threshold: bursty CPU, longer between updates
   - **Recommendation:** Threshold-based (current plan). 5+ candidates is plenty; below that, signal is too noisy.

4. **Phase B duration?**
   - Plan says 2-4 weeks. Worth tightening?
   - **Recommendation:** 14 calendar days minimum + 7 days no-overrides streak. Whichever is later.

5. **Keep NIM as backup or strip everything?**
   - NIM is dedicated keys, not free-tier. Could keep as enterprise option.
   - **Recommendation:** Strip everything in Phase C. If NIM is ever needed, it's a re-add — not worth the dead code.

---

## Expected Outcomes

### After Phase A (Shadow, 2 weeks)
- All 4 tiers operational, all logged in `prefilter_decisions`
- ~50-200 candidates captured, 1-2 promotion cycles run
- Comparison reports show ML agrees with Groq on > 90% of conversations
- Dashboard shows learning panel with growing promoted count

### After Phase B (Live + Net, 4 weeks)
- T4 hit rate: 5-10% (and dropping as auto-learner trains T2/T3)
- Override rate: < 2%
- Groq calls: only during manual rescores
- Cost saved: ~80-95% reduction vs pre-Phase-A

### After Phase C (Stripped)
- 100% local inference
- $0/month AI cost
- Audit throughput: faster (no API latency)
- Privacy: zero conversation data leaves the host
- System self-improves continuously via auto-learner

### Long-Term (3+ months post-Phase C)
- T1 + T2 absorb most traffic; T3/T4 become rare
- Auto-learner reaches diminishing returns (most patterns seen)
- Optional: re-train every N weeks instead of dream-worker triggered
- Optional: explore distillation — train a smaller fine-tuned model on accumulated data for even faster T2

---

## Implementation Schedule

### Week 1 — Phase A Build (Days 1-7)
| Day | Tasks |
|---|---|
| 1 | DB schema migration + db.py helpers + settings additions |
| 2 | `_guards.py` extraction + `tier4_flag_generator.py` skeleton |
| 3 | T4 implementation + unit tests |
| 4 | Multi-label classifier in `train.py` + `tier3_classifier.py` upgrade |
| 5 | T2 flag-copy upgrade + index_builder metadata change |
| 6 | `semantic_learner.py` + scorer hook |
| 7 | `auto_promoter.py` + dream worker hook |

### Week 2 — Phase A Validation (Days 8-14)
| Day | Tasks |
|---|---|
| 8 | Pipeline integration + remove bypass routes |
| 9 | Shadow comparison harness (`scripts/compare_ml_vs_groq.py`) |
| 10-11 | Run shadow mode against live traffic, gather data |
| 12 | Dashboard learning panel + endpoints |
| 13 | Quality gate evaluation; tune thresholds if needed |
| 14 | Phase A → B decision meeting |

### Weeks 3-6 — Phase B Operation
- Flip `PREFILTER_SHADOW_MODE=False`
- Monitor daily for first 3 days
- Weekly quality reviews
- Auto-learner runs unattended

### Week 7 — Phase C Strip (1-2 days)
- Day 1: Code deletions, schema rename, config cleanup
- Day 2: Documentation, CI grep guard, final smoke test

**Total: ~7 weeks from start to fully Groq-free.**

---

## What This Plan Replaces

This document supersedes:
- ~~SEMANTIC_AUTO_LEARNING_PLAN.md~~ (auto-learning is now Section A.4-A.6 + Phase C.5)
- ~~ML_PREFILTER_FINAL_RESULTS.md~~ (Phase A absorbs the ML coverage work)

Both can be archived once this plan is approved.

---

*End of master plan. Ready to begin Phase A on approval.*
