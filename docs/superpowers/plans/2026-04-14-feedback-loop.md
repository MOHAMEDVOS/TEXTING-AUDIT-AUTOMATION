# Feedback Loop — Suppress Invalid AI Flags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent false-positive red flags from ever appearing in audit reports by (1) patching the AI prompt with 8 learned correction rules and (2) adding a scorer-level filter that strips any flag matching a known-invalid pattern from `flag_feedback`.

**Architecture:** Two independent layers — prompt patch eliminates root-cause AI mistakes; scorer filter is a safety net that reads `flag_feedback` at runtime and suppresses survivors. The filter is self-improving: every new flag marked invalid in the dashboard is automatically suppressed on the next run.

**Tech Stack:** Python 3.11, SQLite (stdlib `sqlite3`), pytest, existing `ai/prompts.py` + `ai/scorer.py`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `ai/prompts.py` | Modify | Add PART 13 correction rules to `SYSTEM_PROMPT` + update group B in PART 10 |
| `ai/scorer.py` | Modify | Add `_load_invalid_flag_patterns()`, `_filter_flags()`, wire both into scoring loop |
| `tests/test_flag_filter.py` | Create | Unit tests for `_filter_flags` and `_load_invalid_flag_patterns` |

---

## Task 1: Patch `ai/prompts.py` — PART 10 label equivalence + PART 13 learned corrections

**Files:**
- Modify: `ai/prompts.py`

### Context

`SYSTEM_PROMPT` is one long string. Two edits needed:

1. In **PART 10**, group B currently reads:
   ```
     B: "Not Interested", "Verified", "Not Interested + Verified"
   ```
   Add `"Verified, Not Interested"` to this group.

2. After PART 12 closing `"""` — insert PART 13 **before** the closing triple-quote of `SYSTEM_PROMPT`.
   (PART 12 ends with `"Not Following Lead Flow" → ...followed"""` — the new block goes right before that `"""`)

- [ ] **Step 1: Update group B in PART 10**

In `ai/prompts.py` find the exact line:
```
  B: "Not Interested", "Verified", "Not Interested + Verified"
```
Replace it with:
```
  B: "Not Interested", "Verified", "Not Interested + Verified", "Verified, Not Interested"
```

- [ ] **Step 2: Add PART 13 block before the closing `"""` of SYSTEM_PROMPT**

Find the very end of `SYSTEM_PROMPT` — the last line before the closing `"""`:
```
  "Not Following Lead Flow" → script_adherence_score < 65 OR skipped rebuttals, wrong sequence, NF out of order, follow-up timing violated"""
```
Replace it with:
```
  "Not Following Lead Flow" → script_adherence_score < 65 OR skipped rebuttals, wrong sequence, NF out of order, follow-up timing violated

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 13 — LEARNED CORRECTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
These rules are derived from real auditor feedback. They override general rules above.

RULE 1 — "Verified, Not Interested" is always a valid label.
  It is semantically identical to "Not Interested". Never flag it as wrong.
  ✗ NEVER: "Wrong label: assigned 'Verified, Not Interested' but should be 'Not Interested'"

RULE 2 — Asking the referral question after disinterest is CORRECT. Never flag it.
  The referral close ("Do you know anyone who wants to sell? I pay $1,000") is the required
  scripted exit after a lead declines. It is NOT continuing after disinterest.
  ✗ NEVER flag: "Continued messaging after clear disinterest" when the final message was the referral question.
  ✗ NEVER flag: "Ignored lead's implicit disinterest and continued with a referral question."
  ✗ NEVER flag: "Ignored lead's consistent disinterest and continued to send messages" when the agent used rebuttals then asked the referral question.

RULE 3 — Agent who stopped immediately after opt-out = perfect behavior. Never flag.
  If the agent sent NO further messages after an explicit opt-out (including profanity: "fuck no",
  "stop texting", etc.) AND set "Do Not Call" — that is correct. Only flag if agent actually sent
  another message AFTER the opt-out.
  ✗ NEVER flag: "Ignored explicit opt-out and continued sending messages" when agent stopped.
  ✗ NEVER flag: "Agent did not stop contacting the lead after explicit opt-out" when agent stopped.
  ✗ NEVER flag: "Continued contacting lead after being told it was a wrong number" when agent stopped.

RULE 4 — A cash RANGE is never a firm offer. Never flag a range.
  "129k-172k", "cash range like X-Y", or any two-number spread is NOT a firm offer.
  Only flag "Stated specific dollar amount as FIRM OFFER" when the agent gives ONE specific price
  as their definitive offer (e.g. "we will pay $180k"). Ranges are fine.
  ✗ NEVER flag: "Stated specific dollar amount as FIRM OFFER" for price ranges.
  ✗ NEVER flag: "Promised no timeline but revealed a cash range" — ranges are always acceptable.
  ✗ NEVER flag: "Pushed specific price range repeatedly without pre-qualifying" — ranges are not firm offers.

RULE 5 — "Missed Call" label is valid when lead's last action was a missed call.
  When the lead's final message or action was a missed call notification, "Missed Call" is the
  correct disposition. Never replace it with "Stopped Responding" in this case.
  ✗ NEVER flag: "Wrong label: assigned 'Missed Call' but should be 'Stopped Responding'" when lead sent a missed call.

RULE 6 — Agent explaining SMS-only workflow after lead calls ≠ messaging after silence.
  If a lead is calling and the agent sends one message explaining they can't take calls right now,
  that is correct behavior for an SMS-only outreach role. It is NOT spamming a silent lead.
  ✗ NEVER flag: "Continued messaging after lead stopped responding" in this context.

RULE 7 — "Bluffer" is the correct label for wildly unrealistic/joke prices.
  A price like $650 million on a standard residential property is clearly a bluff or joke.
  "Bluffer" is the correct disposition. Do NOT override with "Not Interested + Abv MV".
  Reserve "Abv MV" for genuinely high-but-plausible prices. Reserve "Bluffer" for clearly absurd ones.

RULE 8 — Label capitalization and spelling variants are always acceptable.
  "Do Not Call", "DO Not Call", "do not call" — all mean the same thing. Never flag spelling/
  capitalization differences as wrong labels.
  "Lead, Pushed" is a valid label when the lead showed genuine interest and was advanced to client.
  Never flag "Lead, Pushed" as should be "Not Interested" when lead was clearly interested."""
```

- [ ] **Step 3: Verify the file is valid Python**

Run:
```bash
.venv\Scripts\python.exe -c "import ai.prompts; print('OK', len(ai.prompts.SYSTEM_PROMPT), 'chars')"
```
Expected output: `OK <number above 8000> chars` with no errors.

- [ ] **Step 4: Commit**

```bash
git add ai/prompts.py
git commit -m "feat: add PART 13 learned correction rules to AI prompt"
```

---

## Task 2: Write tests for the new scorer functions

**Files:**
- Create: `tests/test_flag_filter.py`

Write tests BEFORE implementing the functions. These tests import from `ai.scorer` which doesn't have the functions yet — they will all fail until Task 3.

- [ ] **Step 1: Create `tests/test_flag_filter.py`**

```python
"""Tests for _filter_flags and _load_invalid_flag_patterns in ai.scorer."""
import sqlite3
import pytest
from ai.scorer import _filter_flags, _load_invalid_flag_patterns


# ── _filter_flags ─────────────────────────────────────────────────────────────

def test_filter_flags_empty_patterns_returns_all():
    flags = ["Flag A", "Flag B"]
    assert _filter_flags(flags, set()) == ["Flag A", "Flag B"]


def test_filter_flags_empty_flags_returns_empty():
    patterns = {"some pattern"}
    assert _filter_flags([], patterns) == []


def test_filter_flags_exact_match_suppressed():
    flags = ["Ignored explicit opt-out and continued sending messages"]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_pattern_is_substring_of_flag_suppressed():
    # stored pattern is shorter (truncated in DB) but is contained in the real flag
    flags = ["Wrong label: assigned 'Verified, Not Interested' but should be 'Not Interested'"]
    patterns = {"wrong label: assigned 'verified, not int...' but should be 'not interested'"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_flag_is_substring_of_pattern_suppressed():
    # real flag is shorter than the stored pattern
    flags = ["Continued messaging after clear disinterest"]
    patterns = {"continued messaging after clear disinterest and kept pushing the script"}
    assert _filter_flags(flags, patterns) == []


def test_filter_flags_unrelated_flag_kept():
    flags = ["Lead said stop texting. Agent sent another message."]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    assert _filter_flags(flags, patterns) == ["Lead said stop texting. Agent sent another message."]


def test_filter_flags_mixed_keeps_only_clean():
    flags = [
        "Ignored explicit opt-out and continued sending messages",  # should be suppressed
        "Agent asked price before checking condition.",              # should be kept
    ]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    result = _filter_flags(flags, patterns)
    assert result == ["Agent asked price before checking condition."]


def test_filter_flags_case_insensitive():
    flags = ["IGNORED EXPLICIT OPT-OUT AND CONTINUED SENDING MESSAGES"]
    patterns = {"ignored explicit opt-out and continued sending messages"}
    assert _filter_flags(flags, patterns) == []


# ── _load_invalid_flag_patterns ───────────────────────────────────────────────

def test_load_invalid_flag_patterns_returns_set(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("""
        CREATE TABLE flag_feedback (
            id INTEGER PRIMARY KEY,
            red_flag TEXT NOT NULL
        )
    """)
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("Ignored explicit opt-out",))
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("Stated specific dollar amount as FIRM OFFER",))
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))

    assert "ignored explicit opt-out" in patterns
    assert "stated specific dollar amount as firm offer" in patterns
    assert len(patterns) == 2


def test_load_invalid_flag_patterns_lowercases(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE flag_feedback (id INTEGER PRIMARY KEY, red_flag TEXT NOT NULL)")
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("UPPER CASE FLAG"))
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))
    assert "upper case flag" in patterns


def test_load_invalid_flag_patterns_empty_table(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE flag_feedback (id INTEGER PRIMARY KEY, red_flag TEXT NOT NULL)")
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))
    assert patterns == set()


def test_load_invalid_flag_patterns_missing_db_returns_empty(tmp_path):
    db = tmp_path / "nonexistent.db"
    patterns = _load_invalid_flag_patterns(str(db))
    assert patterns == set()


def test_load_invalid_flag_patterns_skips_null_entries(tmp_path):
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE flag_feedback (id INTEGER PRIMARY KEY, red_flag TEXT)")
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", (None,))
    con.execute("INSERT INTO flag_feedback (red_flag) VALUES (?)", ("valid flag",))
    con.commit()
    con.close()

    patterns = _load_invalid_flag_patterns(str(db))
    assert patterns == {"valid flag"}
```

- [ ] **Step 2: Run tests to verify they all FAIL (functions not yet defined)**

```bash
.venv\Scripts\python.exe -m pytest tests/test_flag_filter.py -v 2>&1 | head -30
```
Expected: `ImportError` or `AttributeError` — `_filter_flags` and `_load_invalid_flag_patterns` don't exist yet.

---

## Task 3: Implement `_filter_flags` and `_load_invalid_flag_patterns` in `ai/scorer.py`

**Files:**
- Modify: `ai/scorer.py`

- [ ] **Step 1: Add the two functions to `ai/scorer.py`**

Add them right after the imports block, before `_check_overdue_unreads`. Insert after line 21 (`logger = logging.getLogger(__name__)`):

```python
# ── Invalid flag filter ───────────────────────────────────────────────────────

def _load_invalid_flag_patterns(db_path: str) -> set[str]:
    """
    Load all red_flag strings from flag_feedback as a set of lowercase patterns.
    Used to suppress known-invalid flags from new audit results.
    Returns empty set on any error so scoring always continues.
    """
    import sqlite3 as _sqlite3
    try:
        con = _sqlite3.connect(db_path)
        cur = con.execute("SELECT red_flag FROM flag_feedback")
        patterns = {row[0].lower().strip() for row in cur.fetchall() if row[0]}
        con.close()
        logger.debug(f"[Scorer] Loaded {len(patterns)} invalid flag patterns from flag_feedback")
        return patterns
    except Exception as e:
        logger.warning(f"[Scorer] Could not load invalid flag patterns: {e}")
        return set()


def _filter_flags(flags: list[str], patterns: set[str]) -> list[str]:
    """
    Remove any flag whose text fuzzy-matches a known-invalid pattern.

    Match logic (both sides lowercased):
      - flag is a substring of a pattern, OR
      - pattern is a substring of the flag
    Either direction catches truncated DB entries and slight wording variations.
    """
    if not patterns:
        return flags
    clean = []
    for flag in flags:
        f = flag.lower().strip()
        suppressed = any(f in p or p in f for p in patterns)
        if suppressed:
            logger.debug(f"[Scorer] Suppressed known-invalid flag: {flag!r}")
        else:
            clean.append(flag)
    return clean
```

- [ ] **Step 2: Run the tests — they should all pass now**

```bash
.venv\Scripts\python.exe -m pytest tests/test_flag_filter.py -v
```
Expected: all 13 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add ai/scorer.py tests/test_flag_filter.py
git commit -m "feat: add _filter_flags and _load_invalid_flag_patterns to scorer"
```

---

## Task 4: Wire the filter into `score_agent_conversations()`

**Files:**
- Modify: `ai/scorer.py`

Two integration points inside `score_agent_conversations()`.

- [ ] **Step 1: Load patterns at the top of `score_agent_conversations()`**

Find the line in `score_agent_conversations()`:
```python
    if not conversations:
        logger.info(f"[Scorer] {agent_name} — no conversations, skipping")
        return {}
```
Add pattern loading right after it:
```python
    if not conversations:
        logger.info(f"[Scorer] {agent_name} — no conversations, skipping")
        return {}

    invalid_patterns = _load_invalid_flag_patterns(str(DB_PATH))
```

- [ ] **Step 2: Filter after batch results**

Find the block that processes batch results (after `results = await asyncio.to_thread(analyze_batch, ...)`):
```python
        for i, result in enumerate(results):
            idx = batch_items[i][0] if i < len(batch_items) else "?"
            contact = result.get("contact_name") or "Contact"
```
Add the filter call right before this loop:
```python
        for result in results:
            result["red_flags"] = _filter_flags(result.get("red_flags") or [], invalid_patterns)

        for i, result in enumerate(results):
            idx = batch_items[i][0] if i < len(batch_items) else "?"
            contact = result.get("contact_name") or "Contact"
```

- [ ] **Step 3: Filter after wrong-label flag injection**

Find the wrong-label injection block (after `# ── Inject wrong-label as a red flag on each conversation`):
```python
    for r in per_convo:
        if r.get("label_correct") is False:
            wrong  = r.get("label_assigned") or "?"
            should = r.get("label_should_be") or "?"
            flag   = f"Wrong label: assigned '{wrong}' but should be '{should}'"
            flags  = list(r.get("red_flags") or [])
            if flag not in flags:
                flags.insert(0, flag)
            r["red_flags"] = flags
```
Add the filter immediately after this entire block:
```python
    # Strip any injected wrong-label flags that are known-invalid
    for r in per_convo:
        r["red_flags"] = _filter_flags(r.get("red_flags") or [], invalid_patterns)
```

- [ ] **Step 4: Run the full test suite to confirm nothing is broken**

```bash
.venv\Scripts\python.exe -m pytest tests/ -v
```
Expected: all tests PASS (19 existing + 13 new = 32 total).

- [ ] **Step 5: Commit**

```bash
git add ai/scorer.py
git commit -m "feat: wire invalid flag filter into score_agent_conversations"
```

---

## Task 5: Smoke test against real DB

**Files:** none changed — verification only.

- [ ] **Step 1: Confirm the 30 known patterns load correctly**

```bash
.venv\Scripts\python.exe -c "
from ai.scorer import _load_invalid_flag_patterns
from config.settings import DB_PATH
patterns = _load_invalid_flag_patterns(str(DB_PATH))
print(f'Loaded {len(patterns)} patterns:')
for p in sorted(patterns):
    print(f'  {p[:80]}')
"
```
Expected: prints 30 patterns (may be fewer if some were duplicates — unique set).

- [ ] **Step 2: Confirm filter suppresses a known false positive**

```bash
.venv\Scripts\python.exe -c "
from ai.scorer import _filter_flags, _load_invalid_flag_patterns
from config.settings import DB_PATH

patterns = _load_invalid_flag_patterns(str(DB_PATH))

# Known false positives from feedback
test_flags = [
    \"Wrong label: assigned 'Verified, Not Interested' but should be 'Not Interested'\",
    \"Ignored explicit opt-out and continued sending messages\",
    \"Stated specific dollar amount as FIRM OFFER\",
    \"Agent asked for price before checking condition.\",   # this one should STAY
]

result = _filter_flags(test_flags, patterns)
print('Kept flags:')
for f in result:
    print(f'  KEPT: {f}')
print(f'Suppressed: {len(test_flags) - len(result)} of {len(test_flags)}')
"
```
Expected output:
```
Kept flags:
  KEPT: Agent asked for price before checking condition.
Suppressed: 3 of 4
```

- [ ] **Step 3: Commit smoke test is clean (no file changes needed)**

No commit needed for this task — it's verification only.

---

## Self-Review

**Spec coverage:**
- Rule 1 (Verified Not Interested) → Task 1 Step 1 (group B) + Task 1 Step 2 (Rule 1 in PART 13) ✓
- Rules 2-8 → Task 1 Step 2 ✓
- `_load_invalid_flag_patterns` → Task 2 + Task 3 ✓
- `_filter_flags` → Task 2 + Task 3 ✓
- Wire into scorer after batch results → Task 4 Step 2 ✓
- Wire into scorer after wrong-label injection → Task 4 Step 3 ✓
- Self-improving loop → automatic via Task 4 (reads DB at runtime) ✓
- Success criterion 3 (legit flags not suppressed) → tested in `test_filter_flags_unrelated_flag_kept` ✓

**Placeholder scan:** No TBDs, no "implement later", no "similar to Task N". All code is complete. ✓

**Type consistency:** `_filter_flags(flags: list[str], patterns: set[str]) -> list[str]` — used identically in Tasks 3 and 4. `_load_invalid_flag_patterns(db_path: str) -> set[str]` — consistent across Tasks 3, 4, and 5. ✓
