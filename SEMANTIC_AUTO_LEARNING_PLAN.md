# Semantic Auto-Learning Cycle — Implementation Plan

> **Goal:** Build a closed feedback loop where every Groq-scored conversation is checked for novelty against the FAISS index. Novel + high-quality conversations are captured as learning candidates, then periodically promoted into the ML training corpus. This grows Tier 2 + Tier 3 coverage over time without manual intervention.

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Data Flow](#data-flow)
4. [Database Changes](#database-changes)
5. [New Files](#new-files)
6. [Files to Modify](#files-to-modify)
7. [Configuration](#configuration)
8. [Dashboard Integration](#dashboard-integration)
9. [Safety Rails](#safety-rails)
10. [Implementation Order](#implementation-order)
11. [Testing Plan](#testing-plan)

---

## Overview

### The Problem
The ML pre-filter (Tier 1/2/3) currently catches ~65% of conversations. The remaining 35% escalate to Groq because:
- They contain novel phrases not seen in training data.
- They differ semantically from indexed conversations (cosine sim < threshold).
- The classifier hasn't learned the pattern yet.

When Groq scores one of these confidently and cleanly (score ≥ 88, zero red flags), that's exactly the kind of conversation the ML system **should** have caught. Right now we throw away that learning opportunity.

### The Fix
A **3-stage cycle**:

1. **CAPTURE** — Post-Groq hook detects novel high-quality conversations and saves them as candidates.
2. **PROMOTE** — Dream worker batches candidates and triggers retrain when threshold met.
3. **ABSORB** — Tier 2 (FAISS index) and Tier 3 (classifier) get rebuilt with the new examples.

Result: every Groq escalation that produces a clean high score teaches the ML system, eventually pushing escalation rate toward zero.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    SCORING PIPELINE                          │
├─────────────────────────────────────────────────────────────┤
│  conversation → prefilter → [escalate?] → Groq → score      │
│                                              │               │
│                                              ▼               │
│                                  ┌───────────────────────┐  │
│                                  │  semantic_learner.py  │  │
│                                  │  (post-score hook)    │  │
│                                  └───────────────────────┘  │
│                                              │               │
│                                              ▼               │
│              ┌──────────────────────────────────────┐       │
│              │  Embed → FAISS top-1 similarity      │       │
│              │  Novel? (sim < 0.75) AND clean?      │       │
│              └──────────────────────────────────────┘       │
│                                              │               │
│                                              ▼               │
│              ┌──────────────────────────────────────┐       │
│              │  INSERT into semantic_candidates     │       │
│              └──────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
                                              │
                                              │ (async, decoupled)
                                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    DREAM WORKER (cron)                       │
├─────────────────────────────────────────────────────────────┤
│  Every 4hr + 3 sessions:                                     │
│   • Existing: cluster flag_feedback → write learned_rules   │
│   • NEW: count unpromoted candidates                         │
│         if >= SEMANTIC_MIN_PROMOTE:                          │
│           • write synthetic_<date>_auto_training.json        │
│           • run index_builder --rebuild                      │
│           • run train.py                                     │
│           • mark candidates promoted=True                    │
└─────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────┐
│              ML TIERS (now smarter on next request)          │
├─────────────────────────────────────────────────────────────┤
│  Tier 2 FAISS index includes the new clean conversations    │
│  Tier 3 classifier weights updated with new examples        │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Flow

### Capture Phase (per conversation, < 50ms overhead)
```
1. ai/scorer.py — Groq returns scored result
2. result.compliance_score >= 88 AND result.red_flags == [] ?
   → No  : skip, move on
   → Yes : continue
3. semantic_learner.evaluate_novelty(messages, scores, ...)
4. Embed conversation with sentence-transformers
5. Search FAISS for top-1 neighbor
6. top_similarity < SEMANTIC_MAX_SIMILARITY (0.75) ?
   → No  : already covered, skip
   → Yes : it's novel — save candidate
7. Extract distinctive phrases (n-grams not in top-K nearest neighbors)
8. INSERT INTO semantic_candidates (...)
```

### Promote Phase (every 4hr via dream worker)
```
1. ai/dream_worker.py — wake up
2. existing: cluster flag_feedback into learned_rules
3. NEW step: _promote_semantic_candidates()
   a. SELECT * FROM semantic_candidates WHERE promoted=FALSE
   b. count < SEMANTIC_MIN_PROMOTE (5) ? → skip
   c. count >= SEMANTIC_MAX_PER_RUN (50) ? → trim to 50 oldest
   d. Build synthetic_<YYYYMMDD_HHMM>_auto_training.json:
      {
        "conversations": [{conversation_id, messages, account_name, funnel_tier}],
        "baselines":     [{conversation_id, outcome, red_flags=[], pillars_gathered, rebuttal_quality}]
      }
   e. Save to scripts/ — train.py auto-discovers via glob("synthetic_*_training.json")
   f. Subprocess: python -m ai.prefilter.index_builder --rebuild
   g. Subprocess: python -m ai.prefilter.train
   h. UPDATE semantic_candidates SET promoted=TRUE, promoted_at=NOW() WHERE id IN (...)
   i. Log to dream_state.json
```

### Absorb Phase (next prefilter call)
- Tier 2 loads new FAISS index on first request after rebuild (lazy reload).
- Tier 3 loads new classifier on first request after retrain.
- No restart needed — both tiers already use lazy loading with mtime checks.

---

## Database Changes

### File: `database/schema.sql`

Add this new table after the `flag_feedback` block:

```sql
-- ── semantic_candidates (auto-learning queue) ────────────────────────────────
-- Conversations Groq scored cleanly that the ML index didn't recognize.
-- Dream worker batches these and promotes them into the training corpus.
CREATE TABLE IF NOT EXISTS semantic_candidates (
    id                  SERIAL PRIMARY KEY,
    conversation_id     INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    funnel_tier         TEXT,
    embedding_hash      TEXT NOT NULL,
    top_similarity      DOUBLE PRECISION,
    nearest_conversation_id INTEGER,
    compliance_score    DOUBLE PRECISION,
    sentiment_score     DOUBLE PRECISION,
    professionalism_score DOUBLE PRECISION,
    script_adherence_score DOUBLE PRECISION,
    distinctive_phrases JSONB,
    is_clean            BOOLEAN DEFAULT TRUE,
    promoted            BOOLEAN DEFAULT FALSE,
    promoted_at         TIMESTAMPTZ,
    rejected            BOOLEAN DEFAULT FALSE,
    rejected_reason     TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(embedding_hash)
);

CREATE INDEX IF NOT EXISTS idx_sem_cand_promoted
    ON semantic_candidates(promoted, rejected, created_at);
CREATE INDEX IF NOT EXISTS idx_sem_cand_conv
    ON semantic_candidates(conversation_id);
```

**Migration command (one-time):**
```bash
psql -U postgres -d texting_audit -f database/schema.sql
```

### File: `database/db.py`

Add helper methods to the `Database` class:

```python
async def insert_semantic_candidate(
    self,
    conversation_id: int,
    funnel_tier: str,
    embedding_hash: str,
    top_similarity: float,
    nearest_conversation_id: int | None,
    scores: dict,
    distinctive_phrases: list[str],
) -> bool:
    """Insert a semantic candidate. Returns False if duplicate (hash conflict)."""

async def fetch_unpromoted_candidates(self, limit: int = 50) -> list[dict]:
    """Pull oldest unpromoted, unrejected candidates with their messages."""

async def mark_candidates_promoted(self, ids: list[int]) -> None:
    """Bulk mark candidates as promoted."""

async def reject_candidate(self, candidate_id: int, reason: str) -> None:
    """Manual reject — candidate never goes into training."""
```

---

## New Files

### 1. `ai/prefilter/semantic_learner.py`

**Purpose:** The capture half. Decides if a Groq-scored conversation is novel enough to learn from.

**Public API:**
```python
async def evaluate_novelty(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    funnel_tier: str,
    conversation_id: int,
    scores: dict,
    red_flags: list[str],
    db_pool,
) -> bool:
    """
    Returns True if this conversation was saved as a learning candidate.
    Returns False if it's already covered by the index, scored too low,
    had flags, or hit any safety guard.

    Fire-and-forget — never raises.
    """
```

**Internal logic:**

```python
1. Settings gate:
   if not settings.SEMANTIC_LEARNING_ENABLED: return False

2. Quality gate:
   if any(scores[k] < settings.SEMANTIC_MIN_SCORE for k in 4 keys): return False
   if red_flags: return False

3. Build text + hash:
   text = embedder.conversation_to_text(messages, agent_name)
   text = f"[{funnel_tier}]\n{text}"
   embedding_hash = embedder.text_hash(text)

4. Embed:
   vec = embedder.embed(text)
   if vec is None: return False

5. Load FAISS index (reuse tier2_embedding._load_index()):
   if no index loaded: save anyway (cold start case)
   else: search top-1 → top_sim, nearest_id

6. Novelty gate:
   if top_sim >= settings.SEMANTIC_MAX_SIMILARITY: return False

7. Extract distinctive phrases:
   phrases = _extract_distinctive_phrases(messages, top_neighbors)
   # n-grams (2-4 words) from this conversation that don't appear in
   # any of the top-5 nearest neighbor texts.
   # Limit to top 10 phrases by frequency.

8. Insert candidate:
   await db.insert_semantic_candidate(...)
   logger.info(f"[SemanticLearner] Captured novel pattern for conv={conversation_id} sim={top_sim:.3f}")
   return True
```

**Helper functions:**
- `_extract_distinctive_phrases(target_messages, neighbor_texts)` — TF-IDF-style novelty
- `_search_top_neighbors(vec, k=5)` — same FAISS search T2 already does, with k=5 for phrase comparison
- `_should_skip_already_pending(embedding_hash, db_pool)` — short-circuit if hash already in candidates table

**Threading note:** Same lazy-loaded FAISS index shared with `tier2_embedding.py`. Acquire `tier2_embedding._index_lock` before reading.

---

### 2. `ai/prefilter/auto_promoter.py`

**Purpose:** The promote half. Called by dream worker.

**Public API:**
```python
def promote_pending_candidates(force: bool = False) -> dict:
    """
    Run the promotion cycle:
      1. Fetch unpromoted candidates (sync DB call via psycopg2)
      2. Bail if count < SEMANTIC_MIN_PROMOTE (unless force=True)
      3. Write synthetic training file
      4. Rebuild index
      5. Retrain classifier
      6. Mark candidates promoted

    Returns: {
        "promoted_count": int,
        "synthetic_file": str,
        "index_rebuilt": bool,
        "classifier_retrained": bool,
        "errors": list[str],
    }
    """
```

**Internal logic:**

```python
1. Fetch candidates:
   conn = psycopg2.connect(DATABASE_URL)
   SELECT sc.*, c.id AS conv_id, ac.name AS agent_name, ct.name AS contact_name
   FROM semantic_candidates sc
   JOIN conversations c ON c.id = sc.conversation_id
   JOIN accounts ac ON ac.id = c.agent_id
   JOIN contacts ct ON ct.id = c.contact_id
   WHERE sc.promoted = FALSE AND sc.rejected = FALSE
   ORDER BY sc.created_at ASC
   LIMIT settings.SEMANTIC_MAX_PER_RUN

2. Threshold check:
   if len(candidates) < settings.SEMANTIC_MIN_PROMOTE and not force:
       return {"promoted_count": 0, ...}

3. Load messages for each candidate:
   SELECT sender, body, sent_at FROM messages WHERE conversation_id IN (...)
   Group by conversation_id.

4. Build synthetic JSON (shape matches train.py:64-76):
   payload = {
       "conversations": [
           {
               "conversation_id": c.conv_id,
               "account_name": c.agent_name,
               "funnel_tier": c.funnel_tier,
               "messages": [{sender, body, sent_at}, ...],
           }
           for c in candidates
       ],
       "baselines": [
           {
               "conversation_id": c.conv_id,
               "outcome": _infer_outcome_from_scores(c.scores),
               "red_flags": [],
               "pillars_gathered": [],
               "rebuttal_quality": "none",
           }
           for c in candidates
       ],
   }

5. Write file:
   ts = datetime.now().strftime("%Y%m%d_%H%M")
   path = PROJECT_ROOT / "scripts" / f"synthetic_{ts}_auto_training.json"
   with open(path, "w") as f: json.dump(payload, f)

6. Rebuild index:
   subprocess.run([sys.executable, "-m", "ai.prefilter.index_builder", "--rebuild"], check=True)

7. Retrain classifier:
   subprocess.run([sys.executable, "-m", "ai.prefilter.train"], check=True)

8. Mark promoted:
   UPDATE semantic_candidates SET promoted=TRUE, promoted_at=NOW() WHERE id IN (...)

9. Update dream_state.json:
   state["last_semantic_promotion"] = ts
   state["last_promoted_count"] = len(candidates)
```

**Failure handling:**
- Any subprocess failure → log error, do NOT mark candidates promoted (so they retry next cycle)
- Subprocess timeout: 5 min for index_builder, 10 min for train
- Wrap entire function in try/except; never let it crash dream worker

---

## Files to Modify

### File 1: `ai/scorer.py`

**Location:** Wherever `analyze_conversation()` returns and we save to `conversation_scores`. Look for the call site that handles the Groq result.

**What to add:** After successful score insert, fire-and-forget call to `semantic_learner.evaluate_novelty()`.

```python
# After: await db.insert_conversation_score(conv_id, result, ...)

if (
    settings.SEMANTIC_LEARNING_ENABLED
    and result.get("model_used", "").startswith("llama")  # Groq result, not prefilter
    and not result.get("red_flags")
    and result.get("compliance_score", 0) >= settings.SEMANTIC_MIN_SCORE
):
    try:
        from ai.prefilter import semantic_learner
        asyncio.create_task(
            semantic_learner.evaluate_novelty(
                messages=parsed_messages,
                agent_name=agent_name,
                contact_name=contact_name,
                funnel_tier=funnel_tier or "NF",
                conversation_id=conv_id,
                scores={
                    "compliance_score": result.get("compliance_score"),
                    "sentiment_score": result.get("sentiment_score"),
                    "professionalism_score": result.get("professionalism_score"),
                    "script_adherence_score": result.get("script_adherence_score"),
                },
                red_flags=result.get("red_flags") or [],
                db_pool=app.state.pool,
            )
        )
    except Exception as e:
        logger.debug(f"[Scorer] Semantic learning hook failed silently: {e}")
```

**Critical:** Wrap in try/except — capture failure must NEVER break scoring.

---

### File 2: `ai/dream_worker.py`

**What to add:** Call `auto_promoter.promote_pending_candidates()` as a new step in the dream cycle.

**Where:** In the main dream loop function, after the existing `flag_feedback` clustering step.

```python
def run_dream_cycle():
    # ── existing: rule generation from flag_feedback ──
    new_rules = _cluster_and_generate_rules()
    if new_rules:
        learned_rules.append_rules(new_rules)

    # ── NEW: semantic candidate promotion ──
    if settings.SEMANTIC_LEARNING_ENABLED:
        try:
            from ai.prefilter.auto_promoter import promote_pending_candidates
            promotion_result = promote_pending_candidates(force=False)
            if promotion_result["promoted_count"] > 0:
                logger.info(
                    f"[DreamWorker] Promoted {promotion_result['promoted_count']} "
                    f"semantic candidates → {promotion_result['synthetic_file']}"
                )
        except Exception as e:
            logger.error(f"[DreamWorker] Semantic promotion failed: {e}")

    # ── existing: update dream_state.json ──
    _save_dream_state(...)
```

**Threshold reuse:** Dream worker already gates on `4hr + 3 sessions`. Promotion inherits that — no extra cron needed.

---

### File 3: `ai/prefilter/__init__.py`

Export the new module:

```python
from . import semantic_learner  # noqa: F401
from . import auto_promoter     # noqa: F401
```

---

### File 4: `ai/prefilter/embedder.py`

**No changes** — already provides `embed()`, `text_hash()`, `conversation_to_text()`.

Reused as-is.

---

### File 5: `ai/prefilter/tier2_embedding.py`

**No changes to behavior** — but `semantic_learner.py` reuses its private helpers:
- `_load_index()` — share the loaded FAISS instance
- `_index_lock` — synchronize searches
- `_index` and `_index_meta` — read-only access

Consider exposing a small public helper:
```python
def search_top_k(vec: np.ndarray, k: int = 5) -> list[dict]:
    """Public wrapper around index.search() — returns list of {conversation_id, similarity, is_clean}."""
```

---

### File 6: `ai/prefilter/index_builder.py`

**Already pulls from `conversation_scores` for indexing.** No changes required for the cycle to work — once promoted candidates land in `synthetic_*_training.json`, train.py picks them up; once train.py finishes, the next index rebuild will include the new conversations because they're already in `conversation_scores` (Groq put them there).

**Optional improvement:** Add `--include-promoted-only` flag for debugging which conversations came from auto-learning.

---

### File 7: `ai/prefilter/train.py`

**No changes required.** Already loads all `synthetic_*_training.json` files via glob (line 64-76). New `synthetic_<ts>_auto_training.json` files get picked up automatically on next training run.

**Optional improvement:** Log which synthetic files contributed to the training run — helps audit trail:

```python
synthetic_paths = sorted(scripts_dir.glob("synthetic_*_training.json"))
logger.info(f"Training on {len(synthetic_paths)} synthetic batches:")
for p in synthetic_paths:
    logger.info(f"  • {p.name}")
```

---

### File 8: `config/settings.py`

Append to the `# ─── Dream Worker (Self-Learning) ───` section:

```python
# ─── Semantic Auto-Learning ─────────────────────────────────
# Closed-loop: novel high-quality Groq scores → captured as candidates →
# dream worker promotes batches into ML training corpus → retrain.
SEMANTIC_LEARNING_ENABLED = os.getenv("SEMANTIC_LEARNING_ENABLED", "true").lower() == "true"

# Only learn from confidently-good conversations.
# Score must be >= this on ALL 4 metrics, AND have zero red flags.
SEMANTIC_MIN_SCORE        = float(os.getenv("SEMANTIC_MIN_SCORE", "88.0"))

# Cosine similarity threshold for "novel". If top neighbor sim >= this,
# the conversation is already covered — skip. Lower = more aggressive learning.
SEMANTIC_MAX_SIMILARITY   = float(os.getenv("SEMANTIC_MAX_SIMILARITY", "0.75"))

# Minimum candidate count before triggering a promotion run.
# Avoids retraining for every 1-2 novel conversations.
SEMANTIC_MIN_PROMOTE      = int(os.getenv("SEMANTIC_MIN_PROMOTE", "5"))

# Safety cap per dream cycle — don't promote more than this in one go.
# Prevents one bad batch from poisoning the training corpus.
SEMANTIC_MAX_PER_RUN      = int(os.getenv("SEMANTIC_MAX_PER_RUN", "50"))

# Subprocess timeouts (seconds) for index rebuild + train.
SEMANTIC_REBUILD_TIMEOUT  = int(os.getenv("SEMANTIC_REBUILD_TIMEOUT", "300"))
SEMANTIC_TRAIN_TIMEOUT    = int(os.getenv("SEMANTIC_TRAIN_TIMEOUT",   "600"))
```

---

## Dashboard Integration

### File 9: `dashboard/app.py`

Add 4 endpoints for visibility + manual control.

#### `GET /api/learning/candidates`
List pending candidates with their scores and similarity.

```python
@app.get("/api/learning/candidates")
async def list_semantic_candidates(limit: int = 50, status: str = "pending"):
    """
    status: 'pending' | 'promoted' | 'rejected' | 'all'
    Returns candidate list with conversation context.
    """
    where = ""
    if status == "pending":
        where = "WHERE sc.promoted=FALSE AND sc.rejected=FALSE"
    elif status == "promoted":
        where = "WHERE sc.promoted=TRUE"
    elif status == "rejected":
        where = "WHERE sc.rejected=TRUE"

    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT sc.*, ct.name AS contact_name, ac.name AS agent_name
            FROM semantic_candidates sc
            LEFT JOIN conversations c ON c.id = sc.conversation_id
            LEFT JOIN contacts ct ON ct.id = c.contact_id
            LEFT JOIN accounts ac ON ac.id = c.agent_id
            {where}
            ORDER BY sc.created_at DESC
            LIMIT $1
        """, limit)
    return {"success": True, "data": [dict(r) for r in rows]}
```

#### `POST /api/learning/promote-now`
Manual trigger — bypass thresholds.

```python
@app.post("/api/learning/promote-now")
async def trigger_semantic_promotion():
    """Force-promote all pending candidates regardless of threshold."""
    from ai.prefilter.auto_promoter import promote_pending_candidates
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: promote_pending_candidates(force=True))
    return {"success": True, "data": result}
```

#### `POST /api/learning/reject/{candidate_id}`
Kick a bad candidate out before promotion.

```python
class RejectRequest(BaseModel):
    reason: str

@app.post("/api/learning/reject/{candidate_id}")
async def reject_semantic_candidate(candidate_id: int, body: RejectRequest):
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            "UPDATE semantic_candidates SET rejected=TRUE, rejected_reason=$1 WHERE id=$2",
            body.reason, candidate_id,
        )
    return {"success": True}
```

#### `GET /api/learning/stats`
Quick health check for the cycle.

```python
@app.get("/api/learning/stats")
async def semantic_learning_stats():
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE NOT promoted AND NOT rejected) AS pending,
                COUNT(*) FILTER (WHERE promoted) AS promoted_total,
                COUNT(*) FILTER (WHERE rejected) AS rejected_total,
                MAX(promoted_at) AS last_promotion_at,
                AVG(top_similarity) FILTER (WHERE NOT promoted AND NOT rejected) AS avg_pending_similarity
            FROM semantic_candidates
        """)
    return {"success": True, "data": dict(row)}
```

### File 10: `dashboard/templates/index.html` (optional UI)

Add a small panel under "Live key assignments":

```html
<div class="learning-panel">
    <h3>🧠 Auto-Learning</h3>
    <div id="learning-stats">
        <span class="stat">Pending: <b id="ll-pending">—</b></span>
        <span class="stat">Promoted: <b id="ll-promoted">—</b></span>
        <span class="stat">Last cycle: <b id="ll-last">—</b></span>
    </div>
    <button id="ll-promote-now">Promote Now</button>
</div>
```

JS:
```javascript
async function refreshLearningStats() {
    const r = await fetch("/api/learning/stats").then(x => x.json());
    if (r.success) {
        document.getElementById("ll-pending").textContent = r.data.pending;
        document.getElementById("ll-promoted").textContent = r.data.promoted_total;
        document.getElementById("ll-last").textContent = r.data.last_promotion_at || "never";
    }
}
setInterval(refreshLearningStats, 30000);
refreshLearningStats();

document.getElementById("ll-promote-now").onclick = async () => {
    if (!confirm("Force-promote all pending candidates?")) return;
    const r = await fetch("/api/learning/promote-now", {method: "POST"}).then(x => x.json());
    alert(JSON.stringify(r.data, null, 2));
    refreshLearningStats();
};
```

---

## Safety Rails

| Risk | Mitigation | Where |
|---|---|---|
| Garbage candidates pollute training | Score gate: ALL 4 metrics ≥ 88, zero flags | `semantic_learner.py` step 2 |
| Duplicate captures (same conversation re-scored) | `UNIQUE(embedding_hash)` constraint | `schema.sql` |
| Capture failure breaks scoring | Fire-and-forget `asyncio.create_task` + try/except | `scorer.py` hook |
| Runaway retrain spam | `SEMANTIC_MIN_PROMOTE=5` floor + dream worker's 4hr cooldown | `auto_promoter.py` |
| Bad cluster of 50 wrong-funnel candidates | `SEMANTIC_MAX_PER_RUN=50` ceiling + manual reject endpoint | `auto_promoter.py` + `app.py` |
| Subprocess hang on retrain | `subprocess.run(timeout=...)` | `auto_promoter.py` |
| Train fails after promotion → candidates not promoted in DB | Mark `promoted=TRUE` ONLY after train returns 0 | `auto_promoter.py` step 8 |
| Index rebuild fails midway | Atomic write — rebuild to temp file, rename on success | `index_builder.py` (already does this) |
| Unvalidated candidates absorbed | Optional gate: require `validation_log.status='valid'` when `PREFILTER_REQUIRE_VALIDATION=true` | `semantic_learner.py` step 2 (extension) |
| Cycle runs but nothing changes | Manifest tracking in `dream_state.json` + `manifest.json` | `auto_promoter.py` step 9 |

---

## Implementation Order

Build in this order so each piece is testable in isolation:

### Day 1 — Capture Half (~3 hours)
1. **DB table** — Add `semantic_candidates` to `schema.sql`, run migration.
2. **DB helpers** — Add 4 methods to `database/db.py`.
3. **Settings** — Add 6 constants to `config/settings.py`.
4. **`semantic_learner.py`** — Build the evaluator. Test in isolation with a few hand-picked conversations.
5. **Scorer hook** — Wire into `ai/scorer.py`. Run a small audit and verify rows appear in `semantic_candidates`.

**Checkpoint:** After 1 audit run, query `SELECT COUNT(*) FROM semantic_candidates`. Should be > 0 if any conversation was novel.

### Day 2 — Promote Half (~3 hours)
6. **`auto_promoter.py`** — Build the promoter. Test with `force=True` against current candidate set.
7. **Dream worker hook** — Wire into `ai/dream_worker.py`.
8. **Manual smoke test** — Run `python -m ai.dream_worker` and confirm:
   - Synthetic file written to `scripts/`
   - Index rebuilt (check `manifest.json` `built_at`)
   - Classifier retrained (check `manifest.json` `classifier.trained_at`)
   - Candidates marked `promoted=TRUE`

**Checkpoint:** After promotion, the next prefilter call should hit T2 with one of the previously-novel conversations and short-circuit (no Groq call). Verify in logs.

### Day 3 — Dashboard + Polish (~2 hours)
9. **Dashboard endpoints** — Add 4 endpoints to `app.py`.
10. **Optional UI panel** — Add stats panel to `index.html`.
11. **Documentation** — Update `CLAUDE.md` with the new cycle.
12. **Obsidian** — Add `04-how-to/Semantic Auto-Learning.md` runbook.

---

## Testing Plan

### Unit (run after each file)
- `semantic_learner.evaluate_novelty()` with mocked embedder → verify thresholds work
- `auto_promoter.promote_pending_candidates(force=True)` with 5 hand-inserted candidates
- DB helper methods round-trip

### Integration
- Full audit run with `SEMANTIC_LEARNING_ENABLED=true` → check candidate insertions
- `python -m ai.dream_worker` → check promotion outputs
- Re-run the same audit → previously-novel conversations should now hit T2 (verify via `prefilter_decisions.tier_hit=2`)

### End-to-end gate (must pass before marking the feature done)
1. Start with index containing N conversations.
2. Run audit on 100 fresh conversations. Record T2/T3 hit rate.
3. Run dream cycle with `force=True`.
4. Re-run audit on the same 100 conversations.
5. **T2 hit rate must increase**, **Groq call count must decrease**, **score distributions stay within 5% of original Groq scores** (no quality regression).

### Failure-mode tests
- Stop Postgres mid-promotion → no half-marked candidates
- Kill train subprocess → candidates remain unpromoted, retried next cycle
- Submit malformed candidate (manually) → reject endpoint works
- Set `SEMANTIC_MAX_SIMILARITY=0.99` → no candidates captured (sanity check)

---

## Files Touched — Summary

| File | Action | LOC est. |
|---|---|---|
| [database/schema.sql](database/schema.sql) | Add `semantic_candidates` table | ~25 |
| [database/db.py](database/db.py) | Add 4 helper methods | ~80 |
| [ai/prefilter/semantic_learner.py](ai/prefilter/semantic_learner.py) | NEW — capture logic | ~150 |
| [ai/prefilter/auto_promoter.py](ai/prefilter/auto_promoter.py) | NEW — promote logic | ~200 |
| [ai/prefilter/__init__.py](ai/prefilter/__init__.py) | Export new modules | +2 |
| [ai/prefilter/tier2_embedding.py](ai/prefilter/tier2_embedding.py) | Optional: expose `search_top_k()` | +10 |
| [ai/prefilter/train.py](ai/prefilter/train.py) | Optional: log synthetic files | +5 |
| [ai/scorer.py](ai/scorer.py) | Add capture hook after Groq | ~25 |
| [ai/dream_worker.py](ai/dream_worker.py) | Add promote step | ~15 |
| [config/settings.py](config/settings.py) | Add 6 constants | ~20 |
| [dashboard/app.py](dashboard/app.py) | Add 4 endpoints | ~80 |
| [dashboard/templates/index.html](dashboard/templates/index.html) | Optional UI panel | ~40 |
| **Total new code** | | **~650 LOC** |

---

## Post-Implementation: Expected Outcomes

After 1 week of operation with steady audit traffic:
- **T2 coverage** climbs from 65% → ~80% (estimated based on long-tail decay)
- **Groq calls per audit** drop ~25%
- **Auto-learning candidates** accumulate at ~10-30/day depending on conversation diversity
- **Dream worker** runs 2-4x/day; promotes 1-2 batches per active day

After 1 month:
- T2/T3 combined coverage targets **90%+**
- Plateau in novel candidate rate (system has seen most patterns)
- Sets the stage for full Groq elimination — see separate plan.

---

## Open Questions / Decisions Needed

1. **Should we auto-promote on similarity ≥ 0.95** (strict duplicates, near-zero risk) **without dream worker batching?** Could add same-day learning at the cost of more frequent retraining.
2. **Should we capture flagged conversations too** for a future "negative example" classifier? Current plan only learns from clean. Worth considering once T4 (flag generator) is on the roadmap.
3. **How aggressive should `SEMANTIC_MAX_SIMILARITY` be?** 0.75 is conservative. 0.85 would capture far more but risks duplicate-ish patterns. Recommend starting at 0.75 and tuning by watching the candidate stream.
4. **Index rebuild cost** — current FAISS index rebuilds in ~30s for ~5k vectors. At 50k vectors this becomes ~5 min. Acceptable for dream worker cadence; revisit if dataset grows past 100k.

---

*End of plan. Ready to implement when you give the go-ahead.*
