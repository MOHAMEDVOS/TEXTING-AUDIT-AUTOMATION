# Plan — Validation Loop + Flag-Aware ML Routing

**Date:** 2026-04-26
**Author:** Mohamed
**Status:** Draft — awaiting approval

---

## Why

Two problems are bleeding into the ML pre-filter and making it less trustworthy than it should be:

### Problem 1 — The training data is not always correct
Groq scores conversations, those scores get saved, the ML learns from them. But Groq is **not 100% accurate** — that is exactly why the dashboard already has a "Not Valid" button on red flags. Today, the only signal we capture is "this Groq result was wrong." There is no way to mark "this Groq result was right." Without explicit confirmation, every Groq score is treated as ground truth — which means the ML eventually learns from wrong data too.

### Problem 2 — The ML can short-circuit on conversations that probably need Groq
Currently every conversation passes through Tier 2 (similarity) and Tier 3 (predictor). The ML decides skip-or-escalate based purely on confidence + similarity. It does not look at whether the conversation **contains red-flag-triggering language**. Result: a conversation where the contact said "stop texting me" could still be matched by Tier 2 against past clean openers and short-circuited into a clean score. The Tier 1 phrase scan does protect against the well-known opt-out phrases, but anything more nuanced (aggressive language, $-amount mentions, agent mistakes) slips past it.

---

## Goal

Make the ML safer and the training data cleaner, with two coordinated changes:

1. **Validation loop:** capture both "valid" and "invalid" signals on Groq results. Only conversations confirmed-valid (or never marked-invalid for a configurable window) enter the ML training set.
2. **Flag-aware routing:** if the conversation has any red flags or any obvious flag-triggering content, skip the ML entirely and send straight to Groq.

Together these mean:
- ML index grows only with conversations the system trusts
- High-risk conversations always get full Groq analysis, never a copied score
- Manager corrections feed the system instead of dying in the database

---

## Non-Goals

- Building a full RAG pipeline (sending top-K examples to Groq). This is a separate future plan.
- Replacing Groq entirely. Groq remains the source of truth for hard cases.
- Re-scoring all historical conversations. Only new conversations adopt the new logic.
- UI redesign. We're adding one button and reusing existing styling.

---

## Architecture — The Two Changes

### Change 1 — The Validation Loop

Today:

```
Groq score → conversation_scores → flag_feedback (only for "Not Valid" clicks)
                ↓
          index_builder pulls everything
                ↓
              ML index
```

After:

```
Groq score → conversation_scores
                ↓
        Manager opens conversation
                ↓
        ┌───────┴────────┐
        │                │
  Click "Valid"   Click "Not Valid"
        │                │
        ↓                ↓
  validation_log    flag_feedback (existing)
   status=valid     status=invalid
        │                │
        └───────┬────────┘
                ↓
       index_builder filter:
       INCLUDE if validated=true
       EXCLUDE if any flag invalidated
                ↓
           ML index
```

### Change 2 — Flag-Aware Routing

Today:

```
Conversation → Tier 1 (phrase) → Tier 2 (similarity) → Tier 3 (predictor) → Groq
              All conversations follow the same path
```

After:

```
Conversation
     ↓
Pre-flight scan: does the contact text contain ANY known flag trigger?
   (opt-out phrases, profanity, "$" amounts, "wrong number", aggressive language)
     ↓
   ┌─┴─┐
   │   │
  YES  NO
   │   │
   ↓   ↓
 Groq  ML (Tier 1 → 2 → 3 → Groq fallback)
 always
```

Tier 1's existing opt-out detection stays — but the new pre-flight scan is **broader**: it detects anything that *could* trigger a flag, not just the narrow set Tier 1 was confident enough to short-circuit on.

---

## Database Changes

### New table — `validation_log`

```sql
CREATE TABLE IF NOT EXISTS validation_log (
    id              SERIAL PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES accounts(id),
    agent_name      TEXT NOT NULL,
    contact_name    TEXT NOT NULL,
    conversation_id BIGINT REFERENCES conversations(id),
    score_id        INTEGER REFERENCES conversation_scores(id),
    status          TEXT NOT NULL CHECK (status IN ('valid', 'invalid')),
    validated_by    TEXT,                    -- user identifier (future-proof; nullable for now)
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_id, contact_name)           -- one validation per agent+contact (latest wins via upsert)
);

CREATE INDEX IF NOT EXISTS idx_validation_log_status     ON validation_log(status);
CREATE INDEX IF NOT EXISTS idx_validation_log_agent      ON validation_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_validation_log_created_at ON validation_log(created_at DESC);
```

**Why upsert on (agent_id, contact_name):** if a manager flips a decision (clicked Valid, then later realized it was wrong, clicks Not Valid), the latest decision wins. We don't want stale conflicting rows.

### Migration file: `database/migrations/003_validation_log.sql`

Include the table above plus a comment explaining the relationship to `flag_feedback`.

### No changes to `flag_feedback`
The existing "Not Valid" button keeps writing there. We don't break that flow — we only add the parallel "Valid" path.

---

## Code Changes — File by File

### 1. New file: `ai/prefilter/flag_triggers.py`

The pre-flight scanner used by Change 2 (flag-aware routing).

```python
"""
Pre-flight scan: detect any text that COULD trigger a red flag.

This is broader than tier1_phrases — it doesn't try to short-circuit
or score the conversation. It only answers one question: "Should this
conversation skip the ML and go straight to Groq for a full audit?"

If ANY of the trigger patterns match in the contact's messages OR in the
agent's messages, return True. The pipeline then bypasses ML entirely.
"""

# Categories of triggers (combined into one pre-compiled regex list at module load):
#   1. Explicit opt-out phrases (reuse from tier1_phrases._OPT_OUT_PATTERNS)
#   2. Profanity / aggressive language
#   3. Dollar amounts in the agent's messages ("$200,000", "200k offer")
#   4. "Wrong number", "wrong person", "not me"
#   5. "Stop", "remove", "unsubscribe" variants
#   6. Aggressive demands ("sue you", "report you", "harassment")

def has_flag_trigger(messages: list[dict], agent_name: str) -> tuple[bool, str | None]:
    """
    Returns (True, matched_pattern_name) if any trigger fires, else (False, None).
    The matched pattern name is logged so we can audit which triggers fire most often.
    """
```

Test this in isolation before wiring it up.

### 2. Modify: `ai/prefilter/pipeline.py`

In `run_prefilter(...)`, add the pre-flight scan as the very first step **before** Tier 1:

```python
def run_prefilter(messages, agent_name, contact_name, conversation_id=None, *, db_pool=None):
    if not settings.PREFILTER_ENABLED:
        return None
    if not messages:
        return None

    # NEW — Pre-flight: any flag trigger? Skip ML entirely, send to Groq.
    if settings.PREFILTER_FLAG_ROUTING_ENABLED:
        from . import flag_triggers
        triggered, pattern = flag_triggers.has_flag_trigger(messages, agent_name)
        if triggered:
            logger.info(
                f"[Prefilter] {contact_name}: flag trigger '{pattern}' detected — "
                f"bypassing ML, escalating to Groq"
            )
            # Record the bypass decision for analytics
            if conversation_id is not None and db_pool is not None:
                _record_decision_async(
                    db_pool, conversation_id,
                    PrefilterResult(
                        tier_hit=0,                # 0 = pre-flight bypass
                        decision="escalate",
                        notes=f"flag-trigger:{pattern}",
                    ),
                )
            return None  # escalate to Groq

    # ... existing logic continues (Tier 1, 2, 3) ...
```

`tier_hit=0` is a new sentinel value meaning "ML was intentionally bypassed pre-flight." Update the `PipelineResult` doc comment to reflect the new value.

### 3. Modify: `config/settings.py`

Two new flags:

```python
# Pre-flight flag-trigger routing (Change 2). When True, conversations with
# any flag-triggering content bypass the ML entirely and go straight to Groq.
PREFILTER_FLAG_ROUTING_ENABLED = os.getenv("PREFILTER_FLAG_ROUTING_ENABLED", "true").lower() == "true"

# Validation-aware index builder (Change 1). When True, index_builder ONLY
# includes conversations whose validation_log.status='valid'. When False
# (current default), include all Groq-scored conversations.
PREFILTER_REQUIRE_VALIDATION = os.getenv("PREFILTER_REQUIRE_VALIDATION", "false").lower() == "true"
```

Default `PREFILTER_REQUIRE_VALIDATION=false` because we have zero validations today — flipping it on immediately would empty the index. Manager validates a few conversations first, then we flip it.

### 4. Modify: `ai/prefilter/index_builder.py`

Update `fetch_training_rows(conn)`:

```python
def fetch_training_rows(conn) -> list[dict]:
    base_sql = """
    SELECT c.id AS conversation_id, cs.compliance_score, ...
    FROM conversations c
    JOIN conversation_scores cs ON cs.conversation_id = c.id
    LEFT JOIN messages m ON m.conversation_id = c.id
    WHERE cs.model_used IS NOT NULL
      AND cs.model_used <> ''
      AND COALESCE(cs.source, 'groq') NOT IN ('prefilter_t1','prefilter_t2','prefilter_t3')
    """

    if settings.PREFILTER_REQUIRE_VALIDATION:
        # Only include conversations the manager confirmed valid
        base_sql += """
          AND EXISTS (
              SELECT 1 FROM validation_log vl
              JOIN contacts ct ON LOWER(ct.name) = LOWER(vl.contact_name)
              WHERE vl.agent_id = c.agent_id
                AND ct.id = c.contact_id
                AND vl.status = 'valid'
          )
        """

    # ALWAYS exclude conversations that were marked invalid (existing flag_feedback)
    # OR explicitly marked invalid in validation_log
    base_sql += """
      AND NOT EXISTS (
          SELECT 1 FROM validation_log vl
          JOIN contacts ct ON LOWER(ct.name) = LOWER(vl.contact_name)
          WHERE vl.agent_id = c.agent_id
            AND ct.id = c.contact_id
            AND vl.status = 'invalid'
      )
    GROUP BY c.id, cs.compliance_score, cs.sentiment_score,
             cs.professionalism_score, cs.script_adherence_score, cs.red_flags
    HAVING STRING_AGG(m.body, '') IS NOT NULL
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(base_sql)
        return list(cur.fetchall())
```

The "always exclude invalid" clause means: even with `PREFILTER_REQUIRE_VALIDATION=false`, the moment a manager marks a conversation invalid, it gets purged from the next index rebuild.

### 5. New API endpoint: `dashboard/app.py`

Mirror the existing `/api/redflag/invalid` pattern:

```python
class ValidationRequest(BaseModel):
    agent_id: int
    agent_name: str
    contact_name: str
    notes: str = ""

@app.post("/api/conversation/valid")
async def api_conversation_valid(body: ValidationRequest):
    """Mark a conversation's Groq score as confirmed valid."""
    if not body.agent_id or not body.contact_name.strip():
        raise HTTPException(status_code=400, detail="agent_id and contact_name required")
    try:
        async with app.state.pool.acquire() as conn:
            # Find the score row
            cs_row = await conn.fetchrow("""
                SELECT cs.id AS score_id, c.id AS conv_id
                FROM conversation_scores cs
                JOIN conversations c ON c.id = cs.conversation_id
                JOIN contacts ct ON ct.id = c.contact_id
                WHERE c.agent_id = $1 AND LOWER(ct.name) = LOWER($2)
                ORDER BY cs.id DESC
                LIMIT 1
            """, body.agent_id, body.contact_name)

            if not cs_row:
                raise HTTPException(status_code=404, detail="conversation not found")

            await conn.execute("""
                INSERT INTO validation_log
                    (agent_id, agent_name, contact_name, conversation_id, score_id, status, notes)
                VALUES ($1, $2, $3, $4, $5, 'valid', $6)
                ON CONFLICT (agent_id, contact_name) DO UPDATE
                    SET status = 'valid',
                        score_id = EXCLUDED.score_id,
                        conversation_id = EXCLUDED.conversation_id,
                        notes = EXCLUDED.notes,
                        created_at = NOW()
            """, body.agent_id, body.agent_name, body.contact_name,
                cs_row["conv_id"], cs_row["score_id"], body.notes.strip())

        logger.info(
            f"Conversation marked valid: agent={body.agent_name}, contact='{body.contact_name}'"
        )
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error in /api/conversation/valid")
        raise HTTPException(status_code=500, detail="internal error")
```

Also update `/api/redflag/invalid` to write into `validation_log` with `status='invalid'` so both signals live in one place. Keep writing to `flag_feedback` too (existing dashboards depend on it).

### 6. UI: `dashboard/templates/index.html`

Find the conversation row template that holds the "Not Valid" button (around line 2995). Add a sibling "Valid" button:

```html
<button class="btn btn-success btn-sm validate-btn"
        data-agent-id="{{ agent_id }}"
        data-contact="{{ contact_name }}"
        data-status="valid">
  ✓ Valid
</button>
<button class="btn btn-warning btn-sm validate-btn"
        data-agent-id="{{ agent_id }}"
        data-contact="{{ contact_name }}"
        data-status="invalid">
  ✗ Not Valid
</button>
```

JS handler:

```javascript
async function markValidation(agentId, agentName, contactName, status) {
  const url = status === 'valid' ? '/api/conversation/valid' : '/api/redflag/invalid';
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_id: agentId, agent_name: agentName, contact_name: contactName }),
  });
  if (res.ok) {
    // Update UI: badge the row "✓ Validated" or "✗ Invalid"
  }
}
```

The "Valid" button gets a green check; the existing "Not Valid" gets a red X. Both buttons disabled after click; row tagged with the chosen status.

### 7. Tests

Create `tests/test_flag_triggers.py`:
- Conversation with "stop texting" → triggered=True
- Conversation with "$200,000" in agent message → triggered=True
- Conversation with profanity → triggered=True
- Clean conversation ("hi, are you selling?") → triggered=False
- Empty conversation → triggered=False (no false positives)

Create `tests/test_validation_log.py`:
- Insert valid → `index_builder.fetch_training_rows()` includes it
- Insert invalid → `fetch_training_rows()` excludes it
- Flip valid → invalid → next call excludes it
- Two conversations same agent same contact → upsert keeps latest

Update `tests/test_prefilter_pipeline.py`:
- With `PREFILTER_FLAG_ROUTING_ENABLED=true`, a flagged conversation never reaches Tier 1/2/3
- With it `=false`, current behavior preserved

---

## Rollout

### Phase 1 — Ship the code (no behavior change)
- Migration runs, `validation_log` table exists
- New endpoint live, "Valid" button visible in UI
- `PREFILTER_FLAG_ROUTING_ENABLED=false` (off by default during shadow)
- `PREFILTER_REQUIRE_VALIDATION=false`
- Manager starts clicking "Valid" / "Not Valid" on recent audits

### Phase 2 — Turn on flag routing
- After 1–2 days of use, flip `PREFILTER_FLAG_ROUTING_ENABLED=true`
- Watch the prefilter_decisions table for tier_hit=0 rows
- Confirm: anything that goes pre-flight=True actually has a flag-worthy trigger

### Phase 3 — Turn on validation-required indexing
- After ~50 manager validations, flip `PREFILTER_REQUIRE_VALIDATION=true`
- Run `python -m ai.prefilter.index_builder --rebuild`
- Index size will drop from 911 → ~50 initially. That is fine — quality over quantity.
- ML will short-circuit less often until validation count grows. Re-eval weekly.

### Phase 4 — Long-term
- Dream Worker also reads `validation_log` (it already reads `flag_feedback`) and uses validated conversations as positive examples for learned rules.
- Eventually the index grows to 500+ validated conversations and short-circuit rate climbs back to acceptable levels — but every short-circuit is now backed by human-confirmed data.

---

## Verification

1. **Pre-flight test:** open a conversation containing "stop texting." Confirm logs show `flag-trigger:opt-out` and the conversation went straight to Groq (no Tier 2/3 lookup).
2. **Validation test:** click Valid on 5 conversations. Run `index_builder --rebuild` with `PREFILTER_REQUIRE_VALIDATION=true`. Confirm the new index size matches the validated count.
3. **Invalidation test:** mark a previously-valid conversation as Not Valid. Rebuild. Confirm it's gone from the index.
4. **Regression:** with both flags off, the system behaves identically to today (run `scripts/eval_prefilter.py` before and after — same numbers).
5. **End-to-end:** `python main.py --single Charles --limit 5` with all flags on. Verify dashboard shows the new buttons and they work.

---

## Risk & Mitigation

| Risk | Likelihood | Mitigation |
|---|---|---|
| Pre-flight regex too aggressive — flags routine conversations | Medium | Start with conservative patterns (only opt-outs + dollar amounts). Watch tier_hit=0 rate; if >40%, tighten. |
| Manager forgets to click Valid → ML index empties | High initially | Keep `PREFILTER_REQUIRE_VALIDATION=false` until validation count crosses 50. |
| Two managers click opposite verdicts on same conversation | Low | Upsert keeps the latest. Add audit trail in `validated_by` field (already in schema). |
| Pre-flight scan adds latency to non-flagged conversations | Very low | Pure regex, ~1ms. Negligible vs. the 8s Groq call we save on flagged ones. |

---

## Open Questions

1. **Should the "Valid" button auto-trigger on certain low-risk patterns** (e.g., if ML already short-circuited and the manager never opens the conversation, do we treat unopened-after-7-days as implicitly valid)? Recommend: **no, for now.** Explicit clicks only. Implicit validation can come in Phase 4 if needed.

2. **Do we backfill validation status for all 911 existing conversations?** Recommend: **no.** Treat the new system as forward-looking. The 911 stay in the index under the old "trust Groq" rules until they get explicitly validated or invalidated.

3. **Where does "valid" get displayed in the dashboard?** Recommend: small green "✓ Validated" badge next to the conversation summary, mirroring the existing red "Has Invalid Flag" badge.

---

## Estimated effort

- Migration + new table: 30 min
- `flag_triggers.py` module + tests: 2 hours
- Pipeline integration: 30 min
- Settings + index_builder filter: 1 hour
- New API endpoint: 1 hour
- UI button + JS handler + styling: 2 hours
- End-to-end testing + docs: 2 hours

**Total: ~9 hours of focused work**, splittable into two sessions (backend day, frontend day).
