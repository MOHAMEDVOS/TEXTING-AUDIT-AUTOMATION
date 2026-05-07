# ML Pre-Filter Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 4-tier local pre-filter (regex → embedding kNN → small classifier → Groq) in front of `analyze_conversation` so ~70-80% of clean conversations skip the expensive LLM call.

**Architecture:** New `ai/prefilter/` package with a single `run_prefilter(messages, ...) -> dict | None` entry point called from `ai/analyzer.py:analyze_conversation`. Local tiers can ONLY short-circuit clean conversations; any flag risk escalates to Groq. Decisions logged to a new `prefilter_decisions` table for shadow-mode evaluation. No existing Groq behavior is removed.

**Tech Stack:** Python 3.11, `sentence-transformers` (`all-MiniLM-L6-v2`, CPU), `faiss-cpu`, `xgboost`, existing `psycopg2`/`asyncpg`, pytest.

**Database state at plan time:** 1,430 scored conversations (1,135 clean / 295 flagged) — sufficient for Tier 2 kNN and Tier 3 classifier without Groq bootstrap.

---

## File Structure

### New files (created by this plan)

| Path | Responsibility |
|---|---|
| `ai/prefilter/__init__.py` | Re-exports `run_prefilter`, `PrefilterResult` |
| `ai/prefilter/types.py` | `PrefilterResult` dataclass + `TierHit` enum |
| `ai/prefilter/tier1_phrases.py` | Pure-function regex/exact-phrase rules. No external state. |
| `ai/prefilter/tier2_embedding.py` | Embedding model singleton + FAISS kNN query |
| `ai/prefilter/tier3_classifier.py` | XGBoost loader + score/flag prediction |
| `ai/prefilter/index_builder.py` | CLI: rebuilds FAISS index from `conversation_scores` |
| `ai/prefilter/train.py` | CLI: trains Tier 3 XGBoost model |
| `ai/prefilter/pipeline.py` | Orchestrates tiers, writes to `prefilter_decisions` |
| `ai/prefilter/storage.py` | DB helpers: load training data, write decisions |
| `database/migrations/002_prefilter.sql` | New tables: `prefilter_decisions`, `conversation_embeddings` |
| `scripts/eval_prefilter.py` | Shadow-mode harness: agreement vs. Groq |
| `tests/test_prefilter_tier1.py` | Tier 1 unit tests |
| `tests/test_prefilter_tier2.py` | Tier 2 unit tests (with synthetic embeddings) |
| `tests/test_prefilter_tier3.py` | Tier 3 unit tests |
| `tests/test_prefilter_pipeline.py` | Pipeline integration tests |

### Modified files

| Path | Change |
|---|---|
| `config/settings.py` | Add prefilter config block |
| `ai/analyzer.py:541` | Call `run_prefilter(...)` first; fall through on None |
| `requirements.txt` | Add 4 deps |

---

## Task 1: Database Migration

**Files:**
- Create: `database/migrations/002_prefilter.sql`

- [ ] **Step 1: Write the migration SQL**

```sql
-- database/migrations/002_prefilter.sql
-- Tables for the ML pre-filter pipeline.
-- Run once: psql -U postgres -d texting_audit -f database/migrations/002_prefilter.sql

CREATE TABLE IF NOT EXISTS prefilter_decisions (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    contact_name    TEXT,
    tier_hit        SMALLINT NOT NULL,         -- 1, 2, 3, 4
    short_circuited BOOLEAN NOT NULL DEFAULT FALSE,
    confidence      REAL,
    predicted       JSONB,                     -- prefilter's prediction
    groq_actual     JSONB,                     -- filled in shadow mode after Groq runs
    agreement       REAL,                      -- 0.0–1.0, computed offline
    shadow_mode     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prefilter_conv ON prefilter_decisions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_prefilter_tier ON prefilter_decisions(tier_hit);
CREATE INDEX IF NOT EXISTS idx_prefilter_created ON prefilter_decisions(created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_embeddings (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    embedding       REAL[] NOT NULL,
    model_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_emb_model ON conversation_embeddings(model_name);
```

- [ ] **Step 2: Run the migration**

```bash
export PGPASSWORD=postgres
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -d texting_audit \
  -f "database/migrations/002_prefilter.sql"
```

Expected output: `CREATE TABLE` (×2), `CREATE INDEX` (×4).

- [ ] **Step 3: Verify the tables exist**

```bash
export PGPASSWORD=postgres
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -d texting_audit \
  -c "\d prefilter_decisions" -c "\d conversation_embeddings"
```

Expected: both tables shown with all columns + indexes.

- [ ] **Step 4: Commit**

```bash
git add database/migrations/002_prefilter.sql
git commit -m "db: add prefilter_decisions and conversation_embeddings tables"
```

---

## Task 2: Add Dependencies + Settings

**Files:**
- Modify: `requirements.txt`
- Modify: `config/settings.py`

- [ ] **Step 1: Add deps to requirements.txt**

Append to `requirements.txt`:

```
sentence-transformers>=2.7.0
faiss-cpu>=1.7.4
xgboost>=2.0.0
scikit-learn>=1.3.0
```

- [ ] **Step 2: Install**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
pip install -r requirements.txt
```

Expected: all 4 packages install cleanly. `sentence-transformers` will pull `transformers` + `torch` (~2GB).

- [ ] **Step 3: Add settings block to config/settings.py**

Append to the bottom of `config/settings.py` (after the Dream Worker block):

```python
# ─── Prefilter (ML pre-filter pipeline) ─────────────────────
PREFILTER_ENABLED       = os.getenv("PREFILTER_ENABLED", "true").lower() == "true"
PREFILTER_SHADOW_MODE   = os.getenv("PREFILTER_SHADOW_MODE", "true").lower() == "true"
PREFILTER_EMBED_MODEL   = os.getenv("PREFILTER_EMBED_MODEL", "all-MiniLM-L6-v2")
PREFILTER_T1_ENABLED    = os.getenv("PREFILTER_T1_ENABLED", "true").lower() == "true"
PREFILTER_T2_ENABLED    = os.getenv("PREFILTER_T2_ENABLED", "false").lower() == "true"
PREFILTER_T3_ENABLED    = os.getenv("PREFILTER_T3_ENABLED", "false").lower() == "true"
PREFILTER_T2_SIM_MIN    = float(os.getenv("PREFILTER_T2_SIM_MIN", "0.92"))
PREFILTER_T2_K_NEIGHBORS= int(os.getenv("PREFILTER_T2_K_NEIGHBORS", "3"))
PREFILTER_T3_FLAG_MAX   = float(os.getenv("PREFILTER_T3_FLAG_MAX", "0.15"))
PREFILTER_T3_SCORE_MIN  = float(os.getenv("PREFILTER_T3_SCORE_MIN", "75"))
PREFILTER_INDEX_PATH    = PROJECT_ROOT / "ai" / "prefilter" / "data" / "kNN.index"
PREFILTER_MODEL_PATH    = PROJECT_ROOT / "ai" / "prefilter" / "data" / "tier3_xgb.json"
```

- [ ] **Step 4: Verify settings load**

```bash
python -c "from config.settings import PREFILTER_ENABLED, PREFILTER_SHADOW_MODE, PREFILTER_EMBED_MODEL; print(PREFILTER_ENABLED, PREFILTER_SHADOW_MODE, PREFILTER_EMBED_MODEL)"
```

Expected: `True True all-MiniLM-L6-v2`

- [ ] **Step 5: Commit**

```bash
git add requirements.txt config/settings.py
git commit -m "deps: add sentence-transformers, faiss, xgboost; add PREFILTER_* settings"
```

---

## Task 3: Types Module

**Files:**
- Create: `ai/prefilter/__init__.py`
- Create: `ai/prefilter/types.py`

- [ ] **Step 1: Create the package init**

```python
# ai/prefilter/__init__.py
"""ML pre-filter pipeline: regex → embedding → classifier → Groq fallback."""
from ai.prefilter.types import PrefilterResult, TierHit

__all__ = ["PrefilterResult", "TierHit"]
```

- [ ] **Step 2: Create types module**

```python
# ai/prefilter/types.py
"""Shared types for the prefilter pipeline."""
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class TierHit(IntEnum):
    """Which tier produced the result."""
    T1_PHRASE = 1
    T2_EMBEDDING = 2
    T3_CLASSIFIER = 3
    T4_GROQ_ESCALATED = 4


@dataclass
class PrefilterResult:
    """
    Result returned by run_prefilter.

    short_circuited=True  → caller must NOT call Groq, use predicted
    short_circuited=False → caller MUST call Groq; predicted is advisory only
    """
    tier_hit: TierHit
    short_circuited: bool
    confidence: float
    predicted: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_jsonable(self) -> dict:
        return {
            "tier_hit": int(self.tier_hit),
            "short_circuited": self.short_circuited,
            "confidence": self.confidence,
            "predicted": self.predicted,
            "reason": self.reason,
        }
```

- [ ] **Step 3: Verify import works**

```bash
python -c "from ai.prefilter import PrefilterResult, TierHit; r = PrefilterResult(TierHit.T1_PHRASE, True, 1.0); print(r)"
```

Expected: prints a `PrefilterResult` dataclass.

- [ ] **Step 4: Commit**

```bash
git add ai/prefilter/__init__.py ai/prefilter/types.py
git commit -m "prefilter: add PrefilterResult and TierHit types"
```

---

## Task 4: Tier 1 — Phrase Matching (TDD)

**Files:**
- Create: `tests/test_prefilter_tier1.py`
- Create: `ai/prefilter/tier1_phrases.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prefilter_tier1.py
"""Tests for Tier 1 phrase-matching prefilter."""
import pytest
from ai.prefilter.tier1_phrases import check_tier1
from ai.prefilter.types import TierHit


def _msgs(*pairs):
    """Build a messages list from (sender, body) pairs."""
    return [{"sender": s, "body": b, "sent_at": None} for s, b in pairs]


def test_explicit_optout_after_agent_continued_returns_flag_escalation():
    """Lead opts out, agent then sends another message → MUST escalate to Groq."""
    msgs = _msgs(
        ("agent",   "Hi, are you the owner of 123 Main St?"),
        ("contact", "stop texting me"),
        ("agent",   "Just one more question — is it for sale?"),
    )
    result = check_tier1(msgs, agent_name="Agent", contact_name="Bob")
    assert result is not None
    assert result.tier_hit == TierHit.T1_PHRASE
    assert result.short_circuited is False    # MUST go to Groq
    assert "opt-out" in result.reason.lower()


def test_clean_short_conversation_returns_none():
    """No suspicious phrases → no Tier 1 decision (let other tiers handle)."""
    msgs = _msgs(
        ("agent",   "Hi, this is Sarah. Are you the owner of 123 Main?"),
        ("contact", "Yes, who's asking?"),
        ("agent",   "I'm a local investor. Would you consider selling?"),
        ("contact", "Maybe, what would you offer?"),
    )
    assert check_tier1(msgs, "Agent", "Bob") is None


def test_optout_with_no_subsequent_agent_message_short_circuits_clean():
    """Lead opts out, agent stops correctly → safe to short-circuit as clean."""
    msgs = _msgs(
        ("agent",   "Hi, are you the owner?"),
        ("contact", "remove me from your list"),
    )
    result = check_tier1(msgs, "Agent", "Bob")
    assert result is not None
    assert result.short_circuited is True
    assert result.predicted["compliance_score"] == 100


def test_empty_messages_returns_none():
    assert check_tier1([], "Agent", "Bob") is None


def test_only_agent_messages_returns_none():
    """No contact reply at all — let downstream tiers decide."""
    msgs = _msgs(("agent", "Hi"), ("agent", "Are you there?"))
    assert check_tier1(msgs, "Agent", "Bob") is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "c:/Users/vos/Desktop/TEXTING AUDIT AUTOMATION"
python -m pytest tests/test_prefilter_tier1.py -v
```

Expected: ImportError / ModuleNotFoundError on `ai.prefilter.tier1_phrases`.

- [ ] **Step 3: Implement `tier1_phrases.py`**

```python
# ai/prefilter/tier1_phrases.py
"""
Tier 1: regex / exact-phrase pre-filter.

Scope:
- Detect explicit opt-out language from the contact.
- If the contact opted out and the agent kept messaging → escalate to Groq
  (we do NOT auto-flag; Groq is authoritative for flag wording).
- If the contact opted out and the agent stopped correctly → short-circuit clean.
- Otherwise return None and let later tiers decide.

The opt-out vocabulary mirrors `ai/prompts.py:227,239`.
"""
from __future__ import annotations

import re
from typing import Optional

from ai.prefilter.types import PrefilterResult, TierHit

# Mirrors prompts.py:227 — these are the ONLY phrases that count as opt-out.
_OPTOUT_PHRASES = (
    "stop texting",
    "stop messaging",
    "remove me",
    "unsubscribe",
    "leave me alone",
    "don't contact me",
    "do not contact me",
    "stop bothering me",
    "stop contacting me",
    "take me off",
)
# Prefer longest match first so "stop texting me" beats bare "stop".
_OPTOUT_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _OPTOUT_PHRASES) + r")\b",
    re.IGNORECASE,
)


def _is_optout(text: str) -> bool:
    return bool(_OPTOUT_RE.search(text or ""))


def check_tier1(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
) -> Optional[PrefilterResult]:
    """
    Return PrefilterResult if Tier 1 is confident; None otherwise.

    short_circuited=False means "Groq must run".
    short_circuited=True  means "skip Groq, use predicted".
    """
    if not messages:
        return None

    # Find latest contact opt-out, if any.
    optout_idx: Optional[int] = None
    for i, m in enumerate(messages):
        if (m.get("sender") or "").lower() == "contact" and _is_optout(m.get("body", "")):
            optout_idx = i

    if optout_idx is None:
        return None

    # Did the agent send anything AFTER the opt-out?
    agent_after = any(
        (m.get("sender") or "").lower() == "agent"
        for m in messages[optout_idx + 1:]
    )

    if agent_after:
        # Compliance risk: escalate to Groq for authoritative flag wording.
        return PrefilterResult(
            tier_hit=TierHit.T1_PHRASE,
            short_circuited=False,
            confidence=0.99,
            predicted={"compliance_risk": True},
            reason="Contact used explicit opt-out phrase; agent continued messaging",
        )

    # Agent stopped correctly — clean compliance.
    return PrefilterResult(
        tier_hit=TierHit.T1_PHRASE,
        short_circuited=True,
        confidence=0.95,
        predicted={
            "compliance_score": 100,
            "sentiment_score": 80,
            "professionalism_score": 90,
            "script_adherence_score": 80,
            "red_flags": [],
            "label_correct": True,
            "summary": "Contact opted out; agent stopped correctly. Compliance clean.",
        },
        reason="Contact opt-out followed by agent silence",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_prefilter_tier1.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add ai/prefilter/tier1_phrases.py tests/test_prefilter_tier1.py
git commit -m "prefilter: tier 1 opt-out phrase detection with 5 unit tests"
```

---

## Task 5: Storage Helpers

**Files:**
- Create: `ai/prefilter/storage.py`

- [ ] **Step 1: Write the storage module**

```python
# ai/prefilter/storage.py
"""
Database helpers for the prefilter pipeline.
Synchronous (psycopg2) so it can be called from analyze_conversation
without dragging asyncio into the analyzer.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import psycopg2

from config.settings import DATABASE_URL
from ai.prefilter.types import PrefilterResult

logger = logging.getLogger(__name__)


def log_decision(
    conversation_id: Optional[int],
    contact_name: str,
    result: PrefilterResult,
    shadow_mode: bool,
    dsn: str = DATABASE_URL,
) -> None:
    """Insert a row into prefilter_decisions. Never raises — logs and swallows."""
    try:
        with psycopg2.connect(dsn) as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO prefilter_decisions
                      (conversation_id, contact_name, tier_hit, short_circuited,
                       confidence, predicted, shadow_mode)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        conversation_id,
                        contact_name,
                        int(result.tier_hit),
                        result.short_circuited,
                        result.confidence,
                        json.dumps(result.predicted),
                        shadow_mode,
                    ),
                )
    except Exception as e:
        logger.warning(f"[Prefilter] Could not log decision: {e}")


def load_training_rows(dsn: str = DATABASE_URL) -> list[dict]:
    """
    Load conversation_scores rows joined with their messages, ready for
    embedding + classifier training.

    Filters: only rows where model_used IS NOT NULL/empty (skips legacy unattributed rows).
    Returns a list of dicts: {conversation_id, transcript, scores..., red_flags, label_correct}.
    """
    rows = []
    with psycopg2.connect(dsn) as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT cs.conversation_id,
                       cs.compliance_score,
                       cs.sentiment_score,
                       cs.professionalism_score,
                       cs.script_adherence_score,
                       cs.red_flags,
                       cs.label_correct
                  FROM conversation_scores cs
                 WHERE cs.model_used IS NOT NULL
                   AND cs.model_used <> ''
            """)
            score_rows = cur.fetchall()

        with con.cursor() as cur:
            for sr in score_rows:
                conv_id = sr[0]
                cur.execute(
                    "SELECT sender, body FROM messages "
                    "WHERE conversation_id = %s ORDER BY id ASC",
                    (conv_id,),
                )
                msgs = cur.fetchall()
                if not msgs:
                    continue
                transcript = "\n".join(f"[{s}] {b}" for s, b in msgs)
                rows.append({
                    "conversation_id": conv_id,
                    "transcript": transcript,
                    "compliance_score": sr[1] or 0.0,
                    "sentiment_score": sr[2] or 0.0,
                    "professionalism_score": sr[3] or 0.0,
                    "script_adherence_score": sr[4] or 0.0,
                    "red_flags": sr[5] or [],
                    "label_correct": sr[6],
                })
    logger.info(f"[Prefilter] Loaded {len(rows)} training rows")
    return rows


def upsert_embedding(
    conversation_id: int,
    embedding: list[float],
    model_name: str,
    dsn: str = DATABASE_URL,
) -> None:
    """Cache an embedding in conversation_embeddings."""
    with psycopg2.connect(dsn) as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversation_embeddings (conversation_id, embedding, model_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (conversation_id) DO UPDATE
                  SET embedding = EXCLUDED.embedding,
                      model_name = EXCLUDED.model_name,
                      created_at = NOW()
                """,
                (conversation_id, embedding, model_name),
            )
```

- [ ] **Step 2: Verify import works against the live DB**

```bash
python -c "from ai.prefilter.storage import load_training_rows; rows = load_training_rows(); print(f'Loaded {len(rows)} rows'); print('First transcript len:', len(rows[0]['transcript']) if rows else 'N/A')"
```

Expected: `Loaded 911 rows` (matches the 911 model-attributed rows we saw earlier) + a transcript length number.

- [ ] **Step 3: Commit**

```bash
git add ai/prefilter/storage.py
git commit -m "prefilter: add storage helpers (decisions log, training data loader, embedding cache)"
```

---

## Task 6: Tier 2 — Embedding kNN (TDD)

**Files:**
- Create: `tests/test_prefilter_tier2.py`
- Create: `ai/prefilter/tier2_embedding.py`
- Create: `ai/prefilter/index_builder.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prefilter_tier2.py
"""Tests for Tier 2 embedding-similarity prefilter."""
import numpy as np
import pytest

from ai.prefilter.tier2_embedding import _decide_from_neighbors, build_transcript
from ai.prefilter.types import TierHit


def _msgs(*pairs):
    return [{"sender": s, "body": b} for s, b in pairs]


def test_build_transcript_joins_messages():
    msgs = _msgs(("agent", "Hi"), ("contact", "Hello"))
    out = build_transcript(msgs)
    assert "[agent] Hi" in out
    assert "[contact] Hello" in out


def test_decide_from_neighbors_all_clean_short_circuits():
    # Three neighbors, all clean (sim >= 0.92, no red_flags)
    neighbors = [
        {"sim": 0.95, "red_flags": [], "compliance_score": 100, "sentiment_score": 90,
         "professionalism_score": 95, "script_adherence_score": 90, "label_correct": True},
        {"sim": 0.94, "red_flags": [], "compliance_score": 100, "sentiment_score": 88,
         "professionalism_score": 92, "script_adherence_score": 88, "label_correct": True},
        {"sim": 0.93, "red_flags": [], "compliance_score": 100, "sentiment_score": 85,
         "professionalism_score": 90, "script_adherence_score": 85, "label_correct": True},
    ]
    result = _decide_from_neighbors(neighbors, sim_min=0.92, k=3)
    assert result is not None
    assert result.tier_hit == TierHit.T2_EMBEDDING
    assert result.short_circuited is True
    assert result.predicted["compliance_score"] == 100  # averaged


def test_decide_from_neighbors_one_flagged_escalates():
    """Any unresolved flag among the top-k neighbors → escalate."""
    neighbors = [
        {"sim": 0.95, "red_flags": [], "compliance_score": 100, "sentiment_score": 90,
         "professionalism_score": 95, "script_adherence_score": 90, "label_correct": True},
        {"sim": 0.94, "red_flags": ["Agent ignored opt-out"], "compliance_score": 0,
         "sentiment_score": 50, "professionalism_score": 80, "script_adherence_score": 60, "label_correct": True},
        {"sim": 0.93, "red_flags": [], "compliance_score": 100, "sentiment_score": 85,
         "professionalism_score": 90, "script_adherence_score": 85, "label_correct": True},
    ]
    result = _decide_from_neighbors(neighbors, sim_min=0.92, k=3)
    assert result is not None
    assert result.short_circuited is False    # MUST escalate
    assert "flag" in result.reason.lower()


def test_decide_from_neighbors_low_similarity_returns_none():
    neighbors = [
        {"sim": 0.80, "red_flags": [], "compliance_score": 100, "sentiment_score": 90,
         "professionalism_score": 95, "script_adherence_score": 90, "label_correct": True},
    ] * 3
    assert _decide_from_neighbors(neighbors, sim_min=0.92, k=3) is None


def test_decide_from_neighbors_too_few_neighbors_returns_none():
    neighbors = [
        {"sim": 0.99, "red_flags": [], "compliance_score": 100, "sentiment_score": 90,
         "professionalism_score": 95, "script_adherence_score": 90, "label_correct": True},
    ]  # only 1, need 3
    assert _decide_from_neighbors(neighbors, sim_min=0.92, k=3) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_prefilter_tier2.py -v
```

Expected: ModuleNotFoundError on `ai.prefilter.tier2_embedding`.

- [ ] **Step 3: Implement `tier2_embedding.py`**

```python
# ai/prefilter/tier2_embedding.py
"""
Tier 2: embedding-similarity prefilter (kNN over past scored conversations).

Loads a sentence-transformers model once, queries a FAISS index built by
`index_builder.py`. If the top-k neighbors are all clean and similar enough,
return their averaged scores. Any unresolved flag among the neighbors → escalate.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ai.prefilter.types import PrefilterResult, TierHit
from config.settings import (
    PREFILTER_EMBED_MODEL,
    PREFILTER_INDEX_PATH,
    PREFILTER_T2_K_NEIGHBORS,
    PREFILTER_T2_SIM_MIN,
)

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_LOCK = threading.Lock()
_INDEX = None
_NEIGHBOR_META: list[dict] = []  # parallel to FAISS index rows


def build_transcript(messages: list[dict]) -> str:
    """Format messages for embedding. Plain text, no timestamps."""
    return "\n".join(
        f"[{m.get('sender','?')}] {m.get('body','')}" for m in messages
    )


def _load_model():
    """Lazy-load the sentence-transformers model (one-time download on first run)."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer
            logger.info(f"[Prefilter T2] Loading embedding model: {PREFILTER_EMBED_MODEL}")
            _MODEL = SentenceTransformer(PREFILTER_EMBED_MODEL)
    return _MODEL


def _load_index():
    """Lazy-load the FAISS index + parallel metadata file."""
    global _INDEX, _NEIGHBOR_META
    if _INDEX is not None:
        return _INDEX
    import faiss
    idx_path = Path(PREFILTER_INDEX_PATH)
    meta_path = idx_path.with_suffix(".meta.json")
    if not idx_path.exists() or not meta_path.exists():
        logger.warning(f"[Prefilter T2] Index missing at {idx_path}; tier disabled")
        return None
    _INDEX = faiss.read_index(str(idx_path))
    with open(meta_path, "r", encoding="utf-8") as f:
        _NEIGHBOR_META = json.load(f)
    logger.info(f"[Prefilter T2] Loaded index with {_INDEX.ntotal} vectors")
    return _INDEX


def embed_text(text: str) -> np.ndarray:
    """Encode a single transcript and L2-normalize for cosine similarity via inner product."""
    model = _load_model()
    vec = model.encode([text], convert_to_numpy=True, normalize_embeddings=True)
    return vec.astype(np.float32)


def _decide_from_neighbors(
    neighbors: list[dict],
    sim_min: float,
    k: int,
) -> Optional[PrefilterResult]:
    """
    Pure decision logic from neighbor data — testable without FAISS.

    neighbors: list of dicts with keys: sim, red_flags, compliance_score,
               sentiment_score, professionalism_score, script_adherence_score,
               label_correct.
    """
    if len(neighbors) < k:
        return None

    top = neighbors[:k]
    if any(n["sim"] < sim_min for n in top):
        return None

    # Any flagged neighbor → escalate (safety rule)
    if any(n.get("red_flags") for n in top):
        return PrefilterResult(
            tier_hit=TierHit.T2_EMBEDDING,
            short_circuited=False,
            confidence=float(top[0]["sim"]),
            predicted={},
            reason="Top-k neighbor has red_flags; escalating to Groq",
        )

    avg = lambda key: float(np.mean([n[key] for n in top]))
    return PrefilterResult(
        tier_hit=TierHit.T2_EMBEDDING,
        short_circuited=True,
        confidence=float(np.mean([n["sim"] for n in top])),
        predicted={
            "compliance_score":      avg("compliance_score"),
            "sentiment_score":       avg("sentiment_score"),
            "professionalism_score": avg("professionalism_score"),
            "script_adherence_score": avg("script_adherence_score"),
            "red_flags": [],
            "label_correct": all(n.get("label_correct", True) for n in top),
            "summary": f"Matched k={k} clean past conversations (avg sim {np.mean([n['sim'] for n in top]):.3f})",
        },
        reason=f"k={k} clean neighbors at sim≥{sim_min}",
    )


def check_tier2(messages: list[dict]) -> Optional[PrefilterResult]:
    """Public entry: run Tier 2. Returns None if index missing or no decision."""
    idx = _load_index()
    if idx is None or not _NEIGHBOR_META:
        return None
    transcript = build_transcript(messages)
    if not transcript.strip():
        return None
    vec = embed_text(transcript)
    k_search = max(PREFILTER_T2_K_NEIGHBORS, 5)
    sims, ids = idx.search(vec, k_search)
    neighbors = []
    for rank in range(k_search):
        meta_idx = int(ids[0][rank])
        if meta_idx < 0 or meta_idx >= len(_NEIGHBOR_META):
            continue
        meta = _NEIGHBOR_META[meta_idx]
        neighbors.append({**meta, "sim": float(sims[0][rank])})
    return _decide_from_neighbors(
        neighbors,
        sim_min=PREFILTER_T2_SIM_MIN,
        k=PREFILTER_T2_K_NEIGHBORS,
    )
```

- [ ] **Step 4: Run tier2 unit tests**

```bash
python -m pytest tests/test_prefilter_tier2.py -v
```

Expected: 5 tests pass (all use `_decide_from_neighbors` which is FAISS-free).

- [ ] **Step 5: Implement the index builder**

```python
# ai/prefilter/index_builder.py
"""
CLI: build the FAISS kNN index from conversation_scores.

Run:
    python -m ai.prefilter.index_builder
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from ai.prefilter.storage import load_training_rows, upsert_embedding
from ai.prefilter.tier2_embedding import _load_model
from config.settings import PREFILTER_EMBED_MODEL, PREFILTER_INDEX_PATH

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def main() -> None:
    import faiss

    rows = load_training_rows()
    if not rows:
        logger.error("No training rows; aborting")
        return

    model = _load_model()
    transcripts = [r["transcript"] for r in rows]
    logger.info(f"Encoding {len(transcripts)} transcripts...")
    vecs = model.encode(
        transcripts,
        batch_size=32,
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=True,
    ).astype(np.float32)

    dim = vecs.shape[1]
    index = faiss.IndexFlatIP(dim)   # inner product == cosine on normalized vectors
    index.add(vecs)

    # Build parallel metadata
    meta = []
    for r, v in zip(rows, vecs):
        meta.append({
            "conversation_id": r["conversation_id"],
            "compliance_score": r["compliance_score"],
            "sentiment_score": r["sentiment_score"],
            "professionalism_score": r["professionalism_score"],
            "script_adherence_score": r["script_adherence_score"],
            "red_flags": r["red_flags"] if isinstance(r["red_flags"], list) else [],
            "label_correct": bool(r["label_correct"]) if r["label_correct"] is not None else True,
        })
        upsert_embedding(r["conversation_id"], v.tolist(), PREFILTER_EMBED_MODEL)

    out_path = Path(PREFILTER_INDEX_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(out_path))
    with open(out_path.with_suffix(".meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)

    logger.info(f"Wrote FAISS index to {out_path} ({index.ntotal} vectors, dim={dim})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Build the actual index against the live DB**

```bash
python -m ai.prefilter.index_builder
```

Expected: downloads `all-MiniLM-L6-v2` (~80MB) on first run, encodes ~911 rows, writes index file at `ai/prefilter/data/kNN.index`.

- [ ] **Step 7: Verify the index file exists**

```bash
ls -la ai/prefilter/data/
```

Expected: `kNN.index` and `kNN.meta.json` both present.

- [ ] **Step 8: Smoke-test live Tier 2**

```bash
python -c "
from ai.prefilter.tier2_embedding import check_tier2
msgs = [{'sender':'agent','body':'Hi, are you the owner of 123 Main?'},
        {'sender':'contact','body':'Yes who is this?'},
        {'sender':'agent','body':'I'm a local investor, would you sell?'}]
r = check_tier2(msgs)
print(r)
"
```

Expected: prints a `PrefilterResult` (could be None if no high-similarity match — that's fine, just verify no crash).

- [ ] **Step 9: Commit**

```bash
git add ai/prefilter/tier2_embedding.py ai/prefilter/index_builder.py tests/test_prefilter_tier2.py
git commit -m "prefilter: tier 2 embedding kNN with FAISS index builder"
```

---

## Task 7: Tier 3 — Classifier (TDD)

**Files:**
- Create: `tests/test_prefilter_tier3.py`
- Create: `ai/prefilter/tier3_classifier.py`
- Create: `ai/prefilter/train.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prefilter_tier3.py
"""Tests for Tier 3 classifier prefilter."""
import numpy as np
import pytest

from ai.prefilter.tier3_classifier import _decide_from_prediction
from ai.prefilter.types import TierHit


def test_decide_from_prediction_clean_short_circuits():
    pred = {
        "flag_prob": 0.05,
        "compliance_score": 95,
        "sentiment_score": 88,
        "professionalism_score": 92,
        "script_adherence_score": 80,
    }
    result = _decide_from_prediction(pred, flag_max=0.15, score_min=75)
    assert result is not None
    assert result.tier_hit == TierHit.T3_CLASSIFIER
    assert result.short_circuited is True
    assert result.predicted["compliance_score"] == 95


def test_decide_from_prediction_high_flag_prob_escalates():
    pred = {
        "flag_prob": 0.40,
        "compliance_score": 95,
        "sentiment_score": 88,
        "professionalism_score": 92,
        "script_adherence_score": 80,
    }
    result = _decide_from_prediction(pred, flag_max=0.15, score_min=75)
    assert result is not None
    assert result.short_circuited is False
    assert "flag_prob" in result.reason


def test_decide_from_prediction_low_score_escalates():
    pred = {
        "flag_prob": 0.05,
        "compliance_score": 95,
        "sentiment_score": 60,    # below threshold
        "professionalism_score": 92,
        "script_adherence_score": 80,
    }
    result = _decide_from_prediction(pred, flag_max=0.15, score_min=75)
    assert result is not None
    assert result.short_circuited is False
    assert "score" in result.reason.lower()


def test_decide_from_prediction_borderline_escalates():
    pred = {
        "flag_prob": 0.15,    # exactly at threshold — escalate (strict <)
        "compliance_score": 95,
        "sentiment_score": 80,
        "professionalism_score": 92,
        "script_adherence_score": 80,
    }
    result = _decide_from_prediction(pred, flag_max=0.15, score_min=75)
    assert result.short_circuited is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_prefilter_tier3.py -v
```

Expected: ModuleNotFoundError on `ai.prefilter.tier3_classifier`.

- [ ] **Step 3: Implement `tier3_classifier.py`**

```python
# ai/prefilter/tier3_classifier.py
"""
Tier 3: small classifier on top of the embedding.

5 outputs: flag_prob (binary classifier) + 4 score regressors.
If flag_prob < FLAG_MAX AND all scores >= SCORE_MIN → short-circuit clean.
Otherwise → escalate to Groq.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ai.prefilter.tier2_embedding import build_transcript, embed_text
from ai.prefilter.types import PrefilterResult, TierHit
from config.settings import (
    PREFILTER_MODEL_PATH,
    PREFILTER_T3_FLAG_MAX,
    PREFILTER_T3_SCORE_MIN,
)

logger = logging.getLogger(__name__)

_MODELS = None
_LOCK = threading.Lock()


def _load_models():
    """Load 5 booster files: flag classifier + 4 score regressors."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    with _LOCK:
        if _MODELS is None:
            base = Path(PREFILTER_MODEL_PATH).parent
            try:
                import xgboost as xgb
                models = {}
                paths = {
                    "flag":     base / "tier3_flag.json",
                    "compliance":      base / "tier3_compliance.json",
                    "sentiment":       base / "tier3_sentiment.json",
                    "professionalism": base / "tier3_prof.json",
                    "script_adherence": base / "tier3_script.json",
                }
                for k, p in paths.items():
                    if not p.exists():
                        logger.warning(f"[Prefilter T3] Missing model {p}; tier disabled")
                        return None
                    if k == "flag":
                        m = xgb.XGBClassifier()
                    else:
                        m = xgb.XGBRegressor()
                    m.load_model(str(p))
                    models[k] = m
                _MODELS = models
            except Exception as e:
                logger.warning(f"[Prefilter T3] Model load failed: {e}")
                return None
    return _MODELS


def _decide_from_prediction(
    pred: dict[str, float],
    flag_max: float,
    score_min: float,
) -> Optional[PrefilterResult]:
    """Pure decision logic, FAISS- and XGBoost-free for unit testing."""
    fp = pred["flag_prob"]
    scores = (
        pred["compliance_score"],
        pred["sentiment_score"],
        pred["professionalism_score"],
        pred["script_adherence_score"],
    )
    if fp >= flag_max:
        return PrefilterResult(
            tier_hit=TierHit.T3_CLASSIFIER,
            short_circuited=False,
            confidence=1.0 - fp,
            predicted=pred,
            reason=f"flag_prob={fp:.3f} ≥ {flag_max}; escalating",
        )
    if min(scores) < score_min:
        return PrefilterResult(
            tier_hit=TierHit.T3_CLASSIFIER,
            short_circuited=False,
            confidence=1.0 - fp,
            predicted=pred,
            reason=f"min score {min(scores):.1f} < {score_min}; escalating",
        )
    return PrefilterResult(
        tier_hit=TierHit.T3_CLASSIFIER,
        short_circuited=True,
        confidence=1.0 - fp,
        predicted={
            "compliance_score":       pred["compliance_score"],
            "sentiment_score":        pred["sentiment_score"],
            "professionalism_score":  pred["professionalism_score"],
            "script_adherence_score": pred["script_adherence_score"],
            "red_flags": [],
            "label_correct": True,
            "summary": "Tier-3 classifier predicts clean conversation",
        },
        reason=f"flag_prob={fp:.3f}, min score {min(scores):.1f}",
    )


def check_tier3(messages: list[dict]) -> Optional[PrefilterResult]:
    models = _load_models()
    if models is None:
        return None
    transcript = build_transcript(messages)
    if not transcript.strip():
        return None
    vec = embed_text(transcript)        # shape (1, dim), normalized
    pred = {
        "flag_prob": float(models["flag"].predict_proba(vec)[0, 1]),
        "compliance_score":       float(models["compliance"].predict(vec)[0]),
        "sentiment_score":        float(models["sentiment"].predict(vec)[0]),
        "professionalism_score":  float(models["professionalism"].predict(vec)[0]),
        "script_adherence_score": float(models["script_adherence"].predict(vec)[0]),
    }
    return _decide_from_prediction(pred, PREFILTER_T3_FLAG_MAX, PREFILTER_T3_SCORE_MIN)
```

- [ ] **Step 4: Run tier3 unit tests to verify pure logic passes**

```bash
python -m pytest tests/test_prefilter_tier3.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Implement the trainer**

```python
# ai/prefilter/train.py
"""
CLI: train Tier 3 XGBoost models from conversation_scores.

Run:
    python -m ai.prefilter.train
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ai.prefilter.storage import load_training_rows
from ai.prefilter.tier2_embedding import _load_model
from config.settings import PREFILTER_MODEL_PATH

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def main() -> None:
    import xgboost as xgb
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import roc_auc_score, mean_absolute_error

    rows = load_training_rows()
    if len(rows) < 100:
        logger.error(f"Only {len(rows)} rows; need at least 100. Aborting.")
        return

    model = _load_model()
    transcripts = [r["transcript"] for r in rows]
    logger.info(f"Encoding {len(transcripts)} transcripts...")
    X = model.encode(transcripts, batch_size=32, convert_to_numpy=True,
                     normalize_embeddings=True, show_progress_bar=True).astype(np.float32)

    y_flag = np.array([1 if (r["red_flags"] or []) else 0 for r in rows])
    y_comp = np.array([r["compliance_score"]       for r in rows], dtype=np.float32)
    y_sent = np.array([r["sentiment_score"]        for r in rows], dtype=np.float32)
    y_prof = np.array([r["professionalism_score"]  for r in rows], dtype=np.float32)
    y_scr  = np.array([r["script_adherence_score"] for r in rows], dtype=np.float32)

    base = Path(PREFILTER_MODEL_PATH).parent
    base.mkdir(parents=True, exist_ok=True)

    # 1) Flag classifier
    Xtr, Xte, ytr, yte = train_test_split(X, y_flag, test_size=0.2, random_state=42, stratify=y_flag)
    clf = xgb.XGBClassifier(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        eval_metric="auc", n_jobs=-1, random_state=42,
    )
    clf.fit(Xtr, ytr)
    auc = roc_auc_score(yte, clf.predict_proba(Xte)[:, 1])
    logger.info(f"Flag classifier holdout AUC: {auc:.3f}")
    clf.save_model(str(base / "tier3_flag.json"))

    # 2) Score regressors
    for name, y, fn in [
        ("compliance", y_comp, "tier3_compliance.json"),
        ("sentiment",  y_sent, "tier3_sentiment.json"),
        ("prof",       y_prof, "tier3_prof.json"),
        ("script",     y_scr,  "tier3_script.json"),
    ]:
        Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=42)
        reg = xgb.XGBRegressor(n_estimators=300, max_depth=4, learning_rate=0.05, n_jobs=-1, random_state=42)
        reg.fit(Xtr, ytr)
        mae = mean_absolute_error(yte, reg.predict(Xte))
        logger.info(f"{name} regressor holdout MAE: {mae:.2f}")
        reg.save_model(str(base / fn))

    logger.info(f"Saved 5 models to {base}/")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Train against the live DB**

```bash
python -m ai.prefilter.train
```

Expected: prints AUC for flag classifier (target ≥ 0.80) + MAE for 4 regressors (target ≤ 8). Saves 5 `.json` files.

- [ ] **Step 7: Verify trained files exist**

```bash
ls -la ai/prefilter/data/
```

Expected: `tier3_flag.json`, `tier3_compliance.json`, `tier3_sentiment.json`, `tier3_prof.json`, `tier3_script.json` all present.

- [ ] **Step 8: Commit**

```bash
git add ai/prefilter/tier3_classifier.py ai/prefilter/train.py tests/test_prefilter_tier3.py
git commit -m "prefilter: tier 3 XGBoost classifier + trainer"
```

---

## Task 8: Pipeline Orchestrator (TDD)

**Files:**
- Create: `tests/test_prefilter_pipeline.py`
- Create: `ai/prefilter/pipeline.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prefilter_pipeline.py
"""Integration tests for the prefilter pipeline orchestrator."""
from unittest.mock import patch, MagicMock

import pytest

from ai.prefilter.pipeline import run_prefilter
from ai.prefilter.types import PrefilterResult, TierHit


def _msgs(*pairs):
    return [{"sender": s, "body": b} for s, b in pairs]


@patch("ai.prefilter.pipeline.log_decision")
@patch("ai.prefilter.pipeline.PREFILTER_ENABLED", True)
@patch("ai.prefilter.pipeline.PREFILTER_T1_ENABLED", True)
@patch("ai.prefilter.pipeline.PREFILTER_T2_ENABLED", False)
@patch("ai.prefilter.pipeline.PREFILTER_T3_ENABLED", False)
def test_pipeline_disabled_returns_none(_log):
    """When PREFILTER_ENABLED is False, returns None immediately."""
    with patch("ai.prefilter.pipeline.PREFILTER_ENABLED", False):
        assert run_prefilter([{"sender":"agent","body":"hi"}], "A", "B", conversation_id=1) is None


@patch("ai.prefilter.pipeline.log_decision")
@patch("ai.prefilter.pipeline.check_tier1")
def test_pipeline_tier1_short_circuit(t1_mock, log_mock):
    t1_mock.return_value = PrefilterResult(
        TierHit.T1_PHRASE, short_circuited=True, confidence=0.95,
        predicted={"compliance_score": 100}, reason="opt-out clean",
    )
    result = run_prefilter(_msgs(("contact", "remove me")), "A", "B", conversation_id=42)
    assert result is not None
    assert result.tier_hit == TierHit.T1_PHRASE
    log_mock.assert_called_once()


@patch("ai.prefilter.pipeline.log_decision")
@patch("ai.prefilter.pipeline.check_tier1")
def test_pipeline_shadow_mode_returns_none_even_on_short_circuit(t1_mock, log_mock):
    """In shadow mode, log decision but tell caller to still call Groq."""
    t1_mock.return_value = PrefilterResult(
        TierHit.T1_PHRASE, short_circuited=True, confidence=0.95,
        predicted={"compliance_score": 100}, reason="opt-out clean",
    )
    with patch("ai.prefilter.pipeline.PREFILTER_SHADOW_MODE", True):
        result = run_prefilter(_msgs(("contact","remove me")), "A", "B", conversation_id=42)
    assert result is None    # caller MUST still call Groq
    log_mock.assert_called_once()    # but decision was logged


@patch("ai.prefilter.pipeline.log_decision")
@patch("ai.prefilter.pipeline.check_tier1")
def test_pipeline_tier1_returns_none_falls_through(t1_mock, log_mock):
    t1_mock.return_value = None
    result = run_prefilter(_msgs(("agent","hi")), "A", "B", conversation_id=1)
    assert result is None    # nothing decided → caller calls Groq
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_prefilter_pipeline.py -v
```

Expected: ModuleNotFoundError on `ai.prefilter.pipeline`.

- [ ] **Step 3: Implement `pipeline.py`**

```python
# ai/prefilter/pipeline.py
"""
Prefilter pipeline orchestrator.

run_prefilter() is called from ai.analyzer.analyze_conversation BEFORE the
Groq call. Returns:

    None                              → caller must call Groq normally
    PrefilterResult(short_circuit=T)  → caller skips Groq, uses .predicted
"""
from __future__ import annotations

import logging
from typing import Optional

from ai.prefilter.storage import log_decision
from ai.prefilter.tier1_phrases import check_tier1
from ai.prefilter.tier2_embedding import check_tier2
from ai.prefilter.tier3_classifier import check_tier3
from ai.prefilter.types import PrefilterResult, TierHit
from config.settings import (
    PREFILTER_ENABLED,
    PREFILTER_SHADOW_MODE,
    PREFILTER_T1_ENABLED,
    PREFILTER_T2_ENABLED,
    PREFILTER_T3_ENABLED,
)

logger = logging.getLogger(__name__)


def run_prefilter(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    conversation_id: Optional[int] = None,
) -> Optional[PrefilterResult]:
    """
    Run enabled tiers in order until one decides.

    Returns None when:
      - prefilter is disabled, OR
      - shadow mode is on (decision is logged but caller still hits Groq), OR
      - no tier reached a decision.

    Returns a short-circuit PrefilterResult only when:
      - prefilter is enabled, NOT in shadow mode, AND
      - a tier returned short_circuited=True.

    Returns an escalation PrefilterResult (short_circuited=False) when a tier
    actively flagged the conversation as risky — caller must still call Groq
    but can attach the reason for telemetry.
    """
    if not PREFILTER_ENABLED:
        return None

    decision: Optional[PrefilterResult] = None

    if PREFILTER_T1_ENABLED:
        try:
            decision = check_tier1(messages, agent_name, contact_name)
        except Exception as e:
            logger.warning(f"[Prefilter] tier1 failed: {e}")

    if decision is None and PREFILTER_T2_ENABLED:
        try:
            decision = check_tier2(messages)
        except Exception as e:
            logger.warning(f"[Prefilter] tier2 failed: {e}")

    if decision is None and PREFILTER_T3_ENABLED:
        try:
            decision = check_tier3(messages)
        except Exception as e:
            logger.warning(f"[Prefilter] tier3 failed: {e}")

    if decision is None:
        return None

    # Always log the decision for offline evaluation
    log_decision(
        conversation_id=conversation_id,
        contact_name=contact_name,
        result=decision,
        shadow_mode=PREFILTER_SHADOW_MODE,
    )

    if PREFILTER_SHADOW_MODE:
        # Telemetry only — caller still calls Groq
        return None

    # Live mode: only short_circuited=True actually skips Groq
    if decision.short_circuited:
        return decision
    return None
```

- [ ] **Step 4: Run pipeline tests**

```bash
python -m pytest tests/test_prefilter_pipeline.py -v
```

Expected: 4 tests pass.

- [ ] **Step 5: Run all prefilter tests**

```bash
python -m pytest tests/test_prefilter_*.py -v
```

Expected: 18 tests pass total (5 + 5 + 4 + 4).

- [ ] **Step 6: Commit**

```bash
git add ai/prefilter/pipeline.py tests/test_prefilter_pipeline.py
git commit -m "prefilter: pipeline orchestrator with tier ordering and shadow mode"
```

---

## Task 9: Hook Into analyze_conversation

**Files:**
- Modify: `ai/analyzer.py:541-591`

- [ ] **Step 1: Read current `analyze_conversation` signature**

Open `ai/analyzer.py` and confirm lines 541-591 still match the version recorded at plan time. If the function moved, locate the new line range before editing.

- [ ] **Step 2: Add the prefilter import**

Find the existing imports near the top of `ai/analyzer.py` (around line 22-24). After:

```python
from ai.providers.base import AIProvider, ProviderRateLimitError, ProviderQuotaExhaustedError
```

Add:

```python
from ai.prefilter.pipeline import run_prefilter
```

- [ ] **Step 3: Insert the prefilter call in `analyze_conversation`**

Find this block in `ai/analyzer.py` (currently around line 564-567):

```python
    if not messages:
        return _empty_result("No messages to analyze", contact_name)

    transcript = format_for_analysis(messages, agent_name, contact_name)
```

Replace it with:

```python
    if not messages:
        return _empty_result("No messages to analyze", contact_name)

    # Pre-filter: try to short-circuit before paying for Groq
    pf = run_prefilter(messages, agent_name, contact_name)
    if pf is not None and pf.short_circuited:
        logger.info(
            f"[Analyzer] Tier-{int(pf.tier_hit)} short-circuit for {contact_name} "
            f"(conf={pf.confidence:.2f}): {pf.reason}"
        )
        return _prefilter_to_result(pf, contact_name)

    transcript = format_for_analysis(messages, agent_name, contact_name)
```

- [ ] **Step 4: Add the `_prefilter_to_result` helper at the bottom of `ai/analyzer.py`**

Append at the end of the file:

```python
def _prefilter_to_result(pf, contact_name: str) -> dict:
    """
    Convert a short-circuit PrefilterResult into the dict shape the rest
    of the pipeline expects from analyze_conversation. Mirrors _empty_result
    but populated from the prefilter's predicted scores.
    """
    p = pf.predicted or {}
    return {
        "contact": contact_name,
        "scores": {
            "compliance_score":       p.get("compliance_score", 100),
            "sentiment_score":        p.get("sentiment_score", 80),
            "professionalism_score":  p.get("professionalism_score", 90),
            "script_adherence_score": p.get("script_adherence_score", 80),
        },
        "funnel_stage_reached": p.get("funnel_stage_reached", "wide"),
        "pillars_gathered":     p.get("pillars_gathered", []),
        "rebuttals_used":       p.get("rebuttals_used", []),
        "label_assigned":       p.get("label_assigned", ""),
        "label_correct":        p.get("label_correct", True),
        "label_should_be":      p.get("label_should_be", ""),
        "label_reason":         p.get("label_reason", ""),
        "red_flags":            p.get("red_flags", []),
        "actions_triggered":    p.get("actions_triggered", []),
        "summary":              p.get("summary", "Pre-filter short-circuit"),
        "model_used":           f"prefilter_t{int(pf.tier_hit)}",
        "error": None,
    }
```

- [ ] **Step 5: Verify nothing crashes by running existing tests**

```bash
python -m pytest tests/ -v --timeout=30
```

Expected: pre-existing tests still pass; new prefilter tests pass; **no test imports analyzer crashes**.

- [ ] **Step 6: Live smoke test in shadow mode**

Confirm `.env` (or environment) has the defaults:

```bash
python -c "from config.settings import PREFILTER_ENABLED, PREFILTER_SHADOW_MODE, PREFILTER_T1_ENABLED, PREFILTER_T2_ENABLED, PREFILTER_T3_ENABLED; print(PREFILTER_ENABLED, PREFILTER_SHADOW_MODE, PREFILTER_T1_ENABLED, PREFILTER_T2_ENABLED, PREFILTER_T3_ENABLED)"
```

Expected: `True True True False False`

Then run:

```bash
python main.py --status
```

Expected: no crash; no behavioral change (status command does not invoke analyzer).

- [ ] **Step 7: Verify a real audit run still works in shadow mode**

Pick any agent that has run before. From the project root:

```bash
python main.py --single "<agent name>" --limit 3
```

Expected:
- Run completes normally.
- Logs may show `[Prefilter] Tier-1` only when an opt-out is present.
- `prefilter_decisions` table receives rows.

Verify:

```bash
export PGPASSWORD=postgres
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -d texting_audit \
  -c "SELECT tier_hit, short_circuited, shadow_mode, COUNT(*) FROM prefilter_decisions GROUP BY 1,2,3 ORDER BY 1;"
```

Expected: at least one row, `shadow_mode=t`.

- [ ] **Step 8: Commit**

```bash
git add ai/analyzer.py
git commit -m "analyzer: hook prefilter pipeline into analyze_conversation"
```

---

## Task 10: Shadow-Mode Evaluation Script

**Files:**
- Create: `scripts/eval_prefilter.py`

- [ ] **Step 1: Write the script**

```python
# scripts/eval_prefilter.py
"""
Evaluate prefilter decisions vs. Groq actuals.

For each row in prefilter_decisions where conversation_id has a matching
conversation_scores row, compute agreement:
  - compliance/sentiment/prof/script: 1 - |pred - actual| / 100
  - red_flags: 1.0 if both empty / both non-empty, else 0.0

Updates prefilter_decisions.groq_actual + .agreement.

Run:
    python scripts/eval_prefilter.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from config.settings import DATABASE_URL

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def main() -> None:
    with psycopg2.connect(DATABASE_URL) as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT pd.id, pd.conversation_id, pd.predicted,
                       cs.compliance_score, cs.sentiment_score,
                       cs.professionalism_score, cs.script_adherence_score,
                       cs.red_flags
                  FROM prefilter_decisions pd
                  JOIN conversation_scores cs
                    ON cs.conversation_id = pd.conversation_id
                 WHERE pd.agreement IS NULL
            """)
            rows = cur.fetchall()
            logger.info(f"Evaluating {len(rows)} unscored decisions")

            for r in rows:
                pd_id, conv_id, predicted, c_a, s_a, p_a, sc_a, rf_a = r
                p = predicted or {}
                actuals = {
                    "compliance_score": c_a or 0.0,
                    "sentiment_score": s_a or 0.0,
                    "professionalism_score": p_a or 0.0,
                    "script_adherence_score": sc_a or 0.0,
                }
                score_agreements = []
                for k, a in actuals.items():
                    if k in p:
                        diff = abs(float(p[k]) - float(a))
                        score_agreements.append(max(0.0, 1.0 - diff / 100.0))

                pred_flags = bool(p.get("red_flags"))
                actual_flags = bool(rf_a)
                flag_agreement = 1.0 if pred_flags == actual_flags else 0.0
                score_agreements.append(flag_agreement)
                agreement = sum(score_agreements) / len(score_agreements) if score_agreements else 0.0

                cur.execute(
                    "UPDATE prefilter_decisions "
                    "SET groq_actual = %s::jsonb, agreement = %s WHERE id = %s",
                    (json.dumps({**actuals, "red_flags": rf_a}), float(agreement), pd_id),
                )

        # Summary stats per tier
        with con.cursor() as cur:
            cur.execute("""
                SELECT tier_hit,
                       COUNT(*),
                       AVG(agreement)::numeric(6,3) AS mean_agreement,
                       SUM(CASE WHEN short_circuited THEN 1 ELSE 0 END) AS short_circuits
                  FROM prefilter_decisions
                 WHERE agreement IS NOT NULL
                 GROUP BY tier_hit
                 ORDER BY tier_hit
            """)
            print("\nPER-TIER AGREEMENT:")
            print(f"{'tier':>6} {'n':>6} {'mean_agreement':>15} {'short_circuits':>16}")
            for tier, n, ma, sc in cur.fetchall():
                print(f"{tier:>6} {n:>6} {str(ma):>15} {sc:>16}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it (no-op if no overlap yet)**

```bash
python scripts/eval_prefilter.py
```

Expected: prints either `Evaluating 0 unscored decisions` (if shadow run hasn't happened yet) or a small number.

- [ ] **Step 3: Commit**

```bash
git add scripts/eval_prefilter.py
git commit -m "scripts: prefilter shadow-mode evaluation harness"
```

---

## Task 11: Verification — End-to-End Shadow Run

- [ ] **Step 1: Reset prefilter_decisions for a clean run**

```bash
export PGPASSWORD=postgres
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -d texting_audit \
  -c "TRUNCATE prefilter_decisions;"
```

- [ ] **Step 2: Enable Tier 2 + Tier 3 in shadow mode**

Set environment for the next run only (does not modify .env):

```bash
export PREFILTER_T2_ENABLED=true
export PREFILTER_T3_ENABLED=true
export PREFILTER_SHADOW_MODE=true
```

- [ ] **Step 3: Run a small audit**

```bash
python main.py --single "<agent name>" --limit 5
```

Expected: completes normally; logs show tier hits.

- [ ] **Step 4: Inspect decisions**

```bash
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -d texting_audit \
  -c "SELECT tier_hit, short_circuited, COUNT(*) FROM prefilter_decisions GROUP BY 1,2 ORDER BY 1;"
```

Expected: rows for one or more tiers.

- [ ] **Step 5: Compute agreement**

```bash
python scripts/eval_prefilter.py
```

Expected: prints PER-TIER AGREEMENT table with `mean_agreement` per tier.

- [ ] **Step 6: Acceptance gate**

Read the table. **Required for moving Tier 2 / Tier 3 to live mode:**
- Tier 1 mean_agreement ≥ 0.90
- Tier 2 mean_agreement ≥ 0.92 (only after ≥ 50 decisions accumulated)
- Tier 3 mean_agreement ≥ 0.90 (only after ≥ 50 decisions accumulated)

If any tier underperforms, do NOT promote it to live yet. Record the gap and either retrain (rerun Task 6 / Task 7 trainers after gathering more data) or tighten the threshold in `config/settings.py`.

- [ ] **Step 7: Commit observations as a markdown doc (optional)**

```bash
mkdir -p docs/superpowers/runs
cat > docs/superpowers/runs/$(date +%Y-%m-%d)-prefilter-shadow.md <<'EOF'
# Prefilter Shadow Run — <date>

Decisions: <N>
Per-tier agreement: <paste table>

Decisions:
- Tier 1: PROMOTE / HOLD
- Tier 2: PROMOTE / HOLD
- Tier 3: PROMOTE / HOLD

Notes:
- ...
EOF

git add docs/superpowers/runs/*.md
git commit -m "docs: prefilter shadow-run report"
```

---

## Task 12: Promote Tier 1 to Live (Gated on Task 11)

Only run this task after Task 11 acceptance gate for Tier 1 passes.

- [ ] **Step 1: Edit `.env` to flip shadow mode off**

In `.env`, add or update:

```
PREFILTER_ENABLED=true
PREFILTER_SHADOW_MODE=false
PREFILTER_T1_ENABLED=true
PREFILTER_T2_ENABLED=false
PREFILTER_T3_ENABLED=false
```

- [ ] **Step 2: Smoke test live mode**

```bash
python main.py --single "<agent>" --limit 3
```

Expected: completes; logs show `[Analyzer] Tier-1 short-circuit` for any opt-out conversations; Groq is NOT called for those.

- [ ] **Step 3: Verify Groq call count dropped**

```bash
"/c/Program Files/PostgreSQL/18/bin/psql.exe" -U postgres -d texting_audit \
  -c "SELECT tier_hit, short_circuited, shadow_mode, COUNT(*) FROM prefilter_decisions WHERE created_at > NOW() - INTERVAL '1 hour' GROUP BY 1,2,3;"
```

Expected: at least one row with `shadow_mode=f, short_circuited=t`.

- [ ] **Step 4: Commit any remaining changes**

```bash
git add -A
git commit -m "config: promote prefilter Tier 1 to live mode"
```

---

## Verification Plan (cross-task)

Final acceptance for the whole feature:

1. **Unit tests:** `python -m pytest tests/test_prefilter_*.py -v` — 18 tests pass.
2. **Index built:** `ai/prefilter/data/kNN.index` exists with ≥ 800 vectors.
3. **Models trained:** 5 `tier3_*.json` files exist; flag classifier AUC ≥ 0.80 in training log.
4. **Shadow run:** ≥ 5 rows in `prefilter_decisions` with `shadow_mode=true`.
5. **Agreement gate:** Tier 1 mean_agreement ≥ 0.90 in `scripts/eval_prefilter.py` output.
6. **Live Tier 1:** at least one `prefilter_decisions` row with `shadow_mode=false, short_circuited=true` after promoting.
7. **No regression:** existing test suite (`python -m pytest tests/ -v`) passes.

Tier 2 and Tier 3 stay shadow-only until they pass the gate in Task 11.
