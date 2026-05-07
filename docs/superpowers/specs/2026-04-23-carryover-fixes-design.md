# Design: 3 Carry-Over Fixes

**Date:** 2026-04-23
**Status:** Approved

---

## Fix 1 — BATCH_SYSTEM_PROMPT mismatch (already resolved)

**Finding:** The old `.replace()` mismatch was fixed in the same session it was discovered.
`_swap_output_format()` uses `re.sub(r"## PART 12 —.*", new_format, prompt, flags=re.DOTALL)`
at `ai/prompts.py:476`. This correctly replaces everything from `## PART 12 —` to end-of-string
with `BATCH_OUTPUT_FORMAT` (the multi-conversation array format).

**Action:** No code change. Update `03-decisions/Known Gotchas.md` to mark resolved.

---

## Fix 2 — Stale `cool_until` / `reserved_until` persisting across sessions

**File:** `ai/analyzer.py` → `KeyPoolManager._load_groq_pool()`

**Root cause:** `_db_cooldown_groq_key()` writes `cool_until = now() + Xs` to `api_keys`.
If the session is killed before the cooldown expires, the timestamp persists forever.
Next session's reservation query skips all stale-cooled keys → pool appears empty.

**Fix:** At the start of `_load_groq_pool()`, before the SELECT, run one cleanup UPDATE:

```sql
UPDATE api_keys
SET cool_until = NULL, reserved_until = NULL
WHERE provider = 'groq'
  AND agent_name IS NULL
  AND (
    cool_until      < now() - INTERVAL '30 minutes'
    OR reserved_until < now() - INTERVAL '30 minutes'
  )
```

Uses the existing `psycopg2` connection that is already open. 30-minute threshold means:
- Previous-session remnants (definitely expired) are cleared.
- Current-session cooldowns (< 30 min, still valid rate-limit signals) are preserved.

**Scope:** `ai/analyzer.py` only. No schema change. No migration needed.

---

## Fix 3 — Edit Account form missing Funnel Tier + Guidelines

Three coordinated changes:

### 3a. `EditAgentRequest` model — `dashboard/app.py:377`
Add two optional fields:
```python
class EditAgentRequest(BaseModel):
    name:        str = ""
    email:       str = ""
    password:    str = ""
    funnel_tier: str | None = None
    guidelines:  str | None = None
```

### 3b. `api_edit_agent()` endpoint — `dashboard/app.py:802`
- Parse `funnel_tier` (`.upper()` if present, else None) and `guidelines` (`.strip()` if present, else None).
- Extend the UPDATE statement to include `funnel_tier = $N` and `guidelines = $N+1`.
- Password update path stays conditional as before.
- Also verify `_fetch_agents_with_scores()` SELECTs `funnel_tier` and `guidelines` from `accounts`
  (needed so JS can pre-populate the form). Add columns if missing.

### 3c. HTML edit form — `dashboard/templates/index.html` manage section
- After the password field, add Funnel Tier select + Guidelines textarea, matching Add Agent form style:
  - Select: `id="manage-edit-tier"`, options None/NF/MF/WF
  - Textarea: `id="manage-edit-guidelines"`, same monospace/resize-vertical style
- In `selectManageAgent()`: populate both fields from `agent.funnel_tier` and `agent.guidelines`
- In `saveSelectedAgent()`: include `funnel_tier` and `guidelines` in the PUT body

---

## Parallelization Plan

All three fixes are fully independent — different files, no shared state:

| Task | Files touched | Agent |
|------|--------------|-------|
| Fix 1 | `03-decisions/Known Gotchas.md` (Obsidian) | Agent A |
| Fix 2 | `ai/analyzer.py` | Agent B |
| Fix 3 | `dashboard/app.py`, `dashboard/templates/index.html` | Agent C |

Each agent works in isolation. No merge conflicts expected.
